import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
import yaml
from urllib.error import HTTPError, URLError
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('preflight', Path(__file__).with_name('release-preflight.py'))
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class ReleasePreflightTest(unittest.TestCase):
    def fetch(self, versions):
        return lambda path: ([{'name': 'paperclip'}] if '/versions?' not in path else versions)

    def test_absent_tag(self):
        preflight.assert_unpublished(['v1', 'sha-abc'], self.fetch([{'metadata': {'container': {'tags': ['v0']}}}]))

    def test_existing_version_and_sha_tags(self):
        for tag in ['v1', 'sha-abc']:
            with self.assertRaises(ValueError):
                preflight.assert_unpublished(['v1', 'sha-abc'], self.fetch([{'metadata': {'container': {'tags': [tag]}}}]))

    def test_first_package(self):
        paths = []
        def fetch(path):
            paths.append(path)
            return []
        preflight.assert_unpublished(['v1'], fetch)
        self.assertEqual(len(paths), 1)

    def test_http_errors_never_mean_absence(self):
        for code in [400, 401, 403, 404, 429, 500]:
            def fetch(path):
                raise HTTPError(path, code, 'fixture error', {}, None)
            with self.assertRaises(HTTPError):
                preflight.assert_unpublished(['v1'], fetch)

    def test_network_error_fails_closed(self):
        def fetch(path):
            raise URLError('fixture network failure')
        with self.assertRaises(URLError):
            preflight.assert_unpublished(['v1'], fetch)

    def test_versions_errors_fail_closed(self):
        def fetch(path):
            if '/versions?' in path:
                raise HTTPError(path, 403, 'fixture denied', {}, None)
            return [{'name': 'paperclip'}]
        with self.assertRaises(HTTPError):
            preflight.assert_unpublished(['v1'], fetch)

    def test_paginated_versions(self):
        def fetch(path):
            if '/versions?' not in path:
                return [{'name': 'paperclip'}]
            tags = ['v1'] if 'page=2' in path else ['old']
            return [{'metadata': {'container': {'tags': tags}}}] * (1 if 'page=2' in path else 100)
        with self.assertRaises(ValueError):
            preflight.assert_unpublished(['v1'], fetch)


class MetadataCredentialTest(unittest.TestCase):
    def test_missing_or_blank_read_token_fails_without_using_publish_token(self):
        for token in ('', '   '):
            with patch.dict(os.environ, {'GH_PACKAGES_READ_TOKEN': token, 'GITHUB_TOKEN': 'publish-secret'}, clear=True), patch.object(preflight.opener, 'open') as request:
                with self.assertRaisesRegex(ValueError, 'GH_PACKAGES_READ_TOKEN is required'):
                    preflight.get('/orgs/Driftward-LLC/packages?package_type=container')
                request.assert_not_called()

    def test_only_dedicated_metadata_token_is_sent(self):
        with patch.dict(os.environ, {'GH_PACKAGES_READ_TOKEN': 'metadata-secret', 'GITHUB_TOKEN': 'publish-secret'}, clear=True), patch.object(preflight.opener, 'open', return_value=io.BytesIO(b'[]')) as request:
            self.assertEqual(preflight.get('/orgs/Driftward-LLC/packages?package_type=container'), [])
            sent = request.call_args.args[0]
            self.assertEqual(sent.get_header('Authorization'), 'Bearer metadata-secret')
            self.assertEqual(sent.get_header('X-github-api-version'), '2022-11-28')
            self.assertEqual(request.call_args.kwargs, {'timeout': 30})
            self.assertNotIn('publish-secret', str(sent.headers))

    def test_http_failures_are_bounded_redacted_and_never_absence(self):
        for code in (302, 400, 401, 403, 404, 429, 500):
            error = HTTPError('https://api.github.com/fixture', code, 'fixture', {}, io.BytesIO(json.dumps({'message': 'metadata-secret' + 'x' * 500}).encode()))
            with patch.dict(os.environ, {'GH_PACKAGES_READ_TOKEN': 'metadata-secret'}, clear=True), patch.object(preflight.opener, 'open', side_effect=error):
                with self.assertRaisesRegex(RuntimeError, f'HTTP {code}') as raised:
                    preflight.get('/fixture')
                self.assertNotIn('metadata-secret', str(raised.exception))
                self.assertLess(len(str(raised.exception)), 320)

    def test_non_json_error_body_is_not_printed(self):
        error = HTTPError('https://api.github.com/fixture', 500, 'fixture', {}, io.BytesIO(b'private arbitrary response'))
        with patch.dict(os.environ, {'GH_PACKAGES_READ_TOKEN': 'metadata-secret'}, clear=True), patch.object(preflight.opener, 'open', side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                preflight.get('/fixture')
            self.assertNotIn('private arbitrary response', str(raised.exception))

    def test_network_error_does_not_disclose_credentials(self):
        with patch.dict(os.environ, {'GH_PACKAGES_READ_TOKEN': 'metadata-secret'}, clear=True), patch.object(preflight.opener, 'open', side_effect=URLError('metadata-secret')):
            with self.assertRaisesRegex(RuntimeError, 'network error') as raised:
                preflight.get('/fixture')
            self.assertNotIn('metadata-secret', str(raised.exception))

    def test_redirect_is_never_followed_with_read_credential(self):
        self.assertIsNone(preflight.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.invalid/'))


class ReleaseCredentialIsolationTest(unittest.TestCase):
    def test_read_token_is_scoped_to_preflight_publish_uses_job_token(self):
        root = Path(__file__).resolve().parents[2]
        workflow = yaml.safe_load((root / '.github/workflows/fork-release.yml').read_text())
        publish = workflow['jobs']['publish']
        metadata = [step for step in publish['steps'] if step.get('run') == 'python3 scripts/fork-ci/release-preflight.py']
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0]['env'], {'GH_PACKAGES_READ_TOKEN': '${{ secrets.GH_PACKAGES_READ_TOKEN }}'})
        for step in publish['steps']:
            if step is not metadata[0]:
                self.assertNotIn('GH_PACKAGES_READ_TOKEN', str(step))
        self.assertNotIn('GH_PACKAGES_READ_TOKEN', str(publish.get('env', {})))
        login = next(step for step in publish['steps'] if step.get('uses', '').startswith('docker/login-action@'))
        self.assertEqual(login['with']['password'], '${{ secrets.GITHUB_TOKEN }}')
        self.assertEqual(publish['permissions'], {'contents': 'read', 'packages': 'write'})
        self.assertNotIn('GH_PACKAGES_READ_TOKEN', (root / '.github/workflows/fork-ci.yml').read_text())
