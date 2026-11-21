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
sizes=(64 8192)
if [[ $variant == bundled ]]; then
    sizes+=(65536)
    args+=(--read-only --tmpfs /tmp:rw,nosuid,nodev,mode=1777 --volume "$smoke_dir/fixtures:/fixtures:ro")
    leanvm_revision=$(docker run "${args[@]}" --entrypoint python3 "$image" -c \
        'import json; print(json.load(open("/opt/pq-bench/build.json"))["leanvm_revision"])')
    bundle_digest=$(docker run "${args[@]}" --entrypoint python3 "$image" -c \
        'import json; print(json.load(open("/opt/pq-bench/bundled-fixtures/manifest.json"))["sha256"])')
    docker run "${args[@]}" --entrypoint python3 "$image" /opt/pq-bench/fixture_bundle.py verify \
        /opt/pq-bench/bundled-fixtures "$leanvm_revision" --sha256 "$bundle_digest" --require-round3
    docker run "${args[@]}" --entrypoint pq-geometry-fixtures "$image" --verify-pack \
        /opt/pq-bench/bundled-fixtures/fixtures.bin 2
else
    args+=(--volume "$smoke_dir/fixtures:/fixtures")
fi
run=(--execution "$execution" --sizes "${sizes[@]}" --reps 1)
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

from fixture_bundle import COMPATIBLE_REVISIONS, fresh_committee_count
from report import render_mean_stdev, timing_statistics, warmed_timings
from run import select_sidecar

assert timing_statistics({"samples_ms": [1, 3]}) == (2, sqrt(2), 2)
assert timing_statistics({"samples_ms": []}) == (None, None, 0)

summary_path = Path(sys.argv[1])
summary = json.loads(summary_path.read_text())
build, hardware, settings = (summary["run"][key] for key in ("build", "hardware", "settings"))
assert hardware["cpu_quota_cores"] == 2.5
assert hardware["memory_cgroup_limit_bytes"] == 3 * 2**30
assert settings["execution"] == sys.argv[2]
assert settings["repetitions"] == 1
assert settings["log_inv_rate"] == 2
assert build["sidecar_wire_version"] == 1 and build["sidecar_verifier"] == "leanvm-v1"
if build["leanvm_revision"] == "c5ee9d239e42d0bc8113eb4aee3e2cae5faf1764":
    assert build["leanvm_bindings_version"] == "0.15.0"
variants = build["sidecar_variants"]
selected = select_sidecar(variants, "auto", set(hardware["cpu_flags"]))
assert selected["profile"] == build["sidecar_cpu_profile"]
assert selected["sidecar_sha256"] == build["sidecar_sha256"]
assert selected["rustflags"] == build["rustflags"]
if build["target_platform"] == "linux/amd64":
    assert select_sidecar(variants, "auto", set())["profile"] == "generic"
    for profile in ("portable", "avx2", "avx2-gfni", "avx512"):
        candidate, = (item for item in variants if item["profile"] == profile)
        flags = set(candidate["required_linux_cpu_flags"])
        assert select_sidecar(variants, "auto", flags)["profile"] == profile
    try:
        select_sidecar(variants, "avx512", set())
    except ValueError as error:
        assert "requires Linux CPU flags" in str(error)
    else:
        raise AssertionError("accepted an unsupported forced profile")
bundled = sys.argv[3] == "bundled"
assert bool(build["fixture_bundle"]) == bundled
if bundled:
    assert build["fixture_bundle"]["record_count"] == 32768
    assert build["fixture_bundle"]["prior_slot"]["record_count"] == 30720
    assert build["fixture_bundle"]["second_prior_slot"]["record_count"] == 30720
single, merge, *saturated = summary["cases"]
if bundled:
    saturated_case, = saturated
    assert saturated_case["committees_per_slot"] == saturated_case["improvement_fan_in"] == 16
    assert saturated_case["improvement_fresh_proofs"] == 15
