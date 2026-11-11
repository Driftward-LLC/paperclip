# Driftward fork CI and Docker releases

This fork publishes only the self-hosted `production` target of the existing
Dockerfile, for Linux amd64, to `ghcr.io/driftward-llc/paperclip`. It does not
publish npm packages or deploy. Successful CI for the current `master` commit
automatically starts an image release. Only `fork-ci.yml` (`CI`) and
`create-docker-release.yml` (`Docker release`) remain in `.github/workflows`; inherited
upstream workflow definitions are removed. Shared release scripts and their
tests remain for upstream synchronization.

## GitHub setup

Enable Actions for the fork and allow the pinned actions used here. Keep default
workflow token permissions read-only and disable workflow PR approvals. Protect
`master`: require a PR and the `Validation` job from `CI` (confirm its
exact displayed context from the first PR run). Independently complete Astra review
until Astra approves and required checks pass before merging; GitHub does not attest Astra
identity. With one operator, do not require that operator to approve their own PR.
Review `.github/**`, `scripts/fork-ci/**`, and this runbook as privileged code. Configure a tag ruleset for `driftward-v*` preventing updates/deletion without
bypass. The automatic release starter uses the built-in workflow token to create
release tags; operators can still create them manually. Create environment
`fork-release` with deployment branch policy allowing only `driftward-v*` tags;
no extra approval click is required. Grant this repository's GITHUB_TOKEN write
access to its GHCR package. Keep unrelated secrets out of the repository/environment.

Keep retired upstream workflow IDs disabled. GitHub can retain their historical
registrations after their definitions are removed. Enable only `CI` and `Docker
release`, and confirm their source files are `fork-ci.yml` and `create-docker-release.yml`.
The renamed release workflow retains the `fork-release` environment and existing
concurrency groups to preserve configured access and serialize old and new runs.

## Branch, review, and merge

Start a `codex/` branch from current fork `master`. Install with Node 24.11+ and
pnpm 9.15.4 using `pnpm install --frozen-lockfile`. Run `pnpm build`,
`pnpm -r typecheck`, `pnpm test:run`, and
`python3 -m unittest discover -s scripts/fork-ci -p 'test_*.py'`.
Validation additionally runs 16 Node contract files for shared scripts alongside
the workflow policy checks.
CI builds/typechecks once, then runs every row from the checked-in upstream
unit matrix: currently 19 general and nine serialized partitions, with at most
six test runners in parallel. The workflow uses the name `CI`; upstream's
`PR` special case would remove native suites from server partitions. Only generated
package outputs and synthetic native test executables are shared by artifact ID
within the same run and revision. Tests install locked dependencies separately;
they do not repeat the whole workspace build. Native suites that need extra provider fixtures still perform
their own Cargo freshness/build checks. The Dot suite uses the explicit test-only
`PAPERCLIP_TEST_PREBUILT_RUNNER` path only after this same-revision artifact is
restored; it checks that the path is absolute, readable, executable, and a regular
file and fails if an explicitly supplied path is invalid. Executable permission
checking is platform dependent: Node enforces `X_OK` on POSIX; on Windows it checks
existence instead. The permission-specific regression runs on POSIX, while all
invalid-path regressions run on every platform. Without that setting,
local Dot tests retain their locked Cargo freshness build. This reuses the prepared
daemon without repeating a cold Rust build in the test partition. The final `Validation` aggregate
requires preparation and every test partition to succeed, including reusable
release invocations; failure, cancellation, and skipped jobs cannot pass.
Install PyYAML 6.0.2 for the workflow policy tests. Rustup must be available;
the runner package pins its Rust toolchain. Build goes first to materialize workspace exports used by typechecking/tests.
Open a PR using the repository template; have Astra review the exact revision and
validation evidence. After required CI and review pass, merge through the protected
branch. PR jobs use hosted runners, read-only tokens, no deployment credentials,
and no upstream-owned reusable workflows. PR runs never publish; successful CI
for the current master push starts the automatic release.

When synchronizing upstream, retain these two fork workflows and keep upstream
workflow definitions absent. Workflow policy rejects extra definitions, upstream
repository references, and missing local reusable workflows. Review script changes as executable code;
CI runs scripts from the checked revision. CI intentionally runs the full
unit suite rather than upstream paid/live-agent qualification campaigns.

## Automatic and manual image releases

`Docker release` listens for completed `CI` runs. Its starter accepts only a
successful push to this fork's `master`, verifies the CI run and its successful
`Validation` job through GitHub, and checks that the tested SHA is still master.
It checks out trusted default-branch code and consumes no PR artifacts or caches.
A superseded master completion is skipped; this releases successful current heads,
not every intermediate merge. GitHub concurrency keeps one running and one pending
starter, so a newer completion can replace a pending one.

The starter reserves a new immutable tag for that exact tested SHA, using UTC
`driftward-vYYYY.MDD.patch` numbering, such as `driftward-v2026.1110.0`. It scans
all tag pages and chooses the next patch for that day. Every existing tag counts,
even when its image was not published. A retry reuses a unique existing release
tag for the same SHA rather than reserving another version or moving a ref.
Ambiguous tag writes are checked by reading the exact ref back.

The starter explicitly dispatches `create-docker-release.yml` at the reserved tag, with
the identical tag input. A tag created by `GITHUB_TOKEN` does not itself trigger
a new Actions run. Only the starter receives `contents: write` and `actions:
write`; reusable CI remains read-only. No additional token is needed. Existing
queued, running, or successful release attempts suppress another dispatch.
Failed or cancelled attempts require investigation and operator recovery.
An API or dispatch failure leaves any reserved tag intact; rerun the starter to
recover dispatch using that same tag. Do not delete or repoint the tag.

