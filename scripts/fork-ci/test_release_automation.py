import copy
import datetime
import importlib.util
import io
import json
import os
import runpy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
import yaml

spec = importlib.util.spec_from_file_location('automation', Path(__file__).with_name('release-automation.py'))
automation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(automation)
ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'a' * 40
OTHER = 'b' * 40
DIGEST = 'sha256:' + 'c' * 64
TODAY = datetime.datetime(2026, 11, 10, tzinfo=datetime.timezone.utc)
TAG = 'driftward-v2026.1110.0'


class GitHub:
    def __init__(self):
        self.ci = {'id': 17, 'run_attempt': 2, 'head_sha': SOURCE, 'name': 'CI',
                   'path': '.github/workflows/fork-ci.yml', 'event': 'push', 'head_branch': 'master',
                   'status': 'completed', 'conclusion': 'success',
                   'repository': {'full_name': automation.REPOSITORY},
                   'head_repository': {'full_name': automation.REPOSITORY}}
        self.jobs = [{'name': 'Validation', 'status': 'completed', 'conclusion': 'success'}]
        self.tags = []
        self.refs = {}
        self.runs = []
        self.release = None
        self.releases = []
        self.calls = []
        self.master = [SOURCE]
        self.tag_error = False
        self.dispatch_error = False

    def event(self):
        return {'workflow_run': copy.deepcopy(self.ci)}

    def existing_tag(self, name=TAG, sha=SOURCE):
        self.tags.append({'name': name, 'commit': {'sha': sha}})
        self.refs[name] = {'ref': 'refs/tags/' + name, 'object': {'type': 'commit', 'sha': sha}}

    def existing_run(self, status='queued', conclusion=None):
        self.runs.append({'id': 18, 'head_sha': SOURCE, 'display_title': 'Docker release / ' + TAG,
                          'path': '.github/workflows/fork-release.yml', 'event': 'workflow_dispatch',
                          'repository': {'full_name': automation.REPOSITORY}, 'status': status, 'conclusion': conclusion})

    def __call__(self, method, path, data=None, missing=False):
        self.calls.append((method, path, data))
        parts = urlsplit(path)
        query = parse_qs(parts.query)
        page = int(query.get('page', ['1'])[0])
        def batch(items): return copy.deepcopy(items[(page - 1) * 100:page * 100])
        if method == 'GET':
            if parts.path == '/actions/runs/17': return copy.deepcopy(self.ci)
            if parts.path == '/actions/runs/17/attempts/2/jobs': return {'total_count': len(self.jobs), 'jobs': batch(self.jobs)}
            if parts.path == '/git/ref/heads/master':
                sha = self.master.pop(0) if len(self.master) > 1 else self.master[0]
                return {'object': {'sha': sha}}
            if parts.path == '/tags': return batch(self.tags)
            if parts.path == '/releases': return batch(self.releases)
            if parts.path.startswith('/git/ref/tags/'):
                assert missing
                return copy.deepcopy(self.refs.get(unquote(parts.path.removeprefix('/git/ref/tags/'))))
            if parts.path == '/actions/workflows/fork-release.yml/runs':
                assert query['head_sha'] == [SOURCE]
                return {'total_count': len(self.runs), 'workflow_runs': batch(self.runs)}
            if parts.path.startswith('/releases/tags/'):
                assert missing
                return copy.deepcopy(self.release)
        if method == 'POST':
            if parts.path == '/git/refs':
                name = data['ref'].removeprefix('refs/tags/')
                assert name not in self.refs
                self.existing_tag(name, data['sha'])
                if self.tag_error: raise RuntimeError('Ambiguous tag write')
                return self.refs[name]
            if parts.path.endswith('/dispatches'):
                if self.dispatch_error: raise RuntimeError('Dispatch unavailable')
                return None
        raise AssertionError((method, path, data))

    def writes(self):
        return [call for call in self.calls if call[0] != 'GET']


