#!/usr/bin/env python3
"""Check release metadata and GHCR write access without publishing an image."""
import argparse
import base64
import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = 'https://api.github.com'
PACKAGE = '/orgs/Driftward-LLC/packages/container/paperclip'
REGISTRY = 'https://ghcr.io'
UPLOAD_PATH = '/v2/driftward-llc/paperclip/blobs/uploads/'


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


def registry_request(request, expected, phase):
    try:
        response = opener.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        error.close()
        raise RuntimeError(f'{phase} failed (HTTP {error.code}); check package Manage Actions access and packages:write') from None
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        raise RuntimeError(f'{phase} failed: network error') from None
    if response.status != expected:
        response.close()
        raise RuntimeError(f'{phase} failed: unexpected HTTP status')
    return response


def assert_publish_access():
    token = os.environ.get('GITHUB_TOKEN', '').strip()
    actor = os.environ.get('GITHUB_ACTOR', '').strip()
    if not token:
        raise ValueError('GITHUB_TOKEN is required for GHCR publish access')
    if not actor:
        raise ValueError('GITHUB_ACTOR is required for GHCR publish access')
    query = urllib.parse.urlencode({'service': 'ghcr.io', 'scope': 'repository:driftward-llc/paperclip:pull,push'})
    basic = base64.b64encode(f'{actor}:{token}'.encode()).decode()
    request = urllib.request.Request(REGISTRY + '/token?' + query, headers={'Authorization': 'Basic ' + basic})
    with registry_request(request, 200, 'GHCR token exchange') as response:
        try:
            body = response.read(65537)
            if len(body) > 65536:
                raise ValueError('oversized response')
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise ValueError('Invalid GHCR token response') from None
        except (OSError, http.client.HTTPException):
            raise RuntimeError('GHCR token exchange failed: network error') from None
    bearer = payload.get('token', payload.get('access_token')) if isinstance(payload, dict) else None
    if not isinstance(bearer, str) or not bearer or len(bearer) > 65536 or any(ord(c) < 33 or ord(c) > 126 for c in bearer):
        raise ValueError('Invalid GHCR token response')
    headers = {'Authorization': 'Bearer ' + bearer}
    request = urllib.request.Request(REGISTRY + UPLOAD_PATH, data=b'', headers=headers, method='POST')
    with registry_request(request, 202, 'GHCR upload access check') as response:
        location = response.headers.get('Location', '')
    if not location or any(ord(c) < 33 or ord(c) > 126 for c in location):
        raise ValueError('Unsafe GHCR upload location; empty upload could not be cancelled')
    try:
        upload_url = urllib.parse.urljoin(REGISTRY + UPLOAD_PATH, location)
        parsed = urllib.parse.urlsplit(upload_url)
    except ValueError:
        raise ValueError('Unsafe GHCR upload location; empty upload could not be cancelled') from None
    suffix = urllib.parse.unquote(parsed.path.removeprefix(UPLOAD_PATH))
    if (parsed.scheme != 'https' or parsed.netloc != 'ghcr.io' or parsed.fragment
            or not parsed.path.startswith(UPLOAD_PATH) or suffix in ('', '.', '..')
            or any(c in suffix for c in '/\\%')):
        raise ValueError('Unsafe GHCR upload location; empty upload could not be cancelled')
    request = urllib.request.Request(upload_url, headers=headers, method='DELETE')
    try:
        with registry_request(request, 204, 'GHCR empty-upload cancellation'):
            pass
    except RuntimeError as error:
        raise RuntimeError(f'{error}; empty upload cleanup unconfirmed') from None


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish-access', action='store_true')
    args = parser.parse_args()
    if args.publish_access:
        assert_publish_access()
        print('GHCR publish token started and cancelled an empty upload; no image or tag published')
    else:
        assert_unpublished([os.environ['VERSION'], 'sha-' + os.environ['REVISION']])
        print('Authenticated GHCR package inventory contains neither release tag')
