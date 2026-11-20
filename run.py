#!/usr/bin/env python3
"""Benchmark rate-2 leanVM committee proofs and recursive slot aggregation."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import statistics
import struct
import subprocess
import tempfile
import time
from uuid import uuid4

from environment import hardware, session_fingerprint
from fixture_bundle import BUNDLE_DIRECTORY, FIXTURE_FORMAT, MAX_RECORDS, RECORD_SIZE, SIZES
from fixture_bundle import digest_records, fresh_committee_count, load_manifest, slot_geometry
from report import measured_rows, render_mean_stdev, save_json, warmed_timings, write_report

LOG_INV_RATE = 2
CPU_PROFILES = ("auto", "generic", "portable", "avx2", "avx2-gfni", "avx512")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def u64(value):
    return struct.pack("<Q", value)


def frame(data):
    return u64(len(data)) + data


def field(data, offset):
    require(offset + 8 <= len(data), "Missing sidecar field length")
    length, = struct.unpack_from("<Q", data, offset)
    end = offset + 8 + length
    require(end <= len(data), "Truncated sidecar field")
    return data[offset + 8:end], end


class Sidecar:
    def __init__(self, client, stream):
        self.client = client
        self.stream = stream
        hello = self.read()
        require(hello[:12] == b"leanvmsc\x01\x00\x00\x00", "Expected leanVM sidecar wire version 1")
        require(len(hello) >= 20, "Truncated sidecar hello")
        self.max_request, = struct.unpack_from("<Q", hello, 12)
        verifier, offset = field(hello, 20)
        prover, end = field(hello, offset)
        require(
            end == len(hello) and verifier == b"leanvm-v1" and prover == b"experimental",
            "Expected the leanvm-v1 verifier and experimental prover",
        )

    def read(self):
        prefix = self.stream.read(8)
        require(len(prefix) == 8, "Missing sidecar response")
        length, = struct.unpack("<Q", prefix)
        require(length <= 256 * 1024 * 1024, "Oversized sidecar response")
        data = self.stream.read(length)
        require(len(data) == length, "Truncated sidecar response")
        return data

    def request(self, body):
        require(len(body) <= self.max_request, "Request exceeds the sidecar limit")
        self.client.sendall(frame(body))
        response = self.read()
        require(response and response[0] == 0, f"Sidecar rejected request: {response[:200]!r}")
        return response[1:]

    def aggregate(self, children, records):
        body = bytes((1, LOG_INV_RATE)) + u64(len(children))
        body += b"".join(frame(claims) + frame(proof) for claims, proof in children)
        body += u64(len(records)) + b"".join(records) + u64(0) + u64(0) + b"\x00"
        response = self.request(body)
        claims, offset = field(response, 0)
        proof, end = field(response, offset)
        require(end == len(response) and proof, "Invalid aggregate response")
        return claims, proof

    def verify(self, pair):
        claims, proof = pair
        response = self.request(b"\x02" + frame(claims) + frame(proof))
        require(response in (b"\x00", b"\x01"), "Invalid verification response")
        return response == b"\x01"


@contextmanager
def run_lock(path, *, blocking):
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(f"Another benchmark process is using {path.parent}") from error
        yield


@contextmanager
def open_sidecar(args, settings, log_path):
    # A short socket path works with the Unix socket limits on Linux and macOS.
    with tempfile.TemporaryDirectory(prefix="pq-bench-", dir="/tmp") as temporary:
        path = Path(temporary) / "sidecar.sock"
        with log_path.open("w", encoding="utf-8") as log:
            start = time.monotonic()
            process = subprocess.Popen(
                [args.sidecar, "--socket", str(path), "--threads", str(settings["prover_threads"])],
                stdout=log, stderr=log,
            )
            try:
                while not path.exists():
                    require(process.poll() is None, f"Sidecar exited during startup. See {log_path}")
                    require(time.monotonic() - start < args.timeout, f"Sidecar startup timed out. See {log_path}")
                    time.sleep(0.05)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(args.timeout)
                    client.connect(str(path))
                    with client.makefile("rb") as stream:
                        connection = Sidecar(client, stream)
                        yield connection, time.monotonic() - start
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def claims_for(records):
    claims = sorted({record[:64] for record in records})
    require(len(claims) == len(records), "Duplicate claims would understate benchmark work")
    return u64(len(claims)) + b"".join(claims) + u64(0) + b"\x00"


def prepare_fixtures(args, manifest, count, prior_slot=False):
    start = time.monotonic()
    build = manifest["build"]
    workers = 0
    bundle = build["fixture_bundle"]
    label = " prior-slot" if prior_slot else ""
    if bundle:
        fixture_source = "bundle"
        print(f"  Loading {count} pre-generated{label} fixtures from the image", flush=True)
        with (BUNDLE_DIRECTORY / "fixtures.bin").open("rb") as payload:
            if prior_slot:
                payload.seek(MAX_RECORDS * RECORD_SIZE)
            records = [payload.read(RECORD_SIZE) for _ in range(count)]
    else:
        fixture_source = "cache"
        directory = args.fixture_cache / FIXTURE_FORMAT / build["leanvm_revision"]
        if prior_slot:
            directory /= "prior-slot"
        directory.mkdir(parents=True, exist_ok=True)
        with run_lock(directory / ".lock", blocking=True):
            paths = [directory / f"key-{index:05}.bin" for index in range(count)]
            missing = sum(not path.is_file() for path in paths)
            if missing:
                fixture_source = "generated"
                workers = min(missing, manifest["settings"]["fixture_threads"])
                print(f"  Generating {missing}{label} fixtures; {count - missing} already cached", flush=True)
                command = [args.generator, str(directory), str(count), str(workers), "1"]
                if prior_slot:
                    command.append("--prior-slot")
                subprocess.run(command, check=True)
            else:
                print(f"  Reusing {count} cached{label} fixtures", flush=True)
            records = [path.read_bytes() for path in paths]

    digest = digest_records(records, prior_slot)
    if bundle:
        segment = bundle["prior_slot"] if prior_slot else bundle
        require(digest == segment["prefix_sha256"][str(count)], "Bundled fixture prefix failed its SHA-256 check")
    return records, {
        "fixture_source": fixture_source,
        "fixture_sha256": digest,
        "fixture_preparation_seconds": time.monotonic() - start,
        "fixture_workers_used": workers,
    }


def timed(call):
    start = time.perf_counter_ns()
    value = call()
    return value, (time.perf_counter_ns() - start) / 1e6


def stats(samples):
    return {
        "samples_ms": samples,
        "median_ms": statistics.median(samples),
        "min_ms": min(samples),
        "max_ms": max(samples),
    }


def verify_claims(connection, pair, expected_claims):
    require(pair[0] == expected_claims, "Aggregate claims differ from the expected claim union")
    valid, verify_ms = timed(lambda: connection.verify(pair))
    require(valid, "Sidecar rejected its own proof")
    return verify_ms


def prove(connection, children, records, expected_claims):
    pair, prove_ms = timed(lambda: connection.aggregate(children, records))
    return pair, prove_ms, verify_claims(connection, pair, expected_claims)


def check_wrong_message(connection, pair):
    claims, proof = pair
    wrong = bytearray(claims)
    wrong[8 + 32] ^= 1
    require(not connection.verify((bytes(wrong), proof)), "Verifier accepted a wrong message")


def repeat_proof(repetitions, connection, children, records, claims, label=None):
    proves, verifies, sizes = [], [], []
    for index in range(repetitions):
        pair, prove_ms, verify_ms = prove(connection, children, records, claims)
        proves.append(prove_ms)
        verifies.append(verify_ms)
        sizes.append(len(pair[1]))
        if label:
            print(f"  {label} {index + 1}/{repetitions}: {prove_ms:.1f} ms, verified", flush=True)
    return stats(proves), stats(verifies), sizes


def measure_improvement(connection, children, fresh_children, first_claims, final_claims, repetitions):
    def sample():
        ready = children[0]
        first_ms = total_ms = None
        if len(children) > 1:
            start = time.perf_counter_ns()
            ready, first_ms = timed(lambda: connection.aggregate(children, []))
        improved, improvement_ms = timed(lambda: connection.aggregate([ready, *fresh_children], []))
        if first_ms is not None:
            total_ms = (time.perf_counter_ns() - start) / 1e6
            verify_claims(connection, ready, first_claims)
        verify_ms = verify_claims(connection, improved, final_claims)
        return improved, improvement_ms, first_ms, total_ms, verify_ms

    initial, initial_ms, initial_first_ms, initial_total_ms, initial_verify_ms = sample()
    check_wrong_message(connection, initial)
    require(
        not connection.verify((first_claims, initial[1])),
        "Improved proof verified against the incomplete original claim set",
    )
    improvements, first_rounds, totals, verifies, sizes = [], [], [], [], []
    for index in range(repetitions):
        improved, improvement_ms, first_ms, total_ms, verify_ms = sample()
        improvements.append(improvement_ms)
        verifies.append(verify_ms)
        sizes.append(len(improved[1]))
        if first_ms is not None:
            first_rounds.append(first_ms)
            totals.append(total_ms)
        print(f"  improvement {index + 1}/{repetitions}: {improvement_ms:.1f} ms, verified", flush=True)
    return {
        "first_improvement_ms": initial_ms,
        "first_improved_verify_ms": initial_verify_ms,
        "first_paired_merge_ms": initial_first_ms,
        "first_two_round_merge_ms": initial_total_ms,
        "proposer_improvement": stats(improvements),
        "paired_first_merge": stats(first_rounds) if first_rounds else None,
        "two_round_merge": stats(totals) if totals else None,
        "improved_verify": stats(verifies),
        "improved_proof_bytes": len(initial[1]),
        "warmed_improved_proof_bytes": sizes,
        "improved_wrong_message_rejected": True,
        "improved_incomplete_claims_rejected": True,
    }


def run_case(args, manifest, size):
    committees, per_committee = slot_geometry(size)
    count = committees * per_committee
    settings = manifest["settings"]
    result = {
        "run_id": manifest["run_id"],
        "active_validators": size,
        "committees_per_slot": committees,
        "signatures_per_committee": per_committee,
        "signatures_per_slot": count,
        "merge_operations": int(committees > 1),
        "utc_start": utc_now(),
    }
    print(f"N={size}, C={committees}, K={per_committee}", flush=True)
    records, fixture_metadata = prepare_fixtures(args, manifest, count)
    result.update(fixture_metadata)
    expected = claims_for(records)
    groups = [
        records[start:start + per_committee] for start in range(0, len(records), per_committee)
    ]
    group_claims = [claims_for(group) for group in groups]
    repetitions = settings["repetitions"]

    result["load_before"] = os.getloadavg()
    log = args.output / "cases" / f"{size}-sidecar.log"
    with open_sidecar(args, settings, log) as (connection, startup_seconds):
        result["sidecar_startup_seconds"] = startup_seconds
        children, prove_times, verify_times = [], [], []
        for index, (group, claims) in enumerate(zip(groups, group_claims)):
            pair, prove_ms, verify_ms = prove(connection, [], group, claims)
            children.append(pair)
            prove_times.append(prove_ms)
            verify_times.append(verify_ms)
            print(
                f"  child {index + 1}/{committees}: {prove_ms:.1f} ms, {len(pair[1])} bytes, verified",
                flush=True,
            )
            if index == 0:
                warmed_prove, warmed_verify, sizes = repeat_proof(
                    repetitions, connection, [], group, claims
                )
                result.update(
                    committee_prove=warmed_prove,
                    committee_verify=warmed_verify,
                    committee_proof_bytes=len(pair[1]),
                    warmed_committee_proof_bytes=sizes,
                )
                check_wrong_message(connection, pair)

        result.update(
            initial_child_prove_ms=prove_times,
            initial_child_verify_ms=verify_times,
            child_proof_bytes_total=sum(len(pair[1]) for pair in children),
        )
        if committees > 1:
            merged, first_ms, first_verify_ms = prove(connection, children, [], expected)
            merge, merge_verify, sizes = repeat_proof(
                repetitions, connection, children, [], expected, "merge"
            )
            check_wrong_message(connection, merged)
            result.update(
                first_merge_ms=first_ms,
                first_merge_verify_ms=first_verify_ms,
                proposer_merge=merge,
                final_verify=merge_verify,
                final_proof_bytes=len(merged[1]),
                warmed_final_proof_bytes=sizes,
            )
        else:
            result.update(
                first_merge_ms=None,
                first_merge_verify_ms=None,
                proposer_merge=None,
                final_verify=result["committee_verify"],
                final_proof_bytes=result["committee_proof_bytes"],
                warmed_final_proof_bytes=result["warmed_committee_proof_bytes"],
            )

        fresh_committees = fresh_committee_count(committees)
        fresh_count = fresh_committees * per_committee
        older_records, prior_metadata = prepare_fixtures(args, manifest, fresh_count, prior_slot=True)
        require(
            {record[:32] for record in records}.isdisjoint(record[:32] for record in older_records),
            "Prior-slot fixture keys overlap the current slot",
        )
        improved_claims = claims_for(records + older_records)
        fresh_children = []
        fresh_proving_ms = []
        for start in range(0, fresh_count, per_committee):
            group = older_records[start:start + per_committee]
            pair, elapsed, _ = prove(connection, [], group, claims_for(group))
            fresh_children.append(pair)
            fresh_proving_ms.append(elapsed)
        result.update(
            improvement_fan_in=1 + fresh_committees,
            improvement_fresh_proofs=fresh_committees,
            improvement_added_claims=fresh_count,
            improvement_total_claims=count + fresh_count,
            improvement_message_groups=2,
            prior_slot_fixture=prior_metadata,
            fresh_child_proving_ms=fresh_proving_ms,
        )
        print(
            f"  improvement: ready proof + {fresh_committees} prior-slot proofs, "
            f"{fresh_count} new claims",
            flush=True,
        )
        result.update(measure_improvement(connection, children, fresh_children,
                                          expected, improved_claims, repetitions))

    result.update(
        all_proofs_verified=True,
        wrong_message_rejected=True,
        merged_wrong_message_rejected=True if committees > 1 else None,
        load_after=os.getloadavg(),
        utc_end=utc_now(),
    )
    return result


def positive_integer(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a positive integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return value


def executable(value):
    path = shutil.which(value)
    if path is None:
        raise ValueError(f"Executable not found: {value}")
    return str(Path(path).resolve())


def select_sidecar(variants, profile, cpu_flags):
    if profile == "auto":
        for candidate in reversed(variants):
            if set(candidate["required_linux_cpu_flags"]) <= cpu_flags:
                return candidate
        raise ValueError("No sidecar build matches the CPU features reported by Linux")

    for candidate in variants:
        if candidate["profile"] != profile:
            continue
        missing = set(candidate["required_linux_cpu_flags"]) - cpu_flags
        require(not missing, f"CPU profile {profile} requires Linux CPU flags: {', '.join(sorted(missing))}")
        return candidate
    raise ValueError(f"CPU profile {profile} is unavailable in this image")


def build_info(args, resources):
    result = json.loads(args.build_info.read_text(encoding="utf-8"))
    require(re.fullmatch("[0-9a-f]{40}", result["leanvm_revision"]), "Missing full leanVM commit SHA")
    require("experimental_prover" in result["sidecar_cargo_features"], "Expected experimental prover build")
    variants = result.get("sidecar_variants")
    if variants is None:
        variants = [{
            "profile": "portable",
            "binary": "leanvm-sidecar",
            "required_linux_cpu_flags": [],
            "target_features": [],
            "rustflags": result["rustflags"],
            "sidecar_sha256": result["sidecar_sha256"],
        }]
    selected = select_sidecar(variants, args.cpu_profile, set(resources["cpu_flags"]))
    args.sidecar = executable(args.sidecar or selected["binary"])
    result.update(
        sidecar_cpu_profile=selected["profile"],
        sidecar_binary=Path(args.sidecar).name,
        sidecar_target_features=selected["target_features"],
        sidecar_required_linux_cpu_flags=selected["required_linux_cpu_flags"],
        sidecar_sha256=selected["sidecar_sha256"],
        rustflags=selected["rustflags"],
    )
    for path, key in (
        (args.sidecar, "sidecar_sha256"), (args.generator, "fixture_generator_sha256")
    ):
        with open(path, "rb") as binary:
            digest = hashlib.file_digest(binary, "sha256").hexdigest()
        require(digest == result[key], f"Executable hash differs from build metadata: {path}")
    result["fixture_bundle"] = (
        load_manifest(BUNDLE_DIRECTORY, result["leanvm_revision"]) if BUNDLE_DIRECTORY.exists() else None
    )
    if result["fixture_bundle"]:
        require("prior_slot" in result["fixture_bundle"], "The bundled image needs prior-slot fixtures")
    result["harness_sha256"] = {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in ("run.py", "environment.py", "fixture_bundle.py", "report.py")
    }
    return result


def get_manifest(args, resources):
    default_threads = max(1, math.floor(resources["effective_cpu_capacity"]))
    settings = {
        "sizes": sorted(args.sizes),
        "prover_threads": args.threads or default_threads,
        "cpu_profile": args.cpu_profile,
        "fixture_threads": args.fixture_threads or default_threads,
        "repetitions": args.reps,
        "sidecar_timeout_seconds": args.timeout,
        "log_inv_rate": LOG_INV_RATE,
        "fixture_format": FIXTURE_FORMAT,
        "improvement_workload": "prior-slot-append-v1",
        "execution": args.execution,
        "host_cpu_model": args.host_cpu_model,
    }
    compatibility = {"schema_version": 1, "hardware": resources, "build": build_info(args, resources), "settings": settings}
    path = args.output / "run.json"
    if path.exists():
        require(args.resume, f"{path} already exists. Use --resume or a new output directory.")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for key, value in compatibility.items():
            require(manifest.get(key) == value, f"Cannot resume because {key} changed. Use a new output directory.")
        old_session = manifest["kernel_session"]
        require(
            old_session["digest"] is not None
            and old_session == session_fingerprint(old_session["salt"]),
            "Cannot confirm the same kernel boot. Use a new output directory.",
        )
        return manifest

    require(not args.resume, f"No run manifest at {path}. Start without --resume.")
    require(
        all(item.name == ".lock" for item in args.output.iterdir()),
        f"Output directory is not empty: {args.output}",
    )
    manifest = {
        **compatibility,
        "kernel_session": session_fingerprint(),
        "run_id": uuid4().hex,
        "utc_start": utc_now(),
    }
    save_json(path, manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", choices=SIZES, default=SIZES, metavar="N",
                        help="validator counts (default: full sweep: %(choices)s)")
    parser.add_argument("--threads", type=positive_integer, help="prover threads (default: effective CPU capacity)")
    parser.add_argument("--fixture-threads", type=positive_integer,
                        help="fixture workers (default: effective CPU capacity)")
    parser.add_argument("--reps", type=positive_integer, default=5, help="warmed repetitions (default: %(default)s)")
    parser.add_argument("--timeout", type=positive_integer, default=600,
                        help="sidecar startup and socket timeout in seconds (default: %(default)s)")
    parser.add_argument("--output", type=Path, default=os.environ.get("BENCH_OUTPUT", "results"))
    parser.add_argument("--fixture-cache", type=Path, default=os.environ.get("BENCH_FIXTURES", "fixtures"))
    parser.add_argument("--resume", action="store_true", help="skip completed cases in a compatible run")
    parser.add_argument("--execution", choices=("native", "emulated", "unverified"), default="unverified",
                        help="operator declaration for this run (default: %(default)s)")
    parser.add_argument("--host-cpu-model", help="optional host model when running in a VM")
    parser.add_argument("--build-info", type=Path, default=Path(__file__).with_name("build.json"))
    parser.add_argument("--cpu-profile", choices=CPU_PROFILES, default="auto",
                        help="compiled prover CPU profile (default: use the highest supported in the image)")
    parser.add_argument("--sidecar", help="override the selected sidecar executable with the same digest")
    parser.add_argument("--generator", default="pq-geometry-fixtures")
    args = parser.parse_args()
    if len(set(args.sizes)) != len(args.sizes):
        parser.error("--sizes must not contain duplicates")
    if args.host_cpu_model and (len(args.host_cpu_model) > 160 or not args.host_cpu_model.isprintable()):
        parser.error("--host-cpu-model must contain at most 160 printable characters")
    args.output = args.output.resolve()
    args.fixture_cache = args.fixture_cache.resolve()
    if args.output == args.fixture_cache:
        parser.error("--output and --fixture-cache must be different directories")

    try:
        args.generator = executable(args.generator)
        args.output.mkdir(parents=True, exist_ok=True)
        with run_lock(args.output / ".lock", blocking=False):
            manifest = get_manifest(args, hardware())
            case_directory = args.output / "cases"
            case_directory.mkdir(exist_ok=True)
            rows = measured_rows(args.output, manifest)
            completed = {row["active_validators"] for row in rows}
            write_report(args.output, manifest, rows)
            for size in manifest["settings"]["sizes"]:
                if size in completed:
                    print(f"Skipping completed case N={size}", flush=True)
                    continue
                result = run_case(args, manifest, size)
                save_json(case_directory / f"{size}.json", result)
                rows = measured_rows(args.output, manifest)
                write_report(args.output, manifest, rows)
            print(f"Reports written to {args.output}", flush=True)
            table = render_mean_stdev(manifest, warmed_timings(rows))
            print(table, end="", flush=True)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Benchmark failed: {error}\n")


def stop(signum, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Benchmark interrupted") from None
