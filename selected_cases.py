"""Individually selected workloads, separate from the historical full-suite schema."""

import hashlib
import json
import os
import subprocess
import time

from fixture_bundle import FIXTURE_FORMAT, RECORD_SIZE, slot_geometry
from report import format_timing, markdown_table, save_json, save_text, terminal_table, timing_statistics, write_csv

TESTS = {
    "committee-proof": "Prove one committee at each selected validator geometry",
    "slot-merge": "Merge one slot's committees (reuse if there is only one)",
    "round2": "Extend a prepared round-1 proof with first prior-slot committees",
    "round3": "Extend a prepared round-2 proof with second prior-slot committees",
    "prune": "Prune the oldest message from a prepared round-3 proof",
    "merge-16x2048-same-message": "Merge 16 proofs × 2,048 disjoint signers, one message",
    "merge-16x2048-multi-message": "Merge 16 proofs × 2,048 disjoint signers, 16 slot messages",
}
FIXED_TESTS = frozenset(name for name in TESTS if name.startswith("merge-16x2048"))
WORKLOAD_VERSION = "selected-workloads-v1"


def case_plan(tests, sizes):
    return [
        {"case_id": test if test in FIXED_TESTS else f"{test}-n{size}",
         "test": test, "active_validators": size}
        for test in TESTS if test in tests
        for size in ([None] if test in FIXED_TESTS else sorted(sizes))
    ]