class AutomaticReleaseTest(unittest.TestCase):
    def start(self, github, event=None, now=TODAY):
        return automation.start_release(event or github.event(), github, now)

    def test_first_release_creates_exact_source_tag_then_explicit_dispatch(self):
        github = GitHub()
        self.assertIn(TAG, self.start(github))
        self.assertEqual(github.writes(), [
            ('POST', '/git/refs', {'ref': 'refs/tags/' + TAG, 'sha': SOURCE}),
            ('POST', '/actions/workflows/fork-release.yml/dispatches', {'ref': TAG, 'inputs': {'tag': TAG}})])

    def test_pagination_and_date_allocation_count_existing_tags_even_without_releases(self):
        github = GitHub()
        for index in range(100): github.existing_tag('old-' + str(index), OTHER)
        github.existing_tag('driftward-v2026.1110.8', OTHER)
        github.existing_tag('driftward-v2026.1109.99', OTHER)
        self.assertIn('driftward-v2026.1110.9', self.start(github))
        self.assertTrue(any('page=2' in path for _, path, _ in github.calls))
        january = GitHub()
        self.assertIn('driftward-v2026.107.0', self.start(january, now=datetime.datetime(2026, 1, 7, tzinfo=datetime.timezone.utc)))

    def test_retry_reuses_same_sha_tag_even_on_a_different_day(self):
        github = GitHub()
        github.dispatch_error = True
        with self.assertRaisesRegex(RuntimeError, 'Dispatch unavailable'): self.start(github)
        github.dispatch_error = False
        self.assertIn(TAG, self.start(github, now=TODAY + datetime.timedelta(days=1)))
        self.assertEqual(sum(path == '/git/refs' for _, path, _ in github.writes()), 1)

    def test_ambiguous_successful_tag_write_is_read_back_before_dispatch(self):
        github = GitHub()
        github.tag_error = True
        self.assertIn(TAG, self.start(github))
        self.assertEqual(len(github.refs), 1)
        self.assertEqual(github.writes()[-1][1], '/actions/workflows/fork-release.yml/dispatches')

    def test_tag_write_failure_never_moves_or_replaces_another_target(self):
        github = GitHub()
        def request(method, path, data=None, **kwargs):
            if method == 'POST' and path == '/git/refs':
                github.existing_tag(TAG, OTHER)
                raise RuntimeError('Tag occupied')
            return github(method, path, data, **kwargs)
        with self.assertRaisesRegex(RuntimeError, 'Tag occupied'):
            automation.start_release(github.event(), request, TODAY)
        self.assertEqual(github.refs[TAG]['object']['sha'], OTHER)
        self.assertEqual(github.writes(), [])

    def test_queued_running_and_successful_dispatches_are_not_duplicated(self):
        for status, conclusion in [('queued', None), ('in_progress', None), ('completed', 'success')]:
            with self.subTest(status=status):
                github = GitHub()
                github.existing_tag()
                github.existing_run(status, conclusion)
                self.assertIn('already started', self.start(github))
                self.assertEqual(github.writes(), [])

    def test_ambiguous_dispatch_response_is_recovered_from_the_observable_run(self):
        github = GitHub()
        def ambiguous(method, path, data=None, **kwargs):
            if method == 'POST' and path.endswith('/dispatches'):
                github.calls.append((method, path, data))
                github.existing_run()
                raise RuntimeError('Dispatch response lost')
            return github(method, path, data, **kwargs)
        with self.assertRaisesRegex(RuntimeError, 'response lost'):
            automation.start_release(github.event(), ambiguous, TODAY)
        self.assertIn('already started', self.start(github))
        self.assertEqual(sum(path.endswith('/dispatches') for _, path, _ in github.writes()), 1)
        self.assertEqual(len(github.refs), 1)

    def test_existing_public_manual_release_does_not_skip_image_publication(self):
        github = GitHub()
        github.existing_tag()
        github.release = {'tag_name': TAG, 'draft': False, 'body': 'Human release notes'}
        self.assertIn('Started release', self.start(github))
        self.assertEqual([path for _, path, _ in github.writes()], ['/actions/workflows/fork-release.yml/dispatches'])

    def test_annotated_manual_tags_resolve_to_the_commit_and_tag_cycles_fail_closed(self):
        github = GitHub()
        github.existing_tag()
        github.refs[TAG]['object'] = {'type': 'tag', 'sha': OTHER}
        def annotated(method, path, data=None, **kwargs):
            if path == '/git/tags/' + OTHER:
                return {'object': {'type': 'commit', 'sha': SOURCE}}
            return github(method, path, data, **kwargs)
        self.assertIn('Started release', automation.start_release(github.event(), annotated, TODAY))
        def cyclic(method, path, **kwargs):
            if path == '/git/tags/' + OTHER:
                return {'object': {'type': 'tag', 'sha': OTHER}}
            return github(method, path, **kwargs)
        with self.assertRaisesRegex(ValueError, 'Invalid release tag target'):
            automation.tag_source(TAG, cyclic)

    def test_failed_and_cancelled_attempts_require_recovery(self):
        for conclusion in ['failure', 'cancelled', 'timed_out']:
            github = GitHub()
            github.existing_tag()
            github.existing_run('completed', conclusion)
            with self.assertRaisesRegex(ValueError, 'rerun its failed jobs'): self.start(github)
            self.assertEqual(github.writes(), [])

    def test_wrong_dispatch_identity_and_incomplete_inventory_fail_closed(self):
        for field, value in [('head_sha', OTHER), ('path', '.github/workflows/other.yml'), ('status', 'unknown'), ('event', 'pull_request')]:
            github = GitHub()
            github.existing_tag()
            github.existing_run()
            github.runs[0][field] = value
            with self.assertRaisesRegex(ValueError, 'identity'): self.start(github)
            self.assertEqual(github.writes(), [])
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            automation.pages('/jobs', lambda *args: {'total_count': 2, 'jobs': [{}]}, 'jobs')
        with self.assertRaisesRegex(ValueError, 'pagination'):
            automation.pages('/tags', lambda *args: {})

    def test_stale_master_is_skipped_before_creation_or_dispatch(self):
        for masters, writes in [([OTHER], 0), ([SOURCE, OTHER], 0), ([SOURCE, SOURCE, OTHER], 1)]:
            github = GitHub()
            github.master = masters
            self.assertIn('Skipped superseded', self.start(github))
            self.assertEqual(len(github.writes()), writes)
            self.assertFalse(any(path.endswith('/dispatches') for _, path, _ in github.writes()))

    def test_invalid_ci_events_and_validation_results_never_write(self):
        for field, value in [('event', 'pull_request'), ('head_branch', 'topic'), ('conclusion', 'failure'),
                             ('status', 'in_progress'), ('path', '.github/workflows/other.yml'), ('name', 'Other'),
                             ('repository', {'full_name': 'someone/paperclip'}), ('head_repository', {'full_name': 'someone/paperclip'}),
                             ('run_attempt', 3), ('head_sha', 'master')]:
            github = GitHub()
            event = github.event()
            event['workflow_run'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.start(github, event)
            self.assertEqual(github.writes(), [])
        for jobs in [[], [{'name': 'Validation', 'status': 'completed', 'conclusion': 'failure'}],
                     [{'name': 'Validation', 'status': 'completed', 'conclusion': 'success'}] * 2]:
            github = GitHub()
            github.jobs = jobs
            with self.assertRaisesRegex(ValueError, 'Validation'): self.start(github)
            self.assertEqual(github.writes(), [])

    def test_api_errors_and_multiple_source_tags_never_dispatch(self):
        github = GitHub()
        for name in [TAG, 'driftward-v2026.1110.1']: github.existing_tag(name)
        with self.assertRaisesRegex(ValueError, 'Multiple'): self.start(github)
        self.assertEqual(github.writes(), [])
        github = GitHub()
        def denied(method, path, data=None, **kwargs):
            if path.startswith('/tags'): raise RuntimeError('HTTP 403')
            return github(method, path, data, **kwargs)
        with self.assertRaisesRegex(RuntimeError, '403'): automation.start_release(github.event(), denied, TODAY)
        self.assertEqual(github.writes(), [])


class ReleaseMetadataTest(unittest.TestCase):
    def test_preflight_is_read_only_for_new_and_existing_public_releases(self):
        for release in [None, {'tag_name': TAG, 'draft': False, 'body': 'Human release notes'}]:
            github = GitHub()
            github.existing_tag()
            github.release = copy.deepcopy(release)
            for _ in range(2):
                self.assertFalse(automation.verify_metadata(SOURCE, 'v2026.1110.0', DIGEST, github))
            self.assertEqual(github.release, release)
            self.assertEqual(github.writes(), [])

    def test_prerelease_classification_preserves_manual_tag_behavior(self):
        github = GitHub()
        github.existing_tag(TAG + '-rc.1')
        self.assertTrue(automation.verify_metadata(SOURCE, 'v2026.1110.0-rc.1', DIGEST, github))
        self.assertEqual(github.writes(), [])

    def test_invalid_digest_source_draft_and_conflicting_tag_never_pass_preflight(self):
        for digest in ['latest', 'sha256:bad']:
            with self.assertRaises(ValueError): automation.verify_metadata(SOURCE, 'v2026.1110.0', digest, GitHub())
        for sha in [None, OTHER]:
            github = GitHub()
            if sha: github.existing_tag(sha=sha)
            with self.assertRaisesRegex(ValueError, 'immutable tag'): automation.verify_metadata(SOURCE, 'v2026.1110.0', DIGEST, github)
            self.assertEqual(github.writes(), [])
        for release in [{'tag_name': TAG, 'draft': True}, {'tag_name': 'wrong', 'draft': False}]:
            github = GitHub()
            github.existing_tag()
            github.release = release
            with self.assertRaises(ValueError): automation.verify_metadata(SOURCE, 'v2026.1110.0', DIGEST, github)
            self.assertEqual(github.writes(), [])

    def test_preflight_cli_emits_action_input_without_writing_to_github(self):
        with tempfile.TemporaryDirectory() as directory:
            output, summary = Path(directory) / 'output', Path(directory) / 'summary'
            environment = {'GITHUB_REPOSITORY': automation.REPOSITORY, 'GITHUB_TOKEN': 'test-token',
                'REVISION': SOURCE, 'VERSION': 'v2026.1110.0', 'DIGEST': DIGEST,
                'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary)}
            responses = [io.BytesIO(json.dumps({'ref': 'refs/tags/' + TAG,
                'object': {'type': 'commit', 'sha': SOURCE}}).encode()),
                io.BytesIO(json.dumps({'tag_name': TAG, 'draft': False, 'body': 'Human notes'}).encode())]
            with patch.dict(os.environ, environment, clear=True), patch('sys.argv', ['release-automation.py', 'verify-metadata']), patch('urllib.request.build_opener') as build, patch('sys.stdout', new_callable=io.StringIO):
                build.return_value.open.side_effect = responses
                runpy.run_path(str(Path(automation.__file__)), run_name='__main__')
            self.assertEqual(output.read_text(), 'prerelease=false\n')
            self.assertEqual(summary.read_text(), 'Verified published image metadata\n')
            self.assertEqual([call.args[0].get_method() for call in build.return_value.open.call_args_list], ['GET', 'GET'])

    def test_preflight_api_errors_do_not_become_absent_releases(self):
        for failure in ['HTTP 401', 'HTTP 403', 'HTTP 429', 'network error']:
            github = GitHub()
            github.existing_tag()
            def failed(method, path, data=None, **kwargs):
                if path.startswith('/releases/tags/'): raise RuntimeError(failure)
                return github(method, path, data, **kwargs)
            with self.assertRaisesRegex(RuntimeError, failure):
                automation.verify_metadata(SOURCE, 'v2026.1110.0', DIGEST, failed)
            self.assertEqual(github.writes(), [])


class ApiBoundaryTest(unittest.TestCase):
    def test_builtin_token_only_fixed_origin_and_no_redirects(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'test-token'}, clear=True), patch.object(automation.opener, 'open', return_value=io.BytesIO(b'{}')) as opened:
            automation.api('POST', '/git/refs', {'sha': SOURCE})
            request = opened.call_args.args[0]
            self.assertEqual(request.full_url, 'https://api.github.com/repos/Driftward-LLC/paperclip/git/refs')
            self.assertEqual(request.get_header('Authorization'), 'Bearer test-token')
            self.assertEqual(json.loads(request.data), {'sha': SOURCE})
        self.assertIsNone(automation.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.invalid/'))
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'GITHUB_TOKEN'):
            automation.api('GET', '/tags')

    def test_errors_never_disclose_response_or_credentials_and_only_explicit_404_is_absent(self):
        for code in [302, 400, 401, 403, 404, 422, 429, 500]:
            error = HTTPError('https://api.github.com/private', code, 'test-token', {}, io.BytesIO(b'test-token'))
            with patch.dict(os.environ, {'GITHUB_TOKEN': 'test-token'}, clear=True), patch.object(automation.opener, 'open', side_effect=error):
                with self.assertRaisesRegex(RuntimeError, f'HTTP {code}') as raised: automation.api('GET', '/tags')
                self.assertNotIn('test-token', str(raised.exception))
                if code == 404: self.assertIsNone(automation.api('GET', '/releases/tags/test', missing=True))
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'test-token'}, clear=True), patch.object(automation.opener, 'open', side_effect=URLError('test-token')):
            with self.assertRaisesRegex(RuntimeError, 'network error') as raised: automation.api('GET', '/tags')
            self.assertNotIn('test-token', str(raised.exception))