assert single["committees_per_slot"] == 1 and single["proposer_merge"] is None
assert merge["committees_per_slot"] == 2 and merge["proposer_merge"] is not None
assert merge["merged_wrong_message_rejected"]
assert single["paired_first_merge"] is single["two_round_merge"] is None
assert single["first_two_round_merge_ms"] is None
assert single["merge_sequence_requests"] == 2
assert merge["merge_sequence_requests"] == 3
for row in summary["cases"]:
    fresh = fresh_committee_count(row["committees_per_slot"])
    assert row["improvement_fresh_proofs"] == fresh
    assert row["improvement_fan_in"] == fresh + 1
    assert row["improvement_added_claims"] == fresh * row["signatures_per_committee"]
    assert row["improvement_total_claims"] == row["signatures_per_slot"] + row["improvement_added_claims"]
    assert row["improvement_message_groups"] == 2
    assert row["round3_fresh_proofs"] == fresh and row["round3_fan_in"] == fresh + 1
    assert row["round3_added_claims"] == row["improvement_added_claims"]
    assert row["round3_total_claims"] == row["improvement_total_claims"] + row["round3_added_claims"]
    assert row["round3_message_groups"] == 3
    assert row["round3_wrong_message_rejected"] and row["round3_incomplete_claims_rejected"]
    third, = row["round3_merge"]["samples_ms"]
    sequence, = row["merge_sequence"]["samples_ms"]
    assert len(row["round3_verify"]["samples_ms"]) == 1
    assert row["merge_sequence_requests"] == 2 + row["merge_operations"]
    assert row["improved_wrong_message_rejected"] and row["improved_incomplete_claims_rejected"]
    assert len(row["proposer_improvement"]["samples_ms"]) == len(row["improved_verify"]["samples_ms"]) == 1
    if row["committees_per_slot"] > 1:
        first, = row["paired_first_merge"]["samples_ms"]
        improvement, = row["proposer_improvement"]["samples_ms"]
        total, = row["two_round_merge"]["samples_ms"]
        assert total >= first + improvement
        assert sequence >= total + third
        assert row["first_two_round_merge_ms"] >= row["first_paired_merge_ms"] + row["first_improvement_ms"]
        assert row["first_merge_sequence_ms"] >= row["first_two_round_merge_ms"] + row["first_round3_merge_ms"]
    else:
        improvement, = row["proposer_improvement"]["samples_ms"]
        assert sequence >= improvement + third
    assert row["all_proofs_verified"] and row["wrong_message_rejected"]
    assert len(row["committee_prove"]["samples_ms"]) == 1
    if bundled:
        assert row["fixture_source"] == "bundle" and row["fixture_workers_used"] == 0
        for slot in ("prior_slot_fixture", "second_prior_slot_fixture"):
            prior = row[slot]
            assert prior["fixture_source"] == "bundle" and prior["fixture_workers_used"] == 0
    original = json.loads(Path(f"baseline/results/{row['active_validators']}.json").read_text())
    if build["leanvm_revision"] == original["prover_pin"] or build["leanvm_revision"] in COMPATIBLE_REVISIONS:
        assert row["fixture_sha256"] == original["fixture_sha256"]
with summary_path.with_name("results-mean-stdev.csv").open(newline="") as file:
    single_report, merge_report, *saturated_reports = csv.DictReader(file)
assert single_report["merge_operations"] == "0" and single_report["proposer_merge_n"] == "0"
assert single_report["proposer_merge_mean_ms"] == single_report["proposer_merge_sample_stdev_ms"] == ""
assert single_report["two_round_merge_n"] == "0" and single_report["two_round_merge_mean_ms"] == ""
for row, raw in zip((single_report, merge_report, *saturated_reports), summary["cases"]):
    assert row["proposer_improvement_n"] == row["improved_verify_n"] == "1"
    assert row["round3_merge_n"] == row["round3_verify_n"] == row["merge_sequence_n"] == "1"
    assert int(row["merge_sequence_requests"]) == raw["merge_sequence_requests"]
    assert float(row["merge_sequence_mean_ms"]) == raw["merge_sequence"]["samples_ms"][0]
    if raw["merge_operations"]:
        assert row["paired_first_merge_n"] == row["two_round_merge_n"] == "1"
        assert float(row["two_round_merge_mean_ms"]) == raw["two_round_merge"]["samples_ms"][0]
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
               "Round 1 (ms)", "Round 2 (ms)", "Round 3 (ms)", "Two rounds (ms)",
               "Full sequence (ms)", "Verify orig. (ms)", "Verify R3 (ms)",
               "Comm verify (ms)", "Final verify n", "Fresh proofs", "New claims"):
    assert column in narrow