def multi_message_fixtures(args, manifest, count=32768, messages=16):
    """Separate cache namespace: changing message grouping changes signatures."""
    import run as engine
    start = time.monotonic()
    directory = args.fixture_cache / FIXTURE_FORMAT / manifest["build"]["leanvm_revision"] / f"messages-v1-m{messages}-n{count}"
    directory.mkdir(parents=True, exist_ok=True)
    with engine.run_lock(directory / ".lock", blocking=True):
        paths = [directory / f"key-{i:05}.bin" for i in range(count)]
        missing = sum(not p.is_file() for p in paths)
        workers = min(missing, manifest["settings"]["fixture_threads"])
        if missing:
            print(f"  Generating {missing} fixtures for {messages} distinct messages", flush=True)
            subprocess.run([args.generator, str(directory), str(count), str(workers), str(messages)], check=True)
        records = [p.read_bytes() for p in paths]
    per_message = count // messages
    for i, record in enumerate(records):
        expected_message = (i // per_message).to_bytes(8, "little") + bytes([0x42]) * 24
        engine.require(len(record) == RECORD_SIZE and record[32:64] == expected_message,
                       "Multi-message fixture has wrong size or signed message")
        engine.require(record[64:68] == (32768).to_bytes(4, "little"), "Wrong same-epoch signing leaf")
    engine.require(len({r[:32] for r in records}) == count, "Multi-message fixture keys overlap")
    return records, {
        "fixture_source": "generated" if missing else "cache",
        "fixture_sha256": hashlib.sha256(b"".join(records)).hexdigest(),
        "fixture_preparation_seconds": time.monotonic() - start,
        "fixture_workers_used": workers, "message_groups": messages,
        "fixture_workload": f"messages-v1-m{messages}-n{count}",
    }


def run_selected_case(args, manifest, case):
    import run as engine
    test, size = case["test"], case["active_validators"]
    committees, per_committee = (16, 2048) if test in FIXED_TESTS else slot_geometry(size)
    count = per_committee if test == "committee-proof" else committees * per_committee
    result = {**case, "run_id": manifest["run_id"], "workload_version": WORKLOAD_VERSION,
              "utc_start": engine.utc_now(), "load_before": os.getloadavg(),
              "signatures_per_fresh_proof": per_committee, "setup": {}}
    print(f"Test {case['case_id']}: {committees} committees, {per_committee} signatures/committee", flush=True)
    setup_start = time.monotonic()
    if test == "merge-16x2048-multi-message":
        records, metadata = multi_message_fixtures(args, manifest)
    else:
        records, metadata = engine.prepare_fixtures(args, manifest, count)
    result["setup"]["current_fixtures"] = metadata
    expected = engine.claims_for(records)
    all_records = records
    connection_log = args.output / "cases" / f"{case['case_id']}-sidecar.log"
    with engine.open_sidecar(args, manifest["settings"], connection_log) as (connection, startup):
        result["setup"]["sidecar_startup_seconds"] = startup
        children, child_timings = [], []
        if test != "committee-proof":
            for i in range(0, len(records), per_committee):
                group = records[i:i + per_committee]
                child, elapsed, verified = engine.prove(connection, [], group, engine.claims_for(group))
                children.append(child)
                child_timings.append({"prove_ms": elapsed, "verify_ms": verified})
                print(f"  prepared child {len(children)}/{committees}", flush=True)
        result["setup"]["input_proofs"] = child_timings
        input_children, raw_records, retained = children, [], None
        input_records = records
        if test == "committee-proof":
            input_children, raw_records = [], records
        elif test in ("round2", "round3", "prune"):
            ready = children[0] if len(children) == 1 else engine.prove(connection, children, [], expected)[0]
            fresh_count = engine.fresh_committee_count(committees) * per_committee
            rounds = 1 if test == "round2" else 2
            for slots_back in range(1, rounds + 1):
                fresh, meta, fresh_children, times = engine.prepare_fresh_children(
                    args, manifest, connection, per_committee, fresh_count, slots_back)
                result["setup"][f"prior{slots_back}_fixtures"] = meta
                result["setup"][f"prior{slots_back}_input_proving_ms"] = times
                retained = expected
                all_records = all_records + fresh
                engine.require(len({r[:32] for r in all_records}) == len(all_records), "Fixture keys overlap")
                input_records = all_records
                expected = engine.claims_for(all_records)
                input_children = [ready, *fresh_children]
                if slots_back < rounds or test == "prune":
                    ready = engine.prove(connection, input_children, [], expected)[0]
            if test == "prune":
                input_children = [ready]
                expected = retained
        result["setup"]["total_seconds"] = time.monotonic() - setup_start
        result.update(input_proof_bytes=sum(len(p[1]) for p in input_children),
                      input_proofs=len(input_children), input_claims=len(input_records),
                      output_claims=int.from_bytes(expected[:8], "little"),
                      input_messages=len({r[32:64] for r in input_records}),
                      output_messages=len({expected[i+32:i+64] for i in range(8, 8 + int.from_bytes(expected[:8], 'little') * 64, 64)}))
        reuse = test == "slot-merge" and len(children) == 1
        result["reuse"] = reuse
        def request():
            return children[0] if reuse else connection.aggregate(input_children, raw_records, expected if test == "prune" else None)
        initial, elapsed = engine.timed(request)
        initial_verify = engine.verify_claims(connection, initial, expected)
        engine.check_wrong_message(connection, initial)
        missing = engine.u64(result["output_claims"] - 1) + expected[8 + 64:]
        engine.require(not connection.verify((missing, initial[1])), "Verifier accepted missing claims")
        if test == "prune":
            engine.require(not connection.verify((engine.claims_for(input_records), initial[1])), "Pruned proof accepted original claims")
        timings, verifies, sizes = [], [], []
        for i in range(manifest["settings"]["repetitions"]):
            pair, elapsed_ms = engine.timed(request)
            verifies.append(engine.verify_claims(connection, pair, expected))
            if not reuse:
                timings.append(elapsed_ms)
            sizes.append(len(pair[1]))
            print(f"  {test} {i + 1}: {'reuse' if reuse else f'{elapsed_ms:.1f} ms'}, verified", flush=True)
        result.update(first_operation_ms=None if reuse else elapsed, first_verify_ms=initial_verify,
                      operation=engine.stats(timings) if timings else None,
                      verify=engine.stats(verifies), proof_bytes=len(initial[1]), warmed_proof_bytes=sizes,
                      all_proofs_verified=True, wrong_message_rejected=True, missing_claims_rejected=True)
    result.update(load_after=os.getloadavg(), utc_end=engine.utc_now())
    return result


def valid_case(row, manifest, case):
    return (row.get("run_id") == manifest["run_id"] and row.get("all_proofs_verified") is True
            and row.get("workload_version") == manifest["settings"].get("selected_workload_version")
            and all(row.get(k) == v for k, v in case.items()))


def selected_rows(directory, manifest):
    rows = []
    for case in manifest["settings"]["selected_cases"]:
        path = directory / "cases" / f"{case['case_id']}.json"
        if path.exists():
            row = json.loads(path.read_text())
            if not valid_case(row, manifest, case):
                raise ValueError(f"Case does not match selected workload: {path}")
            rows.append(row)
    return rows


def render_selected(manifest, rows, *, markdown=False):
    hardware, build, settings = (manifest[k] for k in ("hardware", "build", "settings"))
    table = markdown_table if markdown else terminal_table
    lines = ["PQ selected benchmark timing statistics", "",
             f"Runtime CPU {', '.join(hardware['cpu_models']) or 'unknown'} ({hardware['architecture']}, {settings['execution']}).",
             f"Runtime OS {hardware['os']} {hardware.get('os_release') or ''}.",
             f"{hardware['logical_cpus_visible']} visible logical CPUs; effective allocation {hardware['effective_cpu_capacity']:g}; prover threads {settings['prover_threads']}.",
             f"Runtime-visible RAM {hardware['memory_visible_bytes'] / 2**30:.1f} GiB." if hardware.get('memory_visible_bytes') else "Runtime-visible RAM unknown.",
             f"leanVM {build['leanvm_revision']}; bindings {build.get('leanvm_bindings_version')}; CPU profile {build.get('sidecar_cpu_profile', 'unknown')}; log inverse rate {settings['log_inv_rate']}.",
             f"Completed {len(rows)}/{len(settings['selected_cases'])} selected cases. Benchmark {build['benchmark_revision']}.", "",
             f"Timing boundary: {settings.get('operation_timing', 'legacy-rpc-v1')}. Raw orchestration timings are in per-case .rpc.json files for in-process runs.",
             "Mean ± sample SD (ms) of warmed selected operations. Initial requests, fixture generation, input proving and verification are outside operation timing.",
             "n counts timed operation samples (zero for reuse). Signatures/input is the fresh committee size; recursive input proofs may cover more claims. N is only specified for modeled validator geometries.", ""]
    if hardware.get("memory_cgroup_limit_bytes"):
        lines.insert(7, f"Container memory limit {hardware['memory_cgroup_limit_bytes'] / 2**30:.1f} GiB.")
    if settings.get("host_cpu_model"):
        lines.insert(7, f"Operator reports a physical host CPU of {settings['host_cpu_model']}.")
    columns = ("Test", "N", "Sigs/input", "Input proofs", "Input claims", "Input msgs", "Output claims", "Output msgs", "Operation (ms)", "Verify (ms)", "n")
    values = [(r['test'], r['active_validators'] if r['active_validators'] is not None else '—',
               r['signatures_per_fresh_proof'], r['input_proofs'], r['input_claims'], r['input_messages'],
               r['output_claims'], r['output_messages'], 'Reuse' if r['reuse'] else format_timing(timing_statistics(r['operation'])),
               format_timing(timing_statistics(r['verify'])), len(r['operation']['samples_ms']) if r['operation'] else 0) for r in rows]
    lines.extend(table(columns, values))
    lines.extend(["", "Setup (excluded from measured operation)", ""])
    lines.extend(table(("Case", "Setup elapsed (s)", "Input proof bytes", "Output proof bytes"),
                       [(r['case_id'], f"{r['setup']['total_seconds']:.3f}", r['input_proof_bytes'], r['proof_bytes']) for r in rows]))
    return "\n".join(lines) + "\n"


def write_selected_report(directory, manifest, rows):
    save_json(directory / "summary.json", {"run": manifest, "cases": rows})
    text = render_selected(manifest, rows, markdown=True)
    save_text(directory / "results.md", text)
    save_text(directory / "results-mean-stdev.md", text)
    columns = ("case_id", "test", "active_validators", "signatures_per_fresh_proof", "input_proofs", "input_claims", "input_messages", "output_claims", "output_messages", "setup_seconds", "first_operation_ms", "operation_mean_ms", "operation_sample_stdev_ms", "operation_n", "operation_median_ms", "verify_mean_ms", "verify_sample_stdev_ms", "verify_n", "verify_median_ms")
    values = [(r['case_id'], r['test'], r['active_validators'], r['signatures_per_fresh_proof'], r['input_proofs'], r['input_claims'], r['input_messages'], r['output_claims'], r['output_messages'], r['setup']['total_seconds'], r['first_operation_ms'], *timing_statistics(r['operation']), r['operation']['median_ms'] if r['operation'] else None, *timing_statistics(r['verify']), r['verify']['median_ms']) for r in rows]
    for name in ("results.csv", "results-mean-stdev.csv"):
        write_csv(directory / name, columns, values)