class LatestPromotionTest(unittest.TestCase):
    def release(self, tag=TAG, **changes):
        return {'tag_name': tag, 'draft': False, 'prerelease': False,
            'published_at': '2026-11-10T12:00:00Z',
            'body': f'Image: `ghcr.io/driftward-llc/paperclip@{DIGEST}`\n\nSource: `{SOURCE}`\n\nHuman notes stay intact.', **changes}

    def github(self):
        github = GitHub()
        github.existing_tag()
        github.release = self.release()
        github.releases = [copy.deepcopy(github.release)]
        return github

    def eligible(self, github):
        return automation.latest_eligible(SOURCE, 'v2026.1110.0', DIGEST, github)

    def test_first_promotion_and_retry_are_read_only_and_preserve_human_notes(self):
        github = self.github()
        receipt = copy.deepcopy(github.release)
        for _ in range(2): self.assertTrue(self.eligible(github))
        self.assertEqual(github.release, receipt)
        self.assertEqual(github.writes(), [])

    def test_older_metadata_retry_cannot_replace_a_newer_published_stable_release(self):
        for newer in ['driftward-v2026.1110.1', 'driftward-v2026.1111.0', 'driftward-v2027.101.0']:
            github = self.github()
            github.releases.insert(0, self.release(newer))
            self.assertFalse(self.eligible(github))
            self.assertEqual(github.writes(), [])

    def test_version_order_is_numeric_and_not_publication_order(self):
        github = self.github()
        github.existing_tag('driftward-v2026.1110.9')
        github.release = self.release('driftward-v2026.1110.9')
        github.releases = [self.release('driftward-v2026.1110.10'), github.release]
        self.assertFalse(automation.latest_eligible(SOURCE, 'v2026.1110.9', DIGEST, github))
        github.existing_tag('driftward-v2026.1110.10')
        github.release = self.release('driftward-v2026.1110.10')
        self.assertTrue(automation.latest_eligible(SOURCE, 'v2026.1110.10', DIGEST, github))

    def test_drafts_prereleases_nonfork_releases_and_unpublished_images_do_not_supersede(self):
        github = self.github()
        github.releases += [self.release('driftward-v2026.1111.0', draft=True, published_at=None),
            self.release('driftward-v2026.1111.0', prerelease=True),
            self.release('driftward-v2026.1111.0-rc.1', prerelease=True), self.release('v2027.101.0')]
        # An immutable tag/image without a published GitHub release is not a release.
        github.existing_tag('driftward-v2026.1112.0', OTHER)
        self.assertTrue(self.eligible(github))

    def test_candidate_prerelease_is_skipped(self):
        github = self.github()
        tag = TAG + '-rc.1'
        github.existing_tag(tag)
        github.release = self.release(tag, prerelease=True)
        self.assertFalse(automation.latest_eligible(SOURCE, 'v2026.1110.0-rc.1', DIGEST, github))
        self.assertFalse(any(path.startswith('/releases?') for _, path, _ in github.calls))

    def test_all_release_pages_are_checked_before_promotion(self):
        github = self.github()
        github.releases += [self.release('unrelated-' + str(index)) for index in range(99)]
        github.releases.append(self.release('driftward-v2026.1111.0'))
        self.assertFalse(self.eligible(github))
        self.assertTrue(any('/releases?per_page=100&page=2' == path for _, path, _ in github.calls))

    def test_missing_or_conflicting_image_source_receipts_fail_closed(self):
        github = self.github()
        body = github.release['body']
        for invalid in ['', None, body.replace(DIGEST, 'sha256:' + 'd' * 64),
                        body.replace(SOURCE, OTHER), body + '\nImage: `another image`',
                        body + '\nSource: `' + SOURCE + '`']:
            with self.subTest(body=invalid):
                github = self.github()
                github.release['body'] = invalid
                with self.assertRaises(ValueError): self.eligible(github)
                self.assertEqual(github.writes(), [])

    def test_candidate_must_be_published_stable_in_both_exact_receipt_and_inventory(self):
        github = self.github()
        github.release = None
        with self.assertRaises(ValueError): self.eligible(github)
        for changes in [{'draft': True}, {'prerelease': True}, {'published_at': None}]:
            github = self.github()
            github.release.update(changes)
            with self.assertRaises(ValueError): self.eligible(github)
        for release in [None, *[self.release(**changes) for changes in
                               [{'draft': True}, {'prerelease': True}, {'published_at': None}]]]:
            github = self.github()
            github.releases = [] if release is None else [release]
            with self.assertRaises(ValueError): self.eligible(github)

    def test_malformed_or_ambiguous_inventory_fails_closed(self):
        for record in [None, {}, self.release(draft='false'), self.release(prerelease=None),
                       self.release(published_at=None), self.release('driftward-v02026.1110.0')]:
            github = self.github()
            github.releases.append(record)
            with self.assertRaises(ValueError): self.eligible(github)

    def test_inventory_errors_never_mean_no_newer_release(self):
        for failure in ['HTTP 401', 'HTTP 403', 'HTTP 429', 'network error']:
            github = self.github()
            def failed(method, path, data=None, **kwargs):
                if path.startswith('/releases?'): raise RuntimeError(failure)
                return github(method, path, data, **kwargs)
            with self.assertRaisesRegex(RuntimeError, failure):
                automation.latest_eligible(SOURCE, 'v2026.1110.0', DIGEST, failed)

    def test_cli_outputs_boolean_only_after_read_only_verification(self):
        for newer, wanted in [(False, 'true'), (True, 'false')]:
            with tempfile.TemporaryDirectory() as directory:
                output, summary = Path(directory) / 'output', Path(directory) / 'summary'
                github = self.github()
                if newer: github.releases.append(self.release('driftward-v2026.1111.0'))
                responses = [io.BytesIO(json.dumps(github.refs[TAG]).encode()),
                    io.BytesIO(json.dumps(github.release).encode()), io.BytesIO(json.dumps(github.release).encode()),
                    io.BytesIO(json.dumps(github.releases).encode())]
                environment = {'GITHUB_REPOSITORY': automation.REPOSITORY, 'GITHUB_TOKEN': 'test-token',
                    'REVISION': SOURCE, 'VERSION': 'v2026.1110.0', 'DIGEST': DIGEST,
                    'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary)}
                with patch.dict(os.environ, environment, clear=True), patch('sys.argv', ['release-automation.py', 'verify-latest']), patch('urllib.request.build_opener') as build, patch('sys.stdout', new_callable=io.StringIO):
                    build.return_value.open.side_effect = responses
                    runpy.run_path(str(Path(automation.__file__)), run_name='__main__')
                self.assertEqual(output.read_text(), 'promote=' + wanted + '\n')
                self.assertEqual([call.args[0].get_method() for call in build.return_value.open.call_args_list], ['GET'] * 4)


class WorkflowBoundaryTest(unittest.TestCase):
    def test_latest_promotion_follows_metadata_under_the_publisher_lock(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/fork-release.yml').read_text())
        latest = workflow['jobs']['promote_latest']
        self.assertEqual(latest['needs'], ['source', 'publish', 'release_metadata'])
        self.assertEqual(latest['environment'], 'fork-release')
        self.assertEqual(latest['permissions'], {'contents': 'read', 'packages': 'write'})
        self.assertNotIn('concurrency', latest)
        self.assertNotIn('if', latest)
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        self.assertEqual(latest['env'], {'IMAGE': 'ghcr.io/driftward-llc/paperclip',
            'VERSION': '${{ needs.source.outputs.version }}', 'REVISION': '${{ needs.source.outputs.sha }}',
            'DIGEST': '${{ needs.publish.outputs.digest }}'})
        guard = latest['steps'][1]
        self.assertEqual(guard['run'], 'python3 scripts/fork-ci/release-automation.py verify-latest')
        self.assertEqual(guard['env'], {'GITHUB_TOKEN': '${{ secrets.GITHUB_TOKEN }}'})
        for step in latest['steps'][2:]: self.assertEqual(step['if'], "steps.latest.outputs.promote == 'true'")
        login = latest['steps'][3]
        self.assertEqual(login['uses'], workflow['jobs']['publish']['steps'][3]['uses'])
        self.assertEqual(login['with']['password'], '${{ secrets.GITHUB_TOKEN }}')
        self.assertNotIn('GH_PACKAGES_READ_TOKEN', str(latest))

    def test_native_promotion_preserves_digest_and_propagates_write_read_and_integrity_failures(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/fork-release.yml').read_text())
        source = workflow['jobs']['promote_latest']['steps'][-1]['run']
        image = 'ghcr.io/driftward-llc/paperclip'
        for create, inspect, digest, success in [('0', '0', DIGEST, True), ('9', '0', DIGEST, False),
                                                ('0', '11', DIGEST, False), ('0', '0', 'sha256:' + 'd' * 64, False)]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                docker = root / 'docker'
                docker.write_text('''#!/usr/bin/env python3
import json,os,sys
with open(os.environ['CALLS'], 'a') as log: log.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[3] == 'create': sys.exit(int(os.environ['CREATE_EXIT']))
if sys.argv[3] == 'inspect':
    print(os.environ['ACTUAL_DIGEST'])
    sys.exit(int(os.environ['INSPECT_EXIT']))
sys.exit(99)
''')
                docker.chmod(0o755)
                environment = dict(os.environ, PATH=f'{root}:{os.environ["PATH"]}', IMAGE=image,
                    VERSION='v2026.1110.0', DIGEST=DIGEST, CALLS=str(root / 'calls'),
                    GITHUB_STEP_SUMMARY=str(root / 'summary'), CREATE_EXIT=create, INSPECT_EXIT=inspect,
                    ACTUAL_DIGEST=digest)
                result = subprocess.run(['bash', '-c', source], env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()]
                self.assertEqual(calls[0], ['buildx', 'imagetools', 'create', '--prefer-index=false',
                    '--tag', image + ':latest', image + '@' + DIGEST])
                if create == '0':
                    self.assertEqual(calls[1], ['buildx', 'imagetools', 'inspect', '--format', '{{.Manifest.Digest}}', image + ':latest'])
                self.assertEqual((root / 'summary').exists(), success)

    def test_auto_start_and_metadata_keep_permissions_separate_from_reusable_ci(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/fork-release.yml').read_text())
        self.assertEqual(workflow[True]['workflow_run'], {'workflows': ['CI'], 'types': ['completed'], 'branches': ['master']})
        auto = workflow['jobs']['automatic_start']
        self.assertEqual(auto['permissions'], {'contents': 'write', 'actions': 'write'})
        self.assertEqual(auto['steps'][0]['with'], {'ref': '${{ github.sha }}', 'persist-credentials': False})
        self.assertEqual(workflow['jobs']['release_metadata']['permissions'], {'contents': 'write'})
        self.assertEqual(workflow['jobs']['release_metadata']['needs'], ['source', 'publish'])
        metadata = workflow['jobs']['release_metadata']
        self.assertNotIn('if', metadata)
        preflight, release = metadata['steps'][-2:]
        self.assertEqual(preflight['id'], 'metadata')
        self.assertEqual(preflight['run'], 'python3 scripts/fork-ci/release-automation.py verify-metadata')
        self.assertEqual(preflight['env'], {'GITHUB_TOKEN': '${{ secrets.GITHUB_TOKEN }}',
            'REVISION': '${{ needs.source.outputs.sha }}', 'VERSION': '${{ needs.source.outputs.version }}',
            'DIGEST': '${{ needs.publish.outputs.digest }}'})
        self.assertEqual(release['uses'], 'ncipollo/release-action@339a81892b84b4eeb0f6e744e4574d79d0d9b8dd')
        inputs = release['with']
        self.assertEqual(set(inputs), {'tag', 'commit', 'body', 'prerelease', 'makeLatest', 'skipIfReleaseExists',
            'allowUpdates', 'immutableCreate', 'replacesArtifacts'})
        self.assertEqual(inputs['tag'], 'driftward-${{ needs.source.outputs.version }}')
        self.assertEqual(inputs['commit'], '${{ needs.source.outputs.sha }}')
        self.assertEqual(inputs['prerelease'], '${{ steps.metadata.outputs.prerelease }}')
        self.assertEqual(inputs['makeLatest'], 'legacy')
        self.assertTrue(inputs['skipIfReleaseExists'])
        for name in ['allowUpdates', 'immutableCreate', 'replacesArtifacts']: self.assertFalse(inputs[name])
        self.assertIn('ghcr.io/driftward-llc/paperclip@${{ needs.publish.outputs.digest }}', inputs['body'])
        self.assertIn('Source: `${{ needs.source.outputs.sha }}`', inputs['body'])
        self.assertIn('/actions/runs/${{ github.run_id }}', inputs['body'])
        self.assertIn('Deployment remains manual.', inputs['body'])
        self.assertEqual(workflow['jobs']['publish']['permissions'], {'contents': 'read', 'packages': 'write'})
        self.assertEqual(workflow['jobs']['checks']['uses'], './.github/workflows/fork-ci.yml')
        ci = yaml.safe_load((ROOT / '.github/workflows/fork-ci.yml').read_text())
        self.assertEqual(ci['permissions'], {'contents': 'read'})
        self.assertFalse(any('write' in job.get('permissions', {}).values() for job in ci['jobs'].values()))

    def test_starter_and_image_runs_use_separate_groups_and_observable_titles(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/fork-release.yml').read_text())
        expressions = [workflow['concurrency']['group'].strip()[3:-2], workflow['run-name'].strip()[3:-2]]
        cases = [
            [{'event_name': 'workflow_run', 'event': {'workflow_run': {'id': 17}}}, {},
             ['fork-automatic-release', 'Release from CI 17']],
            [{'event_name': 'workflow_dispatch', 'ref_name': TAG}, {'tag': TAG},
             ['fork-docker-release', 'Docker release / ' + TAG]],
            [{'event_name': 'push', 'ref_name': TAG}, {},
             ['fork-docker-release', 'Docker release / ' + TAG]],
        ]
        code = "const vm=require('node:vm');const f=JSON.parse(process.argv[1]);for(const [github,inputs,wanted] of f.cases){const actual=f.expressions.map(e=>vm.runInNewContext(e,{github,inputs,format:(template,value)=>template.replace('{0}',value)}));if(JSON.stringify(actual)!==JSON.stringify(wanted))throw Error(JSON.stringify(actual));}"
        subprocess.run(['node', '-e', code, json.dumps({'expressions': expressions, 'cases': cases})], check=True, capture_output=True, text=True)

    def test_job_event_conditions_prevent_pr_tag_reusable_and_completion_loops(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/fork-release.yml').read_text())
        automatic = workflow['jobs']['automatic_start']['if'].strip()[3:-2]
        source = workflow['jobs']['source']['if']
        run = GitHub().ci
        contexts = []
        for name, overrides, expected in [
            ('trusted completion', {}, True), ('PR completion', {'event': 'pull_request'}, False),
            ('failed completion', {'conclusion': 'failure'}, False), ('foreign source', {'head_repository': {'full_name': 'elsewhere/repo'}}, False),
            ('other workflow', {'path': '.github/workflows/fork-release.yml'}, False)]:
            github = {'repository': automation.REPOSITORY, 'event_name': 'workflow_run', 'ref': 'refs/heads/master',
                      'event': {'workflow_run': {**run, **overrides}}}
            contexts.append([name, github, expected, False])
        for event, ref in [('push', 'refs/tags/' + TAG), ('workflow_dispatch', 'refs/tags/' + TAG), ('pull_request', 'refs/pull/1/merge'), ('workflow_call', 'refs/heads/master')]:
            contexts.append([event, {'repository': automation.REPOSITORY, 'event_name': event, 'ref': ref, 'event': {}}, False, event in ['push', 'workflow_dispatch']])
        code = "const vm=require('node:vm');const f=JSON.parse(process.argv[1]);for(const [name,github,a,s] of f.cases){for(const [expression,wanted] of [[f.auto,a],[f.source,s]]){if(vm.runInNewContext(expression,{github,startsWith:(value,prefix)=>value.startsWith(prefix)})!==wanted)throw Error(name);}}"
        subprocess.run(['node', '-e', code, json.dumps({'auto': automatic, 'source': source, 'cases': contexts})], check=True, capture_output=True, text=True)
