import importlib.util
import http.client
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
        workflow = yaml.safe_load((root / '.github/workflows/create-docker-release.yml').read_text())
        publish = workflow['jobs']['publish']
        metadata = [step for step in publish['steps'] if step.get('run') == 'python3 scripts/fork-ci/release-preflight.py']
        self.assertEqual(len(metadata), 2)
        for step in metadata:
            self.assertEqual(step['env'], {'GH_PACKAGES_READ_TOKEN': '${{ secrets.GH_PACKAGES_READ_TOKEN }}'})
        for step in publish['steps']:
            if step not in metadata:
                self.assertNotIn('GH_PACKAGES_READ_TOKEN', str(step))
        self.assertNotIn('GH_PACKAGES_READ_TOKEN', str(publish.get('env', {})))
        login = next(step for step in publish['steps'] if step.get('uses', '').startswith('docker/login-action@'))
        self.assertEqual(login['with']['password'], '${{ secrets.GITHUB_TOKEN }}')
        self.assertEqual(publish['permissions'], {'contents': 'read', 'packages': 'write'})
        self.assertNotIn('GH_PACKAGES_READ_TOKEN', (root / '.github/workflows/fork-ci.yml').read_text())


class PublishAccessTest(unittest.TestCase):
    def response(self, status, payload=b'', headers=None):
        response = io.BytesIO(payload)
        response.status = status
        response.headers = headers or {}
        return response

    def exchange(self, location='/v2/driftward-llc/paperclip/blobs/uploads/fixture?_state=opaque'):
        return [self.response(200, b'{"token":"opaque-registry-secret"}'),
                self.response(202, headers={'Location': location}), self.response(204)]

    def test_publish_token_starts_empty_upload_and_cancels_without_publishing(self):
        import base64
        for location in ('/v2/driftward-llc/paperclip/blobs/uploads/fixture?_state=opaque',
                         'https://ghcr.io/v2/driftward-llc/paperclip/blobs/uploads/fixture?_state=opaque'):
            with self.subTest(location=location), patch.dict(os.environ, {'GITHUB_TOKEN': 'publish-secret', 'GITHUB_ACTOR': 'fixture-actor', 'GH_PACKAGES_READ_TOKEN': 'metadata-secret'}, clear=True), patch.object(preflight.opener, 'open', side_effect=self.exchange(location)) as opened:
                preflight.assert_publish_access()
                requests = [call.args[0] for call in opened.call_args_list]
                self.assertEqual([request.get_method() for request in requests], ['GET', 'POST', 'DELETE'])
                self.assertEqual(requests[0].full_url, 'https://ghcr.io/token?service=ghcr.io&scope=repository%3Adriftward-llc%2Fpaperclip%3Apull%2Cpush')
                self.assertEqual(base64.b64decode(requests[0].get_header('Authorization').split()[1]), b'fixture-actor:publish-secret')
                self.assertEqual(requests[1].full_url, 'https://ghcr.io/v2/driftward-llc/paperclip/blobs/uploads/')
                self.assertEqual(requests[1].data, b'')
                self.assertEqual(requests[2].full_url, 'https://ghcr.io/v2/driftward-llc/paperclip/blobs/uploads/fixture?_state=opaque')
                for request in requests[1:]:
                    self.assertEqual(request.get_header('Authorization'), 'Bearer opaque-registry-secret')
                for call in opened.call_args_list:
                    self.assertEqual(call.kwargs, {'timeout': 30})
                    self.assertNotIn('metadata-secret', str(call))

    def test_missing_publish_credentials_do_not_fall_back_to_metadata_token(self):
        for missing in ('GITHUB_TOKEN', 'GITHUB_ACTOR'):
            env = {'GITHUB_TOKEN': 'publish-secret', 'GITHUB_ACTOR': 'fixture-actor', 'GH_PACKAGES_READ_TOKEN': 'metadata-secret'}
            env[missing] = ' '
            with self.subTest(missing=missing), patch.dict(os.environ, env, clear=True), patch.object(preflight.opener, 'open') as opened:
                with self.assertRaisesRegex(ValueError, missing):
                    preflight.assert_publish_access()
                opened.assert_not_called()

    def test_successful_token_exchange_is_not_write_access_proof(self):
        error = HTTPError('https://ghcr.io/fixture', 403, 'publish-secret', {}, io.BytesIO(b'opaque-registry-secret'))
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'publish-secret', 'GITHUB_ACTOR': 'fixture-actor'}, clear=True), patch.object(preflight.opener, 'open', side_effect=[self.exchange()[0], error]) as opened:
            with self.assertRaisesRegex(RuntimeError, 'upload access.*HTTP 403') as raised:
                preflight.assert_publish_access()
            self.assertEqual(opened.call_count, 2)
            self.assertNotIn('secret', str(raised.exception))

    def test_every_request_fails_closed_on_http_transport_and_unexpected_status(self):
        for phase in range(3):
            for failure in (HTTPError('https://ghcr.io/private', 302, 'publish-secret', {}, io.BytesIO(b'opaque-registry-secret')),
                            HTTPError('https://ghcr.io/private', 401, 'publish-secret', {}, None),
                            HTTPError('https://ghcr.io/private', 403, 'publish-secret', {}, None),
                            HTTPError('https://ghcr.io/private', 429, 'publish-secret', {}, None),
                            HTTPError('https://ghcr.io/private', 500, 'publish-secret', {}, None),
                            URLError('publish-secret'), TimeoutError('opaque-registry-secret'), self.response(201)):
                responses = self.exchange()[:phase] + [failure]
                with self.subTest(phase=phase, failure=type(failure).__name__), patch.dict(os.environ, {'GITHUB_TOKEN': 'publish-secret', 'GITHUB_ACTOR': 'fixture-actor'}, clear=True), patch.object(preflight.opener, 'open', side_effect=responses) as opened:
                    with self.assertRaises(RuntimeError) as raised:
                        preflight.assert_publish_access()
                    self.assertNotIn('secret', str(raised.exception))
                    self.assertEqual(opened.call_count, phase + 1)
                    if phase == 2:
                        self.assertIn('cleanup unconfirmed', str(raised.exception))

    def test_token_response_read_failure_is_sanitized(self):
        response = self.response(200)
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'publish-secret', 'GITHUB_ACTOR': 'fixture-actor'}, clear=True), patch.object(preflight.opener, 'open', return_value=response), patch.object(response, 'read', side_effect=http.client.IncompleteRead(b'opaque-registry-secret')):
            with self.assertRaisesRegex(RuntimeError, 'network error') as raised:
                preflight.assert_publish_access()
            self.assertNotIn('secret', str(raised.exception))

    def test_malformed_token_response_never_reaches_upload(self):
        for payload in (b'not json publish-secret', b'[]', b'{}', b'{"token":null}', b'{"token":""}', b'{"token":"bad\\nheader"}', b'{"token":"x"}' + b' ' * 65536):
            with self.subTest(payload=payload), patch.dict(os.environ, {'GITHUB_TOKEN': 'publish-secret', 'GITHUB_ACTOR': 'fixture-actor'}, clear=True), patch.object(preflight.opener, 'open', return_value=self.response(200, payload)) as opened:
                with self.assertRaisesRegex(ValueError, 'token response') as raised:
                    preflight.assert_publish_access()
                self.assertNotIn('secret', str(raised.exception))
                self.assertEqual(opened.call_count, 1)

    def test_unsafe_or_missing_upload_location_never_receives_credentials(self):
        for location in ('', 'https://elsewhere.invalid/v2/driftward-llc/paperclip/blobs/uploads/id',
                         '//elsewhere.invalid/v2/driftward-llc/paperclip/blobs/uploads/id',
                         'http://ghcr.io/v2/driftward-llc/paperclip/blobs/uploads/id',
                         'https://actor@ghcr.io/v2/driftward-llc/paperclip/blobs/uploads/id',
                         'https://[invalid/v2/driftward-llc/paperclip/blobs/uploads/id',
                         '/v2/other/package/blobs/uploads/id', '/v2/driftward-llc/paperclip/blobs/sha256:id',
                         '/v2/driftward-llc/paperclip/blobs/uploads/',
                         '/v2/driftward-llc/paperclip/blobs/uploads/%2e%2e',
                         '/v2/driftward-llc/paperclip/blobs/uploads/id%2f..',
                         '/v2/driftward-llc/paperclip/blobs/uploads/id#fragment',
                         '/v2/driftward-llc/paperclip/blobs/uploads/id\n'):
            with self.subTest(location=location), patch.dict(os.environ, {'GITHUB_TOKEN': 'publish-secret', 'GITHUB_ACTOR': 'fixture-actor'}, clear=True), patch.object(preflight.opener, 'open', side_effect=self.exchange(location)) as opened:
                with self.assertRaisesRegex(ValueError, 'upload location'):
                    preflight.assert_publish_access()
                self.assertEqual(opened.call_count, 2)