For an explicit release after a reviewed merge, create a protected tag such as
`driftward-v2026.1107.0` at the selected master commit using the authorized GitHub
integration. Tag creation triggers the release. Alternatively select that existing
tag in Actions **Run workflow** and enter the identical tag as the input; selecting
a branch is rejected. This is release intent, separate from production deployment.

The `Docker release` workflow resolves the tag once, requires master ancestry,
checks metadata and GHCR write access, then reruns full CI on that SHA, builds once,
checks health and HTML using a fresh anonymous data volume,
and pushes that same smoke-tested image. The smoke creates no companies/agents
and uses no provider credentials or host data. The source preflight and publish
jobs use the same `fork-release` environment and `packages: write` permission;
reusable CI stays read-only. The run summary records the source SHA and image digest.
Only after that job succeeds, a separate job with `contents: write` verifies the
immutable source and uses the pinned Marketplace **Create Release** action
(`ncipollo/release-action`) to record the digest, source SHA, and build link.
Release updates and artifact replacement are disabled. An existing
public release for the same immutable tag and SHA keeps its human-authored notes.
A conflicting or draft release fails verification and is left unchanged. If only
metadata publication fails, rerun the failed job rather than rebuilding or
republishing the image.
Actual deployment remains a separate manual decision.

Published tags are `v2026.1107.0` and `sha-<full-40-character-commit>`; no `latest`
or mutable channel is written. The workflow serializes releases and uses authenticated GitHub Packages inventory
and version metadata to refuse existing version/SHA tags. A successful empty
inventory supports the first package publication; HTTP authorization errors,
missing endpoints, rate limits, malformed data, and network errors fail closed.
Configure repository Actions secret `GH_PACKAGES_READ_TOKEN` before releasing:
create a dedicated **personal access token (classic)** with only `read:packages`
([GitHub scope documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#authenticating-with-a-personal-access-token-classic)),
from an operator authorized to read this organization's container packages. Do
not add `repo`, write, or delete scopes. Authorize the token for organization SSO
if required, set an expiry, and rotate the secret before expiry. Enter it through
GitHub's secret settings; never commit it, print it, or put it in workflow inputs.
The token must see the target package when it exists; an incomplete readable
inventory is not proof that an inaccessible package is absent.

Only the metadata preflight steps receive this token. GHCR login and publication
continue to use the built-in `GITHUB_TOKEN` with the existing package-write
permission; the metadata token never publishes. PR CI receives neither release
credential. GitHub's container-package REST endpoints can reject the built-in
job token with `400 Invalid argument` even though GHCR login/publishing supports
it; the dedicated read token avoids relying on that endpoint compatibility.
Missing/expired/denied metadata credentials and redirects fail closed, with
bounded error messages. Do not infer absence from registry `403 DENIED`, public
filters, or REST `404`. Stop and fix access rather than bypassing inventory.
Before repeated CI starts, metadata inventory checks the read credential and
refuses existing release tags. A separate write check uses that job's
`GITHUB_TOKEN` and `GITHUB_ACTOR` to request a scoped GHCR registry token, start an
empty blob upload for `driftward-llc/paperclip`, and immediately cancel that upload.
No blob, manifest, image, or tag is published by this check. Registry tokens are
opaque; successful login or token exchange alone does not prove write access,
because the token server may grant fewer permissions than requested
([Distribution authentication contract](https://distribution.github.io/distribution/spec/auth/token/)).
The [Distribution upload contract](https://distribution.github.io/distribution/spec/api/#starting-an-upload)
defines upload initiation and cancellation. The check requires HTTP 202 for
initiation and HTTP 204 for cancellation, rejects redirects, and sends credentials
only to fixed HTTPS GHCR endpoints or a returned upload URL inside this package.
Denied access, malformed responses, network errors, or failed cancellation stop
the release before expensive work. For package denial, grant this repository
**Write** under the package's **Manage Actions access**; Docker login succeeding
does not establish that permission.

The publish job repeats the write check with its own fresh `GITHUB_TOKEN` before
image preparation. Metadata inventory runs again before the build and immediately
before the push. These checks catch changed credentials, permissions and tags;
they cannot make publication atomic or guarantee that access remains unchanged.
The upload check proves permission to begin a push at check time; it cannot
guarantee later blob or manifest publication. Rerunning an older release tag uses
that tag's original workflow; these gates apply only to revisions containing them.
GHCR upload/cancellation compatibility and actual publication require an authorized
workflow run; offline tests do not prove live registry access. An interrupted or
uncancellable check may leave an empty upload session for registry expiry; it
never completes the upload or alters a release tag.
GHCR does not enforce
immutable tags: restrict package write access and do not edit tags externally.
A failed partial publication must be investigated; do not delete/overwrite tags to
retry it. Use a newly reviewed commit and new version if recovery requires a release.
The existing Dockerfile contains moving tool/base-image dependencies, so a source
SHA alone is not a reproducible image identity; retain and deploy by digest.

## Pull, deployment smoke, and rollback

Copy the digest from the successful run summary:

```sh
docker pull ghcr.io/driftward-llc/paperclip@sha256:<digest>
```

Record the digest, source SHA, tag, CI run, and Astra decision in the private
operations handoff. Deployment is a separate operator-approved step using the
existing private deployment procedure; never mount live data for CI smoke tests.
Before production, smoke the digest in an isolated deployment with fresh storage,
then verify health, sign-in, and the expected build revision. Do not run agents.
Operate live Paperclip through its official CLI; keep credentials and host details
out of tracked docs.

Retain the previous known-good digest and a validated database backup before an
upgrade. Roll back the image by selecting that retained digest in the deployment
configuration and following the private restart/verification procedure. Image
rollback cannot reverse database migrations: assess compatibility before restart,
and restore a verified backup when the previous revision cannot read upgraded data.
