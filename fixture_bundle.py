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
PRIOR_SLOT_MESSAGE = b"\x01" + ATT_MESSAGE[1:]
MAX_IMPROVEMENT_CHILDREN = 16
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


def fresh_committee_count(committees):
    return min(committees, MAX_IMPROVEMENT_CHILDREN - 1)


def prior_record_count(size):
    committees, per_committee = slot_geometry(size)
    return fresh_committee_count(committees) * per_committee


MAX_RECORDS = record_count(SIZES[-1])
MAX_PRIOR_RECORDS = prior_record_count(SIZES[-1])
PREFIX_COUNTS = {str(record_count(size)) for size in SIZES}
PRIOR_PREFIX_COUNTS = {str(prior_record_count(size)) for size in SIZES}


def validate_record(record, keys, prior_slot=False):
    if len(record) != RECORD_SIZE:
        raise ValueError("Wrong fixture size")
    key = record[:32]
    if key in keys:
        raise ValueError("Public keys must be disjoint")
    keys.add(key)
    if record[32:64] != (PRIOR_SLOT_MESSAGE if prior_slot else ATT_MESSAGE):
        raise ValueError("Unexpected attestation message")
    if struct.unpack_from("<I", record, 64)[0] != ATT_LEAF - int(prior_slot):
        raise ValueError("Unexpected XMSS leaf index")


def digest_records(records, prior_slot=False):
    keys = set()
    digest = hashlib.sha256()
    for record in records:
        validate_record(record, keys, prior_slot)
        digest.update(record)
    return digest.hexdigest()


def reference_fields(revision, version):
    return {
        "schema_version": version,
        "fixture_format": FIXTURE_FORMAT,
        "leanvm_revision": revision,
        "record_bytes": RECORD_SIZE,
        "record_count": MAX_RECORDS,
    }


def is_sha256(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def check_prefixes(prefixes, counts):
    if (
        not isinstance(prefixes, dict)
        or set(prefixes) != counts
        or not all(is_sha256(digest) for digest in prefixes.values())
    ):
        raise ValueError("Invalid fixture bundle prefix digests")


def load_manifest(directory, revision):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    version = manifest.get("schema_version")
    if version not in (1, 2):
        raise ValueError("Unsupported fixture bundle schema")
    for name, value in reference_fields(revision, version).items():
        if manifest.get(name) != value:
            raise ValueError(f"Fixture bundle has incompatible {name}")

    count = MAX_RECORDS
    prefixes = manifest.get("prefix_sha256")
    check_prefixes(prefixes, PREFIX_COUNTS)
    if not is_sha256(manifest.get("sha256")):
        raise ValueError("Invalid fixture bundle digest")
    if version == 1:
        if manifest["sha256"] != prefixes[str(MAX_RECORDS)] or "prior_slot" in manifest:
            raise ValueError("Invalid legacy fixture bundle digest or metadata")
    else:
        prior = manifest.get("prior_slot")
        if not isinstance(prior, dict) or prior.get("record_count") != MAX_PRIOR_RECORDS:
            raise ValueError("Invalid prior-slot fixture count")
        check_prefixes(prior.get("prefix_sha256"), PRIOR_PREFIX_COUNTS)
        count += MAX_PRIOR_RECORDS
    if (directory / "fixtures.bin").stat().st_size != count * RECORD_SIZE:
        raise ValueError("Wrong fixture bundle payload size")
    return manifest


def scan_records(records, cutoffs, keys, digest, *, prior_slot=False, output=None):
    prefixes = {}
    segment = hashlib.sha256()
    for index, record in enumerate(records, start=1):
        validate_record(record, keys, prior_slot)
        if output is not None:
            output.write(record)
        digest.update(record)
        segment.update(record)
        if str(index) in cutoffs:
            prefixes[str(index)] = segment.hexdigest()
    return prefixes


def verify_bundle(directory, revision, expected_digest, require_prior_slot=False):
    manifest = load_manifest(directory, revision)
    if manifest["sha256"] != expected_digest:
        raise ValueError("Fixture bundle does not match the requested digest")
    prior = manifest.get("prior_slot")
    if require_prior_slot and prior is None:
        raise ValueError("This benchmark needs a bundle containing prior-slot fixtures")
    digest = hashlib.sha256()
    keys = set()
    with (directory / "fixtures.bin").open("rb") as payload:
        current = (payload.read(RECORD_SIZE) for _ in range(MAX_RECORDS))
        if scan_records(current, PREFIX_COUNTS, keys, digest) != manifest["prefix_sha256"]:
            raise ValueError("Current-slot fixture prefix digest differs from the manifest")
        if prior:
            older = (payload.read(RECORD_SIZE) for _ in range(MAX_PRIOR_RECORDS))
            prefixes = scan_records(older, PRIOR_PREFIX_COUNTS, keys, digest, prior_slot=True)
            if prefixes != prior["prefix_sha256"]:
                raise ValueError("Prior-slot fixture prefix digest differs from the manifest")
    if digest.hexdigest() != manifest["sha256"]:
        raise ValueError("Fixture payload digest differs from the manifest")
    return manifest


def pack_fixtures(source, output, requested_revision=None, prior_source=None):
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
        version = 2 if prior_source is not None else 1
        manifest = reference_fields(revision, version)
        with (staging / "fixtures.bin").open("wb") as payload:
            current = ((source / f"key-{index:05}.bin").read_bytes() for index in range(MAX_RECORDS))
            prefixes = scan_records(current, PREFIX_COUNTS, keys, digest, output=payload)
            if prefixes != baseline_hashes:
                raise ValueError("Current-slot fixture prefix differs from the historical dataset")
            manifest["prefix_sha256"] = prefixes
            if prior_source is not None:
                older = ((prior_source / f"key-{index:05}.bin").read_bytes()
                         for index in range(MAX_PRIOR_RECORDS))
                prefixes = scan_records(older, PRIOR_PREFIX_COUNTS, keys, digest,
                                        prior_slot=True, output=payload)
                manifest["prior_slot"] = {
                    "record_count": MAX_PRIOR_RECORDS,
                    "prefix_sha256": prefixes,
                }
        manifest["sha256"] = digest.hexdigest()
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
    pack.add_argument("--prior-slot-source", type=Path, help="directory containing the prior-slot records")
    verify = actions.add_parser("verify", help="verify packed records and their manifest")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("revision")
    verify.add_argument("--sha256", help="expected full payload SHA-256")
    verify.add_argument("--require-prior-slot", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "pack":
            manifest = pack_fixtures(args.source, args.output, args.leanvm_revision, args.prior_slot_source)
        else:
            digest = args.sha256 or load_manifest(args.bundle, args.revision)["sha256"]
            manifest = verify_bundle(args.bundle, args.revision, digest, args.require_prior_slot)
        print(manifest["sha256"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Fixture bundle failed: {error}\n")


if __name__ == "__main__":
    main()
