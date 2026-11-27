"""Selection dependency and report tests; cryptographic smoke lives in scripts/."""
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

import run
import selected_cases as selected
from render_table import read_saved_run
from report import measured_rows, write_report


def records(count, slot=0):
    return [i.to_bytes(32, 'little') + slot.to_bytes(32, 'little') + bytes(2404)
            for i in range(slot * 32768, slot * 32768 + count)]


class FakeSidecar:
    def __init__(self):
        self.requests = []

    def aggregate(self, children, raw, output_claims=None):
        self.requests.append((len(children), len(raw), output_claims is not None))
        claims = {r[:64] for r in raw}
        for encoded, _ in children:
            claims.update(encoded[i:i+64] for i in range(8, 8+64*int.from_bytes(encoded[:8], 'little'), 64))
        encoded = run.u64(len(claims)) + b''.join(sorted(claims)) + run.u64(0) + b'\0'
        encoded = output_claims or encoded
        return encoded, encoded  # Deterministic test double, never used for benchmark results.

    def verify(self, pair):
        return pair[0] == pair[1]


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory / 'cases').mkdir()
        self.args = SimpleNamespace(output=self.directory)
        self.manifest = {
            'run_id': 'test', 'hardware': {'cpu_models': ['test'], 'architecture': 'test', 'os': 'test',
                'logical_cpus_visible': 1, 'effective_cpu_capacity': 1},
            'build': {'leanvm_revision': 'a'*40, 'benchmark_revision': 'b'*40},
            'settings': {'sizes': [8192], 'repetitions': 2, 'execution': 'unverified',
                'prover_threads': 1, 'log_inv_rate': 2}}

    def execute(self, name, size=8192):
        connection = FakeSidecar()
        @contextmanager
        def open_sidecar(*_):
            yield connection, 0
        def fixtures(args, manifest, count, slots_back=0):
            return records(count, slots_back), {'fixture_source': 'unit-test'}
        case = selected.case_plan([name], [size])[0]
        with patch.object(run, 'prepare_fixtures', fixtures), patch.object(run, 'open_sidecar', open_sidecar):
            row = selected.run_selected_case(self.args, self.manifest, case)
        return row, connection.requests

    def test_only_selected_work(self):
        row, calls = self.execute('committee-proof')
        self.assertEqual(calls, [(0, 128, False)] * 3)
        row, calls = self.execute('slot-merge')
        self.assertEqual(calls, [(0,128,False)]*2 + [(2,0,False)]*3)
        self.assertEqual(row['output_messages'], 1)
        for name, setup_merges, messages in [('round2', 1, 2), ('round3', 2, 3), ('prune', 3, 2)]:
            row, calls = self.execute(name)
            self.assertEqual(sum(bool(c[0]) for c in calls), setup_merges + 3)
            self.assertEqual(row['output_messages'], messages)
            self.assertTrue(row['missing_claims_rejected'])

    def test_single_committee_reuse(self):
        row, calls = self.execute('slot-merge', 64)
        self.assertTrue(row['reuse'])
        self.assertIsNone(row['operation'])
        self.assertEqual(calls, [(0,2,False)])

    def test_plan(self):
        plan = selected.case_plan(['slot-merge', 'merge-16x2048-multi-message'], [8192,64])
        self.assertEqual(len(plan), 3)
        self.assertIsNone(plan[-1]['active_validators'])
        self.assertEqual(plan, selected.case_plan(['merge-16x2048-multi-message','slot-merge'], [64,8192]))

    def test_reports_and_resume_isolation(self):
        row, _ = self.execute('slot-merge')
        row = json.loads(json.dumps(row))
        plan = selected.case_plan(['slot-merge'], [8192])
        self.manifest['settings']['selected_cases'] = plan
        self.manifest['settings']['selected_workload_version'] = selected.WORKLOAD_VERSION
        path = self.directory / 'cases' / (row['case_id'] + '.json')
        path.write_text(json.dumps(row))
        self.assertEqual(measured_rows(self.directory,self.manifest), [row])
        write_report(self.directory,self.manifest,[row])
        manifest, rows = read_saved_run(self.directory/'summary.json')
        self.assertEqual(rows,[row])
        self.assertIn('slot-merge', selected.render_selected(manifest,rows))
        self.assertEqual((self.directory/'results.md').read_text(),selected.render_selected(manifest,rows,markdown=True))
        self.assertIn('input_messages',(self.directory/'results.csv').read_text())
        row['workload_version'] = 'wrong'
        path.write_text(json.dumps(row))
        with self.assertRaises(ValueError):
            measured_rows(self.directory,self.manifest)


if __name__ == '__main__':
    unittest.main()
