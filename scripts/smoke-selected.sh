#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
image=${1:?Expected a built benchmark image}
smoke_dir=$(mktemp -d "${TMPDIR:-/tmp}/pq-selected-smoke.XXXXXX")
trap 'rm -rf "$smoke_dir"' EXIT
mkdir "$smoke_dir/results" "$smoke_dir/fixtures"
args=(--rm --network none --user "$(id -u):$(id -g)" --cpus 2 --memory 6g
      -v "$smoke_dir/results:/results" -v "$smoke_dir/fixtures:/fixtures")
docker run "${args[@]}" "$image" --list-tests | tee "$smoke_dir/list.txt"
for name in committee-proof slot-merge round2 round3 prune; do
    docker run "${args[@]}" "$image" --test "$name" --sizes 64 8192 --reps 1 \
        --execution native --output "/results/$name" > "$smoke_dir/$name.log" 2>&1 || {
        cat "$smoke_dir/$name.log"; exit 1;
    }
done
docker run "${args[@]}" "$image" --test slot-merge --sizes 64 8192 --reps 1 \
    --execution native --output /results/slot-merge --resume > "$smoke_dir/resume.log"
if docker run "${args[@]}" "$image" --test round2 --sizes 64 8192 --reps 1 \
    --execution native --output /results/slot-merge --resume; then
    echo "Accepted different selected workload on resume" >&2; exit 1
fi
python3 - "$smoke_dir/results" <<'PY'
import json
from pathlib import Path
import sys
from render_table import read_saved_run
from selected_cases import render_selected
root = Path(sys.argv[1])
for test in ('committee-proof', 'slot-merge', 'round2', 'round3', 'prune'):
    manifest, rows = read_saved_run(root / test)
    assert len(rows) == 2
    assert {r['test'] for r in rows} == {test}
    for row in rows:
        assert row['all_proofs_verified'] and row['missing_claims_rejected'] and row['wrong_message_rejected']
        expected_messages = {'committee-proof': 1, 'slot-merge': 1, 'round2': 2, 'round3': 3, 'prune': 2}[test]
        assert row['output_messages'] == expected_messages
    assert render_selected(manifest, rows, markdown=True) == (root / test / 'results.md').read_text()
    assert read_saved_run(root/test/'summary.json')[1] == rows
print('Selected real proofs, negative checks, reporting and resume isolation verified')
PY
# Small 16-message fixture pack independently verifies real signatures and grouping.
docker run "${args[@]}" --entrypoint pq-geometry-fixtures "$image" /fixtures/multi-smoke 16 2 16
docker run -i "${args[@]}" --entrypoint python3 "$image" - <<'PY'
import json
from pathlib import Path
from types import SimpleNamespace
import sys
sys.path.insert(0, '/opt/pq-bench')
import run
from environment import hardware
build = json.loads(Path('/opt/pq-bench/build.json').read_text())
variant = run.select_sidecar(build['sidecar_variants'], 'auto', set(hardware()['cpu_flags']))
args = SimpleNamespace(sidecar=variant['binary'], timeout=600)
records = [p.read_bytes() for p in sorted(Path('/fixtures/multi-smoke').glob('key-*.bin'))]
assert len(records) == len({r[:32] for r in records}) == len({r[32:64] for r in records}) == 16
assert {r[64:68] for r in records} == {(32768).to_bytes(4, 'little')}
with run.open_sidecar(args, {'prover_threads': 2}, Path('/results/multi-smoke.log')) as (connection, _):
    children = [run.prove(connection, [], [r], run.claims_for([r]))[0] for r in records]
    expected = run.claims_for(records)
    pair = run.prove(connection, children, [], expected)[0]
    run.check_wrong_message(connection, pair)
    assert not connection.verify((run.claims_for(records[:-1]), pair[1]))
print('Real 16-message merge and negative checks passed (16 disjoint one-signature inputs)')
PY
