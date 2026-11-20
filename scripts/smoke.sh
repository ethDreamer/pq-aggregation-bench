#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

image=${1:?Expected a built benchmark image}
execution=${2:-native}
smoke_dir=$(mktemp -d "${TMPDIR:-/tmp}/pq-bench-smoke.XXXXXX")
trap 'rm -rf "$smoke_dir"' EXIT
mkdir "$smoke_dir/results" "$smoke_dir/fixtures"
args=(
    --rm --network none --user "$(id -u):$(id -g)" --cpus 2.5 --memory 3g
    --volume "$smoke_dir/results:/results" --volume "$smoke_dir/fixtures:/fixtures"
)
run=(--execution "$execution" --sizes 64 8192 --reps 1)
docker run "${args[@]}" "$image" "${run[@]}"
docker run "${args[@]}" "$image" "${run[@]}" --resume
if docker run "${args[@]}" "$image" "${run[@]}" --threads 1 --resume; then
    echo "Accepted a run with changed prover thread count" >&2
    exit 1
fi

python3 - "$smoke_dir/results/summary.json" "$execution" <<'PY'
import json
from pathlib import Path
import sys

summary = json.loads(Path(sys.argv[1]).read_text())
build, hardware, settings = (summary["run"][key] for key in ("build", "hardware", "settings"))
assert hardware["cpu_quota_cores"] == 2.5
assert hardware["memory_cgroup_limit_bytes"] == 3 * 2**30
assert settings["execution"] == sys.argv[2]
assert settings["repetitions"] == 1
single, merge = summary["cases"]
assert single["committees_per_slot"] == 1 and single["proposer_merge"] is None
assert merge["committees_per_slot"] == 2 and merge["proposer_merge"] is not None
assert merge["merged_wrong_message_rejected"]
for row in (single, merge):
    assert row["all_proofs_verified"] and row["wrong_message_rejected"]
    assert len(row["committee_prove"]["samples_ms"]) == 1
    if build["leanvm_revision"] == "283686575c4e81790591d2c6e3ed21c69c0e3bbe":
        original = json.loads(Path(f"baseline/results/{row['active_validators']}.json").read_text())
        assert row["fixture_sha256"] == original["fixture_sha256"]
print("Verified proofs, claim unions, negative checks, runtime limits and resume")
PY
