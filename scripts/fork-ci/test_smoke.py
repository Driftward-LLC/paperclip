import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]


class ImageSmokeTest(unittest.TestCase):
    def run_smoke(self, running='true', ui='<!doctype html>'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / 'calls'
            commands = {
                'docker': '''echo "$*" >> "$CALLS"
case "$1" in
  run)
    [ "${#BETTER_AUTH_SECRET}" = 64 ] || exit 1
    case "$BETTER_AUTH_SECRET" in *[!0-9a-f]*) exit 1;; esac
    case " $* " in *" -e BETTER_AUTH_SECRET "*) ;; *) exit 1;; esac
    echo auth-secret-present >> "$CALLS";;
  port) echo '127.0.0.1:54321';;
  inspect) echo "$RUNNING";;
esac
''',
                'curl': '''case "$*" in
  *api/health*) [ "$RUNNING" = true ] && echo '{"status":"ok"}' || exit 22;;
  *) echo "$UI";;
esac
''',
                'sleep': 'exit 0\n',
            }
            for name, body in commands.items():
                file = root / name
                file.write_text('#!/bin/sh\n' + body)
                file.chmod(0o755)
            env = dict(os.environ, PATH=f'{root}:{os.environ["PATH"]}', CALLS=str(log), RUNNING=running, UI=ui)
            result = subprocess.run(['bash', str(ROOT / 'scripts/fork-ci/image-smoke.sh'), 'test-image'], env=env, capture_output=True, text=True)
            calls = log.read_text()
            self.assertIn('rm -fv fork-image-smoke-', calls)
            self.assertIn('auth-secret-present', calls)
            self.assertIn('-e PAPERCLIP_DEPLOYMENT_MODE=authenticated', calls)
            self.assertNotIn('BETTER_AUTH_SECRET=', calls)
            self.assertIn('-v /paperclip', calls)
            self.assertIn('-p 127.0.0.1::3100', calls)
            return result

    def test_ready_image_and_ui(self):
        self.assertEqual(self.run_smoke().returncode, 0)

    def test_exited_container_fails_and_cleans_up(self):
        self.assertNotEqual(self.run_smoke(running='false').returncode, 0)

    def test_missing_ui_fails_and_cleans_up(self):
        self.assertNotEqual(self.run_smoke(ui='missing').returncode, 0)

    def test_removed_upstream_workflows_stay_absent_and_fork_workflows_remain(self):
        workflows = ROOT / '.github/workflows'
        for name in ['release.yml', 'refresh-lockfile.yml', 'cloud-migrator-artifacts.yml',
                     'docker.yml', 'cloud-readiness.yml']:
            self.assertFalse((workflows / name).exists(), name)
        for name in ['fork-ci.yml', 'fork-release.yml']:
            self.assertTrue((workflows / name).is_file(), name)

    def test_inherited_jobs_are_upstream_only(self):
        for file in (ROOT / '.github/workflows').glob('*.yml'):
            if file.name.startswith('fork-'):
                continue
            workflow = yaml.safe_load(file.read_text())
            for name, job in workflow['jobs'].items():
                condition = job['if'].strip().removeprefix('${{').strip()
                self.assertTrue(condition.startswith("github.repository == 'paperclipai/paperclip'"), f'{file.name}: {name}')

    def test_profile_condition_preserves_hash_in_title(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/ci-profile.yml').read_text())
        self.assertTrue(workflow['jobs']['export']['if'].endswith("'CI profile · PR #')) }}"))
