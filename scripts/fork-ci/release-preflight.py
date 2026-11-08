#!/usr/bin/env python3
"""Read authenticated package metadata; never infer absence from HTTP errors."""
import json
import os
import urllib.error
import urllib.request

API = 'https://api.github.com'
PACKAGE = '/orgs/Driftward-LLC/packages/container/paperclip'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


opener = urllib.request.build_opener(NoRedirect)


def get(path):
    token = os.environ.get('GH_PACKAGES_READ_TOKEN', '').strip()
    if not token:
        raise ValueError('GH_PACKAGES_READ_TOKEN is required: configure a classic PAT with read:packages')
    request = urllib.request.Request(API + path, headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    try:
        with opener.open(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Print only bounded structured diagnostics, never raw bodies or headers.
        message = ''
        try:
            payload = json.loads(error.read(4096))
            if isinstance(payload, dict) and isinstance(payload.get('message'), str):
                message = ''.join(c for c in payload['message'].replace(token, '[REDACTED]') if c.isprintable())[:256]
        except (ValueError, UnicodeError):
            pass
        raise RuntimeError(f'Package metadata request failed (HTTP {error.code}): {message}') from None
    except urllib.error.URLError:
        raise RuntimeError('Package metadata request failed: network error') from None


def pages(path, fetch):
    for page in range(1, 101):
        separator = '&' if '?' in path else '?'
        records = fetch(f'{path}{separator}per_page=100&page={page}')
        if not isinstance(records, list):
            raise ValueError('Expected authenticated package metadata list')
        yield from records
        if len(records) < 100:
            return
    raise ValueError('Package metadata pagination limit exceeded')


def assert_unpublished(tags, fetch=get):
    # A successful authenticated inventory also handles the first package release.
    # 401/403/404 and transport failures propagate; none mean "absent".
    packages = list(pages('/orgs/Driftward-LLC/packages?package_type=container', fetch))
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get('name'), str):
            raise ValueError('Invalid package metadata')
    if not any(package['name'] == 'paperclip' for package in packages):
        return
    for version in pages(PACKAGE + '/versions?state=active', fetch):
        published = version['metadata']['container']['tags']
        if not isinstance(published, list):
            raise ValueError('Invalid version tag metadata')
        if set(tags).intersection(published):
            raise ValueError('Refusing to replace an existing version or SHA tag')


if __name__ == '__main__':
    assert_unpublished([os.environ['VERSION'], 'sha-' + os.environ['REVISION']])
    print('Authenticated GHCR package inventory contains neither release tag')
