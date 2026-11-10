# Driftward fork CI and Docker releases

This fork publishes only the self-hosted `production` target of the existing
Dockerfile, for Linux amd64, to `ghcr.io/driftward-llc/paperclip`. It does not
publish npm packages or deploy. The inherited `release.yml`, `refresh-lockfile.yml`,
`cloud-migrator-artifacts.yml`, `docker.yml`, and `cloud-readiness.yml` definitions
are removed. Remaining inherited Actions jobs are guarded to run only in
`paperclipai/paperclip`; shared release scripts and their tests remain for
upstream synchronization.

## GitHub setup

Enable Actions for the fork and allow the pinned actions used here. Keep default
workflow token permissions read-only and disable workflow PR approvals. Protect
`master`: require a PR and the `Fork validation` job from `Fork CI` (confirm its
exact displayed context from the first PR run). Independently complete Astra review
until Astra approves and required checks pass before merging; GitHub does not attest Astra
identity. With one operator, do not require that operator to approve their own PR.
Review `.github/**`, `scripts/fork-ci/**`, and this runbook as privileged code. Configure a tag ruleset for `driftward-v*` preventing updates/deletion without
bypass. Tag creation uses existing repository write permission; only operators
should create release tags. Create environment
`fork-release` with deployment branch policy allowing only `driftward-v*` tags;
no extra approval click is required. Grant this repository's GITHUB_TOKEN write
access to its GHCR package. Keep unrelated secrets out of the repository/environment.

Before the bootstrap PR, leave all inherited workflow IDs disabled, especially
scheduled, release, cloud, agent-runtime, and privileged PR workflows. Enabling
Actions globally on an old default branch can activate inherited schedules before
this PR merges. Enable only Fork CI for bootstrap; workflows may register
only after the first PR event. During this fork's bootstrap, 13 inherited IDs
registered and were explicitly disabled; do not infer registration from source
files alone. After merge confirm the only fork-executable definitions are
`fork-ci.yml` and `fork-release.yml`.

## Branch, review, and merge

Start a `codex/` branch from current fork `master`. Install with Node 24.11+ and
pnpm 9.15.4 using `pnpm install --frozen-lockfile`. Run `pnpm build`,
`pnpm -r typecheck`, `pnpm test:run`, and
`python3 -m unittest discover -s scripts/fork-ci -p 'test_*.py'`.
Fork validation additionally runs all 17 inherited Node test files referencing
retained workflow definitions and shared release scripts alongside fork guards.
CI builds/typechecks once, then runs every row from the checked-in upstream
unit matrix: currently 19 general and nine serialized partitions, with at most
six test runners in parallel. The workflow retains the name `Fork CI`; upstream's
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
daemon without repeating a cold Rust build in the test partition. The final `Fork validation` aggregate
requires preparation and every test partition to succeed, including reusable
release invocations; failure, cancellation, and skipped jobs cannot pass.
Install PyYAML 6.0.2 for the workflow policy tests. Rustup must be available;
the runner package pins its Rust toolchain. Build goes first to materialize workspace exports used by typechecking/tests.
Open a PR using the repository template; have Astra review the exact revision and
validation evidence. After required CI and review pass, merge through the protected
branch. PR jobs use hosted runners, read-only tokens, no deployment credentials,
and no upstream-owned reusable workflows. Ordinary master pushes never publish.

When synchronizing upstream, retain these fork workflows, keep the five removed
definitions absent, and retain the upstream-only job guards. The workflow policy test
fails when an inherited job lacks its repository guard. Review script changes as executable code;
CI runs scripts from the checked revision. Fork CI intentionally runs the full
unit suite rather than upstream paid/live-agent qualification campaigns.

## Explicit image release

After a reviewed merge, create a protected tag such as
`driftward-v2026.1107.0` at the selected master commit using the authorized GitHub
integration. Tag creation triggers the release. Alternatively select that existing
tag in Actions **Run workflow** and enter the identical tag as the input; selecting
a branch is rejected. This is release intent, separate from production deployment.

The workflow resolves the tag once, requires master ancestry, reruns full CI on
that SHA, builds once, checks health and HTML using a fresh anonymous data volume,
and pushes that same smoke-tested image. The smoke creates no companies/agents
and uses no provider credentials or host data. Only the publish job has package
write permission. The run summary records the source SHA and image digest.

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

Only the release preflight step receives this token. GHCR login and publication
continue to use the built-in `GITHUB_TOKEN` with the existing package-write
permission; the metadata token never publishes. PR CI receives neither release
credential. GitHub's container-package REST endpoints can reject the built-in
job token with `400 Invalid argument` even though GHCR login/publishing supports
it; the dedicated read token avoids relying on that endpoint compatibility.
Missing/expired/denied metadata credentials and redirects fail closed, with
bounded error messages. Do not infer absence from registry `403 DENIED`, public
filters, or REST `404`. Stop and fix access rather than bypassing inventory.
The first live publication is deliberately not exercised here.
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