class EarlyReleaseAccessTest(unittest.TestCase):
    def test_access_and_inventory_precede_reusable_ci_and_publish_build(self):
        root = Path(__file__).resolve().parents[2]
        jobs = yaml.safe_load((root / '.github/workflows/create-docker-release.yml').read_text())['jobs']
        self.assertEqual(jobs['checks']['needs'], 'source')
        self.assertEqual(jobs['publish']['needs'], ['source', 'checks'])
        for name in ('source', 'publish'):
            job = jobs[name]
            self.assertEqual(job['environment'], 'fork-release')
            self.assertEqual(job['permissions'], {'contents': 'read', 'packages': 'write'})
            steps = job['steps']
            access = next(i for i, step in enumerate(steps) if step.get('run') == 'python3 scripts/fork-ci/release-preflight.py --publish-access')
            inventory = next(i for i, step in enumerate(steps) if step.get('run') == 'python3 scripts/fork-ci/release-preflight.py')
            self.assertEqual(steps[access]['env'], {'GITHUB_TOKEN': '${{ secrets.GITHUB_TOKEN }}'})
            self.assertEqual(steps[inventory]['env']['GH_PACKAGES_READ_TOKEN'], '${{ secrets.GH_PACKAGES_READ_TOKEN }}')
            if name == 'source':
                resolve = next(i for i, step in enumerate(steps) if step.get('id') == 'source')
                self.assertLess(resolve, inventory)
                self.assertLess(inventory, access)
                self.assertEqual(steps[inventory]['env']['VERSION'], '${{ steps.source.outputs.version }}')
                self.assertEqual(steps[inventory]['env']['REVISION'], '${{ steps.source.outputs.sha }}')
            else:
                build = next(i for i, step in enumerate(steps) if step.get('run', '').startswith('docker buildx build'))
                smoke = next(i for i, step in enumerate(steps) if step.get('run', '').startswith('bash scripts/fork-ci/image-smoke.sh'))
                publish = next(i for i, step in enumerate(steps) if step.get('id') == 'image')
                inventories = [i for i, step in enumerate(steps) if step.get('run') == 'python3 scripts/fork-ci/release-preflight.py']
                self.assertEqual(len(inventories), 2)
                self.assertLess(access, build)
                self.assertLess(inventory, build)
                self.assertLess(smoke, inventories[-1])
                self.assertEqual(inventories[-1] + 1, publish)
