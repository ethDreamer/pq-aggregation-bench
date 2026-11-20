"""Pack or verify the deterministic XMSS fixture set."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import tempfile

SIZES = (64, 1024, 4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576)
RECORD_SIZE = 32 + 32 + 2404
FIXTURE_FORMAT = "one-leaf-xmss-v1"
ATT_MESSAGE = bytes(8) + bytes([0x42]) * 24
ATT_LEAF = 32768
BUNDLE_DIRECTORY = Path(__file__).with_name("bundled-fixtures")
BASELINE_RESULTS = Path(__file__).with_name("baseline") / "results"
# Verified against every record using the corresponding XMSS verifier.
COMPATIBLE_REVISIONS = {"c5ee9d239e42d0bc8113eb4aee3e2cae5faf1764"}


def slot_geometry(size):
    committees = max(1, min(16, size // 4096))
    per_committee = size // (32 * committees)
    return committees, per_committee


def record_count(size):
    committees, per_committee = slot_geometry(size)
    return committees * per_committee


MAX_RECORDS = record_count(SIZES[-1])
PREFIX_COUNTS = {str(record_count(size)) for size in SIZES}


def validate_record(record, keys):
    if len(record) != RECORD_SIZE:
        raise ValueError("Wrong fixture size")
    key = record[:32]
    if key in keys:
        raise ValueError("Public keys must be disjoint")
    keys.add(key)
    if record[32:64] != ATT_MESSAGE:
        raise ValueError("Unexpected attestation message")
    if struct.unpack_from("<I", record, 64)[0] != ATT_LEAF:
        raise ValueError("Unexpected XMSS leaf index")


def digest_records(records):
    keys = set()
    digest = hashlib.sha256()
    for record in records:
        validate_record(record, keys)
        digest.update(record)
    return digest.hexdigest()


def reference_fields(revision):
    return {
        "schema_version": 1,
        "fixture_format": FIXTURE_FORMAT,
        "leanvm_revision": revision,
        "record_bytes": RECORD_SIZE,
        "record_count": MAX_RECORDS,
    }


def load_manifest(directory, revision):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for name, value in reference_fields(revision).items():
        if manifest.get(name) != value:
            raise ValueError(f"Fixture bundle has incompatible {name}")
    prefixes = manifest.get("prefix_sha256")
    if (
        not isinstance(prefixes, dict)
        or set(prefixes) != PREFIX_COUNTS
        or not all(
            isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest)
            for digest in prefixes.values()
        )
        or manifest.get("sha256") != prefixes[str(MAX_RECORDS)]
    ):
        raise ValueError("Invalid fixture bundle digests")
    if (directory / "fixtures.bin").stat().st_size != MAX_RECORDS * RECORD_SIZE:
        raise ValueError("Wrong fixture bundle payload size")
    return manifest


def verify_bundle(directory, revision, expected_digest):
    manifest = load_manifest(directory, revision)
    if manifest["sha256"] != expected_digest:
        raise ValueError("Fixture bundle does not match the requested digest")
    digest = hashlib.sha256()
    keys = set()
    with (directory / "fixtures.bin").open("rb") as payload:
        for index in range(1, MAX_RECORDS + 1):
            record = payload.read(RECORD_SIZE)
            validate_record(record, keys)
            digest.update(record)
            expected = manifest["prefix_sha256"].get(str(index))
            if expected and digest.hexdigest() != expected:
                raise ValueError(f"Fixture bundle digest mismatch at record {index}")
    return manifest


def pack_fixtures(source, output, requested_revision=None):
    if output.exists():
        raise ValueError(f"Bundle destination already exists: {output}")
    baseline_hashes = {}
    revisions = set()
    for size in SIZES:
        baseline = json.loads((BASELINE_RESULTS / f"{size}.json").read_text(encoding="utf-8"))
        records = record_count(size)
        if baseline["active_validators"] != size or baseline["signatures_per_slot"] != records:
            raise ValueError(f"Unexpected geometry in baseline for N={size}")
        baseline_hashes[str(records)] = baseline["fixture_sha256"]
        revisions.add(baseline["prover_pin"])
    if len(revisions) != 1:
        raise ValueError("Expected one leanVM revision across all baseline cases")
    baseline_revision = revisions.pop()
    revision = requested_revision or baseline_revision
    if revision != baseline_revision and revision not in COMPATIBLE_REVISIONS:
        raise ValueError(f"Fixture compatibility has not been established for {revision}")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fixture-pack-", dir=output.parent) as directory:
        staging = Path(directory)
        digest = hashlib.sha256()
        keys = set()
        with (staging / "fixtures.bin").open("wb") as payload:
            for index in range(MAX_RECORDS):
                record = (source / f"key-{index:05}.bin").read_bytes()
                validate_record(record, keys)
                payload.write(record)
                digest.update(record)
                expected = baseline_hashes.get(str(index + 1))
                if expected and digest.hexdigest() != expected:
                    raise ValueError(f"Historical fixture digest mismatch at record {index + 1}")
        manifest = {
            **reference_fields(revision),
            "sha256": digest.hexdigest(),
            "prefix_sha256": baseline_hashes,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        staging.rename(output)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    pack = actions.add_parser("pack", help="pack records and verify all baseline hashes")
    pack.add_argument("source", type=Path)
    pack.add_argument("output", type=Path)
    pack.add_argument("--leanvm-revision", help="validated full leanVM SHA (default: historical reference)")
    verify = actions.add_parser("verify", help="verify packed records and their manifest")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("revision")
    verify.add_argument("--sha256", help="expected full payload SHA-256")
    args = parser.parse_args()
    try:
        if args.action == "pack":
            manifest = pack_fixtures(args.source, args.output, args.leanvm_revision)
        else:
            digest = args.sha256 or load_manifest(args.bundle, args.revision)["sha256"]
            manifest = verify_bundle(args.bundle, args.revision, digest)
        print(manifest["sha256"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Fixture bundle failed: {error}\n")


if __name__ == "__main__":
    main()
