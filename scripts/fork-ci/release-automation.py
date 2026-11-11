import datetime
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = 'Driftward-LLC/paperclip'
PREFIX = f'/repos/{REPOSITORY}'
TAG = re.compile(r'driftward-v[0-9]+\.[0-9]+\.[0-9]+(?:[.-][a-zA-Z0-9.-]+)?')
SHA = re.compile(r'[a-f0-9]{40}')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


opener = urllib.request.build_opener(NoRedirect)


def api(method, path, data=None, missing=False):
    token = os.environ.get('GITHUB_TOKEN', '').strip()
    if not token:
        raise ValueError('GITHUB_TOKEN is required')
    request = urllib.request.Request('https://api.github.com' + PREFIX + path,
        data=json.dumps(data).encode() if data is not None else None, method=method, headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2026-03-10',
        })
    try:
        with opener.open(request, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        if missing and error.code == 404:
            return None
        raise RuntimeError(f'GitHub request failed (HTTP {error.code})') from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError('GitHub request failed: network error') from None


def pages(path, request, key=None):
    records = []
    total = None
    for page in range(1, 101):
        result = request('GET', f'{path}{"&" if "?" in path else "?"}per_page=100&page={page}')
        batch = result.get(key) if key and isinstance(result, dict) else result
        if not isinstance(batch, list) or len(batch) > 100:
            raise ValueError('Invalid GitHub pagination response')
        if key:
            count = result.get('total_count')
            if type(count) is not int or count < 0 or (total is not None and count != total):
                raise ValueError('Incomplete or changing GitHub inventory')
            total = count
        records.extend(batch)
        if len(batch) < 100:
            if total is not None and len(records) != total:
                raise ValueError('Incomplete GitHub inventory')
            return records
    raise ValueError('GitHub pagination limit exceeded')


def validate_sha(sha):
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError('Expected an exact source SHA')


def tag_source(tag, request):
    ref = request('GET', '/git/ref/tags/' + urllib.parse.quote(tag, safe=''), missing=True)
    if ref is None:
        return None
    if ref.get('ref') != 'refs/tags/' + tag:
        raise ValueError('Release tag identity mismatch')
    obj = ref['object']
    for _ in range(5):
        validate_sha(obj.get('sha'))
        if obj.get('type') == 'commit':
            return obj['sha']
        if obj.get('type') != 'tag':
            break
        obj = request('GET', '/git/tags/' + obj['sha'])['object']
    raise ValueError('Invalid release tag target')


def start_release(event, request=api, now=None):
    run = event['workflow_run']
    sha = run.get('head_sha')
    validate_sha(sha)
    run_id, attempt = run.get('id'), run.get('run_attempt')
    if type(run_id) is not int or run_id <= 0 or type(attempt) is not int or attempt <= 0:
        raise ValueError('Invalid CI run identity')
    actual = request('GET', f'/actions/runs/{run_id}')
    for record in [run, actual]:
        if (record.get('id') != run_id or record.get('run_attempt') != attempt or
            record.get('head_sha') != sha or record.get('name') != 'CI' or
            record.get('path') not in ['.github/workflows/fork-ci.yml', '.github/workflows/fork-ci.yml@master'] or
            record.get('event') != 'push' or record.get('head_branch') != 'master' or
            record.get('status') != 'completed' or record.get('conclusion') != 'success' or
            record.get('repository', {}).get('full_name') != REPOSITORY or
            record.get('head_repository', {}).get('full_name') != REPOSITORY):
            raise ValueError('Only successful trusted master-push CI can start a release')
    jobs = pages(f'/actions/runs/{run_id}/attempts/{attempt}/jobs', request, 'jobs')
    validation = [job for job in jobs if job.get('name') == 'Validation']
    if (len(validation) != 1 or validation[0].get('status') != 'completed' or
        validation[0].get('conclusion') != 'success'):
        raise ValueError('Successful Validation is required')
    if request('GET', '/git/ref/heads/master')['object']['sha'] != sha:
        return 'Skipped superseded master source'
    tags = pages('/tags', request)
    matches = []
    today = now or datetime.datetime.now(datetime.timezone.utc)
    prefix = f'driftward-v{today.year}.{today.month}{today.day:02d}.'
    patch = -1
    for tag in tags:
        if not isinstance(tag, dict) or not isinstance(tag.get('name'), str):
            raise ValueError('Invalid tag inventory')
        validate_sha(tag.get('commit', {}).get('sha'))
        name = tag['name']
        if TAG.fullmatch(name) and tag['commit']['sha'] == sha:
            matches.append(name)
        if name.startswith(prefix) and name[len(prefix):].isdigit():
            patch = max(patch, int(name[len(prefix):]))
    if len(matches) > 1:
        raise ValueError('Multiple release tags already identify this source')
    tag = matches[0] if matches else prefix + str(patch + 1)
    if request('GET', '/git/ref/heads/master')['object']['sha'] != sha:
        return 'Skipped superseded master source'
    if not matches:
        try:
            request('POST', '/git/refs', {'ref': 'refs/tags/' + tag, 'sha': sha})
        except RuntimeError:
            if tag_source(tag, request) != sha:
                raise
    if tag_source(tag, request) != sha:
        raise ValueError('Refusing to replace a release tag')
    runs = pages(f'/actions/workflows/create-docker-release.yml/runs?head_sha={sha}', request, 'workflow_runs')
    attempts = [item for item in runs if item.get('display_title') == 'Docker release / ' + tag]
    for item in attempts:
        if (type(item.get('id')) is not int or item['id'] <= 0 or
            item.get('status') not in ['queued', 'requested', 'waiting', 'pending', 'in_progress', 'completed'] or
            item.get('head_sha') != sha or item.get('path') not in
            ['.github/workflows/create-docker-release.yml', '.github/workflows/create-docker-release.yml@' + tag,
             '.github/workflows/create-docker-release.yml@refs/tags/' + tag] or
            item.get('repository', {}).get('full_name') != REPOSITORY or
            item.get('event') not in ['push', 'workflow_dispatch']):
            raise ValueError('Release run identity mismatch')
        if item.get('status') == 'completed' and item.get('conclusion') != 'success':
            raise ValueError('Existing release failed; rerun its failed jobs after investigation')
    if attempts:
        return 'Release already started for ' + tag
    if request('GET', '/git/ref/heads/master')['object']['sha'] != sha:
        return 'Skipped superseded master source; reserved tag ' + tag
    request('POST', '/actions/workflows/create-docker-release.yml/dispatches', {'ref': tag, 'inputs': {'tag': tag}})
    return 'Started release ' + tag + ' for ' + sha


def verify_metadata(sha, version, digest, request=api):
    validate_sha(sha)
    tag = 'driftward-' + version
    if not TAG.fullmatch(tag) or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise ValueError('Invalid published image identity')
    if tag_source(tag, request) != sha:
        raise ValueError('Published release source does not match its immutable tag')
    existing = request('GET', '/releases/tags/' + urllib.parse.quote(tag, safe=''), missing=True)
    if existing is not None and (existing.get('tag_name') != tag or existing.get('draft') is not False):
        raise ValueError('Existing GitHub release does not match the published image receipt')
    return re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', version) is None


if __name__ == '__main__':
    if os.environ.get('GITHUB_REPOSITORY') != REPOSITORY:
        raise ValueError('Unexpected release repository')
    if sys.argv[1:] == ['start']:
        if os.environ.get('GITHUB_EVENT_NAME') != 'workflow_run':
            raise ValueError('Expected a CI completion event')
        result = start_release(json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text()))
    elif sys.argv[1:] == ['verify-metadata']:
        prerelease = verify_metadata(os.environ['REVISION'], os.environ['VERSION'], os.environ['DIGEST'])
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('prerelease=' + str(prerelease).lower() + '\n')
        result = 'Verified published image metadata'
    else:
        raise ValueError('Expected start or verify-metadata')
    print(result)
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
        summary.write(result + '\n')
