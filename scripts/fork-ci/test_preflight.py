import importlib.util
from pathlib import Path
import unittest
from urllib.error import HTTPError, URLError

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
        for code in [401, 403, 404, 429, 500]:
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
