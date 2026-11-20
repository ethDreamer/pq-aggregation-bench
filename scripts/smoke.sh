#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

image=${1:?Expected a built benchmark image}
execution=${2:-native}
variant=${3:-fixture-free}
smoke_dir=$(mktemp -d "${TMPDIR:-/tmp}/pq-bench-smoke.XXXXXX")
trap 'rm -rf "$smoke_dir"' EXIT
mkdir "$smoke_dir/results" "$smoke_dir/fixtures"
args=(
    --rm --network none --user "$(id -u):$(id -g)" --cpus 2.5 --memory 3g
    --volume "$smoke_dir/results:/results"
)
if [[ $variant == bundled ]]; then
    args+=(--read-only --tmpfs /tmp:rw,nosuid,nodev,mode=1777 --volume "$smoke_dir/fixtures:/fixtures:ro")
    read -r leanvm_revision bundle_digest < <(python3 - <<'BASELINE'
import json
from pathlib import Path

reference = json.loads(Path("baseline/results/1048576.json").read_text())
print(reference["prover_pin"], reference["fixture_sha256"])
BASELINE
)
    docker run "${args[@]}" --entrypoint python3 "$image" /opt/pq-bench/fixture_bundle.py verify \
        /opt/pq-bench/bundled-fixtures "$leanvm_revision" --sha256 "$bundle_digest"
else
    args+=(--volume "$smoke_dir/fixtures:/fixtures")
fi
run=(--execution "$execution" --sizes 64 8192 --reps 1)
docker run "${args[@]}" "$image" "${run[@]}" 2>&1 | tee "$smoke_dir/run.log"
if [[ $variant == bundled ]]; then
    grep -q "Loading 256 pre-generated fixtures" "$smoke_dir/run.log"
    if grep -Eq "Generating|Generated" "$smoke_dir/run.log" || [[ -n $(ls -A "$smoke_dir/fixtures") ]]; then
        echo "Bundled run generated or wrote fixture files" >&2
        exit 1
    fi
fi
docker run "${args[@]}" "$image" "${run[@]}" --resume | tee "$smoke_dir/resume.log"
if docker run "${args[@]}" "$image" "${run[@]}" --threads 1 --resume; then
    echo "Accepted a run with changed prover thread count" >&2
    exit 1
fi

python3 - "$smoke_dir/results/summary.json" "$execution" "$variant" "$smoke_dir" <<'PY'
import csv
import json
from pathlib import Path
from math import sqrt
import subprocess
import sys

from report import render_mean_stdev, timing_statistics, warmed_timings

assert timing_statistics({"samples_ms": [1, 3]}) == (2, sqrt(2), 2)
assert timing_statistics({"samples_ms": []}) == (None, None, 0)

summary_path = Path(sys.argv[1])
summary = json.loads(summary_path.read_text())
build, hardware, settings = (summary["run"][key] for key in ("build", "hardware", "settings"))
assert hardware["cpu_quota_cores"] == 2.5
assert hardware["memory_cgroup_limit_bytes"] == 3 * 2**30
assert settings["execution"] == sys.argv[2]
assert settings["repetitions"] == 1
bundled = sys.argv[3] == "bundled"
assert bool(build["fixture_bundle"]) == bundled
if bundled:
    assert build["fixture_bundle"]["record_count"] == 32768
single, merge = summary["cases"]
assert single["committees_per_slot"] == 1 and single["proposer_merge"] is None
assert merge["committees_per_slot"] == 2 and merge["proposer_merge"] is not None
assert merge["merged_wrong_message_rejected"]
for row in (single, merge):
    assert row["all_proofs_verified"] and row["wrong_message_rejected"]
    assert len(row["committee_prove"]["samples_ms"]) == 1
    if bundled:
        assert row["fixture_source"] == "bundle" and row["fixture_workers_used"] == 0
    if build["leanvm_revision"] == "283686575c4e81790591d2c6e3ed21c69c0e3bbe":
        original = json.loads(Path(f"baseline/results/{row['active_validators']}.json").read_text())
        assert row["fixture_sha256"] == original["fixture_sha256"]
with summary_path.with_name("results-mean-stdev.csv").open(newline="") as file:
    single_report, merge_report = csv.DictReader(file)
assert single_report["merge_operations"] == "0" and single_report["proposer_merge_n"] == "0"
assert single_report["proposer_merge_mean_ms"] == single_report["proposer_merge_sample_stdev_ms"] == ""
for row, raw in zip((single_report, merge_report), summary["cases"]):
    assert row["committee_prove_n"] == "1" and row["committee_prove_sample_stdev_ms"] == ""
    assert float(row["committee_prove_mean_ms"]) == raw["committee_prove"]["samples_ms"][0]
    assert row["committee_verify_n"] == row["final_verify_n"] == "1"
assert merge_report["proposer_merge_n"] == "1" and merge_report["proposer_merge_sample_stdev_ms"] == ""
table = summary_path.with_name("results-mean-stdev.md").read_text()
command = [sys.executable, "render_table.py", str(summary_path)]
markdown = subprocess.check_output([*command, "--markdown"], text=True)
terminal = subprocess.check_output(command, text=True)
assert table == markdown and "| 64 |" in markdown
assert "Reuse" in terminal and "Validators" in terminal and "|" not in terminal
assert "Effective CPU allocation 2.5 CPU equivalents" in terminal
assert "Container memory limit 3.0 GiB" in terminal
narrow = render_mean_stdev(summary["run"], warmed_timings(summary["cases"]), width=58)
assert max(map(len, narrow.splitlines())) <= 58
for column in ("Comm/slot", "Sigs/comm", "Sigs/slot", "Merges", "Prove one (ms)",
               "Merge (ms)", "Verify (ms)", "Comm verify (ms)", "Final verify n"):
    assert column in narrow
for log in ("run.log", "resume.log"):
    assert (Path(sys.argv[4]) / log).read_text().endswith(terminal)
print("Verified proofs, claim unions, negative checks, runtime limits, resume and timing reports")
PY
