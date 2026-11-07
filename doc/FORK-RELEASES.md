# Driftward fork CI and Docker releases

This fork publishes only the self-hosted `production` target of the existing
Dockerfile, for Linux amd64, to `ghcr.io/driftward-llc/paperclip`. It does not
publish npm packages or deploy. Inherited upstream Actions jobs are guarded to run only in paperclipai/paperclip;
their scripts remain available in source history and for upstream synchronization.

## GitHub setup

Enable Actions for the fork and allow the pinned actions used here. Keep default
workflow token permissions read-only and disable workflow PR approvals. Protect
`master`: require a PR and the `Fork validation` job from `Fork CI` (confirm its
exact displayed context from the first PR run). Independently complete Astra review
under the local three-iteration policy before merging; GitHub does not attest Astra
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
this PR merges. Enable only Fork CI for bootstrap; after merge confirm the only
fork-executable definitions are `fork-ci.yml` and `fork-release.yml`.

## Branch, review, and merge

Start a `codex/` branch from current fork `master`. Install with Node 24.11+ and
pnpm 9.15.4 using `pnpm install --frozen-lockfile`. Run `pnpm build`,
`pnpm -r typecheck`, `pnpm test:run`, and
`python3 -m unittest discover -s scripts/fork-ci -p 'test_*.py'`.
Fork validation additionally runs all 17 inherited Node test files referencing
workflow definitions, preserving their upstream assertions alongside fork guards.
Install PyYAML 6.0.2 for the workflow policy tests. Rustup must be available;
the runner package pins its Rust toolchain. Build goes first to materialize workspace exports used by typechecking/tests.
Open a PR using the repository template; have Astra review the exact revision and
validation evidence. After required CI and review pass, merge through the protected
branch. PR jobs use hosted runners, read-only tokens, no deployment credentials,
and no upstream-owned reusable workflows. Ordinary master pushes never publish.

When synchronizing upstream, retain these fork workflows and retain the upstream-only job guards. The workflow policy test
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
The job token must be allowed to read the organization's visible container-package
inventory and write this repository's package. If inventory access is denied,
stop and correct permissions before retrying; do not treat a registry error as an
absent image. The first live publication is deliberately not exercised here.
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