assert "Slot geometry" in narrow and "Timings by validator count" in narrow
wide_header = next(line for line in terminal.splitlines() if "Comm/slot" in line)
for column in ("Prove one (ms)", "Round 1 (ms)", "Round 2 (ms)", "Round 3 (ms)",
               "Two rounds (ms)", "Full sequence (ms)", "Verify orig. (ms)", "Verify R3 (ms)"):
    assert column in wide_header
markdown_header = next(line for line in markdown.splitlines() if "Committees / slot" in line)
gfm_rows = [line for line in markdown.splitlines() if line.startswith("| ")][:2 + len(summary["cases"])]
assert all(line.count("|") == markdown_header.count("|") for line in gfm_rows)
assert "Round 3 merge (ms)" in markdown_header and "Full sequence (ms)" in markdown_header
for log in ("run.log", "resume.log"):
    assert (Path(sys.argv[4]) / log).read_text().endswith(terminal)
print("Verified proofs, claim unions, negative checks, runtime limits, resume and timing reports")
PY

if [[ ${COMPARE_CPU_PROFILES:-0} == 1 ]]; then
    profiles=$(docker run "${args[@]}" --entrypoint python3 "$image" -c '
import json
import sys
sys.path.insert(0, "/opt/pq-bench")
from environment import hardware
info = json.load(open("/opt/pq-bench/build.json"))
flags = set(hardware()["cpu_flags"])
for candidate in info["sidecar_variants"]:
    if set(candidate["required_linux_cpu_flags"]) <= flags:
        print(candidate["profile"])
')
    for profile in $profiles; do
        sizes=(--sizes 8192 --reps 3)
        if [[ $profile == generic ]]; then
            sizes=(--sizes 64 --reps 1)
        fi
        docker run "${args[@]}" "$image" --execution native --cpu-profile "$profile" \
            "${sizes[@]}" --output "/results/profile-$profile" > "$smoke_dir/profile-$profile.log" 2>&1 || {
            cat "$smoke_dir/profile-$profile.log"
            exit 1
        }
    done
    if [[ $profiles == *generic* && $profiles == *portable* ]]; then
        if docker run "${args[@]}" "$image" --execution native --cpu-profile portable \
            --sizes 64 --reps 1 --output /results/profile-generic --resume; then
            echo "Accepted a resume with a different CPU profile" >&2
            exit 1
        fi
    fi
    python3 - "$smoke_dir/results" <<'PY'
import json
from pathlib import Path
import sys

print("Matched native CPU-profile smoke (8,192 validators, three warmed reps, milliseconds)")
reference = None
for summary in sorted(Path(sys.argv[1]).glob("profile-*/summary.json")):
    run = json.loads(summary.read_text())
    case, = run["cases"]
    build = run["run"]["build"]
    profile = build["sidecar_cpu_profile"]
    assert case["all_proofs_verified"] and case["wrong_message_rejected"]
    if case["active_validators"] == 64:
        assert profile == "generic"
        print("Generic fallback verified a single committee")
        continue
    assert case["merged_wrong_message_rejected"] and case["committees_per_slot"] == 2
    identity = (
        run["run"]["hardware"], build["leanvm_revision"], case["fixture_sha256"],
        run["run"]["settings"]["prover_threads"], run["run"]["settings"]["log_inv_rate"],
    )
    if reference is None:
        reference = identity
        print(", ".join(identity[0]["cpu_models"]), "prover threads", identity[3])
    assert reference == identity
    print(
        profile,
        f"prove median {case['committee_prove']['median_ms']:.1f}",
        f"merge median {case['proposer_merge']['median_ms']:.1f}",
        f"verify median {case['final_verify']['median_ms']:.1f}",
    )
PY
fi
