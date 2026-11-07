import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/fork-ci' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class MatrixTest(unittest.TestCase):
    def test_every_canonical_partition_is_planned_once(self):
        canonical = json.loads((ROOT / '.github/ci-task-matrix.json').read_text())
        rows = module('test-plan').plan(canonical)['include']
        expected = []
        for key, mode in [('general_tests', 'general'), ('verify_serialized_server', 'serialized')]:
            for row in canonical[key]:
                args = ['--mode', mode]
                if mode == 'general':
                    args += ['--group', row['group']]
                if 'shard_count' in row:
                    args += ['--shard-index', str(row['shard_index']), '--shard-count', str(row['shard_count'])]
                expected.append(args)
        self.assertEqual([row['args'] for row in rows], expected)
        self.assertEqual(len({tuple(row['args']) for row in rows}), len(expected))
        # Independently require every index in each upstream shard family.
        for key in ['general_tests', 'verify_serialized_server']:
            groups = {}
            for row in canonical[key]:
                if 'shard_count' in row:
                    groups.setdefault((row.get('group'), row['shard_count']), []).append(row['shard_index'])
            for (_, count), indices in groups.items():
                self.assertEqual(sorted(indices), list(range(count)))

    def test_aggregate_fails_for_every_non_success_and_missing_job(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/fork-ci.yml').read_text())
        aggregate = workflow['jobs']['validation']
        self.assertEqual(aggregate['name'], 'Fork validation')
        self.assertEqual(aggregate['needs'], ['prepare', 'tests'])
        self.assertIn('always()', aggregate['if'])
        source = aggregate['steps'][0]['run'].split("python3 - <<'PYTHON'\n")[1].rsplit('\nPYTHON', 1)[0]
        for status in ['success', 'failure', 'cancelled', 'skipped', 'timed_out']:
            for job in ['prepare', 'tests']:
                results = {name: {'result': 'success'} for name in ['prepare', 'tests']}
                results[job]['result'] = status
                with patch.dict(os.environ, RESULTS=json.dumps(results)):
                    if status == 'success':
                        exec(source, {})
                    else:
                        with self.assertRaises(AssertionError):
                            exec(source, {})
        with patch.dict(os.environ, RESULTS='{}'):
            with self.assertRaises(AssertionError):
                exec(source, {})
        tests = workflow['jobs']['tests']
        self.assertEqual(tests['strategy']['max-parallel'], 6)
        self.assertFalse(tests['strategy']['fail-fast'])
        self.assertEqual(workflow['name'], 'Fork CI')
        self.assertEqual(tests['strategy']['matrix'], '${{ fromJSON(needs.prepare.outputs.matrix) }}')

    def test_artifact_contains_only_package_outputs_and_native_fixtures(self):
        artifact = module('build-artifact')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ['packages/shared/dist/index.js', 'node_modules/secret', 'data/token', '.local/auth',
                         'packages/paperclip-runner/runner/target/release/paperclip-runnerd']:
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('fixture')
            fixtures = ['paperclip-runnerd', 'conformance-tracer', 'fake-harness', 'fake-codex-app-server', 'fake-acpx-sidecar']
            for name in fixtures:
                file = root / 'packages/paperclip-runner/runner/target/debug' / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('fixture')
            with patch.object(artifact.subprocess, 'check_output', return_value=b'packages/shared/package.json\0'):
                paths = list(artifact.outputs(root))
            self.assertEqual(len(paths), 7)
            self.assertIn(Path('packages/shared/dist'), paths)
            self.assertFalse(any(str(path).startswith(('node_modules', 'data', '.local')) for path in paths))

    def test_fork_server_partition_retains_native_suites(self):
        args = ['node', 'scripts/run-vitest-stable.mjs', '--mode', 'general', '--group',
                'general-server-without-chat', '--shard-index', '0', '--shard-count', '1', '--dry-run']
        result = subprocess.run(args, cwd=ROOT, env=dict(os.environ, GITHUB_WORKFLOW='Fork CI'), capture_output=True, text=True, check=True)
        plan = json.loads(result.stdout)
        self.assertIn('server/src/services/native-runtime/native-codex-runner.integration.test.ts', plan['selectedGeneralServerSuites'])
