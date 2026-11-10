import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";

import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { createServer } from "node:http";
import { canaryStartup } from "./canary-startup-fixture.mjs";
import { canRetryPublishedTarball404, TARBALL_METADATA_TIMEOUT_MS } from "../../tests/canary-onboarding/start-published-canary.mjs";

const repoRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
);

function withCanaryGitFixture(check) {
  const root = mkdtempSync(path.join(os.tmpdir(), "canary-publisher-git-"));
  const cwd = path.join(root, "repository with spaces");
  mkdirSync(cwd);
  const git = (...args) => {
    const result = spawnSync("git", args, { cwd, encoding: "utf8" });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  };
  try {
    git("init", "--initial-branch=master");
    git("config", "user.name", "Release fixture");
    git("config", "user.email", "release-fixture@example.invalid");
    git("config", "commit.gpgsign", "false");
    git("config", "core.hooksPath", "/dev/null");
    writeFileSync(path.join(cwd, "source.txt"), "pinned source\n");
    git("add", "source.txt");
    git("commit", "-m", "Create pinned source");
    const pinned = git("rev-parse", "HEAD");
    writeFileSync(path.join(cwd, "source.txt"), "newer master source\n");
    git("commit", "-am", "Advance master");
    const newer = git("rev-parse", "HEAD");
    git("update-ref", "refs/remotes/origin/master", newer);
    git("checkout", "--detach", pinned);
    const env = {
      ...process.env, REPO_ROOT: cwd, SOURCE_SHA: pinned,
      GITHUB_ACTIONS: "true", GITHUB_REPOSITORY: "paperclipai/paperclip",
      GITHUB_EVENT_NAME: "push", GITHUB_REF: "refs/heads/master",
    };
    // Run the original release guard without running release.sh, npm, or a publisher.
    const guard = () => spawnSync("bash", ["-c", 'source "$1"; require_on_master_branch', "guard", path.join(repoRoot, "scripts/release-lib.sh")], {
      cwd, env, encoding: "utf8", timeout: 5_000,
    });
    check({ git, pinned, guard });
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

test("the original canary release guard still rejects detached HEAD and non-master branches in Actions", () => {
  withCanaryGitFixture(({ git, pinned, guard }) => {
    assert.equal(guard().status, 1, "the release guard must reject detached HEAD even in Actions");
    git("checkout", "-b", "topic", pinned);
    const refs = git("show-ref");
    assert.equal(guard().status, 1);
    assert.equal(git("symbolic-ref", "--short", "HEAD"), "topic");
    assert.equal(git("show-ref"), refs);
  });
});

test("onboard smoke container binds beyond loopback so the mapped port is reachable", () => {
  const dockerfile = readFileSync(
    path.join(repoRoot, "docker/Dockerfile.onboard-smoke"),
    "utf8",
  );

  // `onboard --yes` without an explicit --bind prefers trusted-local
  // defaults and writes a loopback bind, which Docker port mapping cannot
  // reach. The smoke container must pin a non-loopback preset.
  assert.match(dockerfile, /onboard --yes --bind lan/);
});

test("release scripts build native daemons before packaging public bundles", () => {
  const runner = JSON.parse(readFileSync(path.join(repoRoot, 'packages/paperclip-runner/package.json'), 'utf8'));
  assert.match(runner.scripts['build:binary'], /cargo build --release --manifest-path runner\/Cargo.toml --locked/);
  assert.match(runner.scripts['build:binary'], /&& node scripts\/stage-runner-binary.mjs$/);
  const script = readFileSync(path.join(repoRoot, 'scripts/release.sh'), 'utf8');
  assert.ok(script.indexOf('assemble-daemons "$PAPERCLIP_RELEASE_RUNNER_ARTIFACTS_DIR"') < script.indexOf('scripts/build-standalone-public-packages.mjs'));
  assert.match(script, /cp -R .*paperclip-runner\/dist\/bin\/\." .*server\/dist\/vendor\/paperclip-runner\/bin\//);
  assert.match(script, /elif \[ "\$dry_run" = false \]; then[\s\S]*?release_fail "Public releases require/);
});

test("Linux ARM prerequisite setup rejects other hosts before invoking tools and preserves scoped security", { skip: process.platform === "win32" }, () => {
  const helper = path.join(repoRoot, "scripts/prepare-linux-arm64-qualification.sh");
  const source = readFileSync(helper, "utf8");
  const root = mkdtempSync(path.join(tmpdir(), "arm-prerequisite-guard-"));
  try {
    const tools = path.join(root, "tools"); mkdirSync(tools);
    const sentinel = path.join(root, "invoked");
    for (const command of ["dpkg", "uname", "mkdir", "mktemp", "sysctl", "sudo"]) {
      writeFileSync(path.join(tools, command), '#!/bin/bash\nprintf "%s\\n" "$0" >> "$PREREQUISITE_SENTINEL"\nexit 85\n', { mode: 0o755 });
    }
    for (const [actions, runnerOs] of [["", "Linux"], ["false", "Linux"], ["true", "macOS"], ["true", "Windows"]]) {
      const result = spawnSync("/bin/bash", [helper], { encoding: "utf8", env: {
        PATH: tools, GITHUB_ACTIONS: actions, RUNNER_OS: runnerOs, RUNNER_TEMP: root, PREREQUISITE_SENTINEL: sentinel,
      } });
      assert.equal(result.status, 1);
      assert.match(result.stderr, /requires a GitHub Actions Linux runner/);
      assert.equal(existsSync(sentinel), false, "host guard must precede every external tool");
      assert.equal(existsSync(path.join(root, "paperclip-arm-bwrap-prerequisite")), false);
    }
    assert.ok(source.indexOf('"${GITHUB_ACTIONS:-}" != true') < source.indexOf(". /etc/os-release"));
    assert.match(source, /-o Acquire::AllowInsecureRepositories=false/);
    assert.match(source, /download "\$package_name=\$package_version"/);
    assert.match(source, /printf '%s  %s\\n' "\$package_sha256" "\$package_file" \| sha256sum -c -/);
    assert.match(source, /dpkg-deb --fsys-tarfile "\$package_file"/);
    assert.match(source, /tar -xO \.\/usr\/bin\/bwrap/);
    assert.match(source, /11d39094f044f0cda0febb3ad517b830301da6b2ce929664af09ee9e4dd264f9/);
    assert.ok(source.indexOf('test ! -L "$target"') < source.indexOf('sudo install -o root -g root -m 0644 "$stock_profile" "$target"'));
    assert.match(source, /timeout 15s sudo "\$parser" -r "\$target"/);
    for (const setting of ["userns", "apparmor", "clone"]) assert.ok(source.includes(`test "$after_${setting}" = "$before_${setting}"`));
    const aptCommands = source.replace(/\\\n\s*/g, " ").split("\n").filter(line => /\bapt-get\b/.test(line));
    assert.equal(aptCommands.length, 2, "only signed-index update and download may invoke APT");
    assert.match(aptCommands[0], /\bupdate\b/);
    assert.match(aptCommands[1], /\bdownload\b/);
    for (const command of aptCommands) assert.doesNotMatch(command, /\b(?:install|upgrade|remove|purge)\b/);
    assert.doesNotMatch(source, /apt-get[^\n]* install|\bdpkg(?:-deb)?\s+(?:-i|--install|--unpack)|sysctl\s+(?:-w|--system)|\baa-disable\b|\bsetcap\b|systemctl|--no-sandbox/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("canary startup refreshes an incomplete dependency publication, then onboards once", async () => {
  const result = await canaryStartup({ mode: "recover" });
  assert.equal(result.code, 0, result.output);
  const installs = result.calls.filter(call => call.kind === "npm");
  const onboarding = result.calls.filter(call => call.kind === "onboard");
  assert.equal(installs.length, 2);
  assert.equal(onboarding.length, 1);
  for (const call of installs) {
    assert.equal(call.args[0], "install");
    assert.ok(call.args.includes("paperclipai@2026.1009.0-canary.1"));
    assert.ok(call.args.includes("--no-package-lock"));
    assert.ok(call.args.includes("--no-save"));
  }
  assert.ok(!installs[0].args.includes("--prefer-online"));
  assert.ok(installs[1].args.includes("--prefer-online"));
  assert.notEqual(installs[0].args[2], installs[1].args[2]);
  assert.equal(installs[0].cache, installs[1].cache);
  assert.deepEqual(onboarding[0].args.slice(0, 3), ["onboard", "--yes", "--data-dir"]);
});

test("permanent missing versions stop after three attempts without onboarding", async () => {
  const result = await canaryStartup({ mode: "permanent" });
  assert.equal(result.code, 1);
  assert.equal(result.calls.filter(call => call.kind === "npm").length, 3);
  assert.equal(result.calls.filter(call => call.kind === "onboard").length, 0);
  assert.match(result.output, /installation failed.*onboarding was not started/);
});

test("npm failures other than ETARGET fail immediately without onboarding", async () => {
  const result = await canaryStartup({ mode: "auth" });
  assert.equal(result.code, 1);
  assert.equal(result.calls.length, 1);
  assert.match(result.output, /E401/);
});

test("onboarding failures are never retried, even when they mention ETARGET", async () => {
  const result = await canaryStartup({ mode: "success", onboard: "fail" });
  assert.equal(result.code, 1);
  assert.equal(result.calls.filter(call => call.kind === "npm").length, 1);
  assert.equal(result.calls.filter(call => call.kind === "onboard").length, 1);
  assert.match(result.output, /onboarding failed \(exit 17\)/);
});

test("a hanging npm install is stopped within the shared acquisition budget", async () => {
  const result = await canaryStartup({ mode: "hang", budget: 1000 });
  assert.equal(result.code, 1);
  assert.equal(result.calls.filter(call => call.kind === "npm").length, 1);
  assert.equal(result.calls.filter(call => call.kind === "onboard").length, 0);
  assert.ok(result.elapsed < 4000, `install exceeded bounded cancellation: ${result.elapsed}ms`);
  assert.match(result.output, /exceeded its startup budget/);
});

test("backoff consumes the shared budget and does not begin another install after expiry", async () => {
  const result = await canaryStartup({ mode: "permanent", budget: 1000, delay: 5000 });
  assert.equal(result.code, 1);
  assert.equal(result.calls.length, 1);
  assert.match(result.output, /exceeded its startup budget/);
});

test("Playwright cancellation stops an onboarding process without replay", async () => {
  const result = await canaryStartup({ mode: "success", onboard: "hang", cancelAtKind: "onboard" });
  assert.equal(result.code, 1);
  assert.equal(result.calls.filter(call => call.kind === "npm").length, 1);
  const onboarding = result.calls.filter(call => call.kind === "onboard");
  assert.equal(onboarding.length, 1);
  assert.throws(() => process.kill(onboarding[0].pid, 0), {code:"ESRCH"});
  assert.match(result.output, /stopped by SIGTERM/);
  assert.ok(result.elapsed < 2500, `onboarding cancellation took ${result.elapsed}ms`);
});

test("canary install retries retain the existing overall Playwright startup deadline", async () => {
  const { INSTALL_BUDGET_MS } = await import("../../tests/canary-onboarding/start-published-canary.mjs");
  const config = readFileSync(path.join(repoRoot, "tests/canary-onboarding/playwright.config.ts"), "utf8");
  assert.equal(INSTALL_BUDGET_MS, 120_000);
  assert.match(config, /timeout: 300_000/);
  assert.match(config, /start-published-canary\.mjs/);
  assert.match(config, /reuseExistingServer: false/);
});

test("nested ETARGET output does not retry a final authentication or network failure", async () => {
  for (const mode of ["mixed", "network"]) {
    const result = await canaryStartup({ mode });
    assert.equal(result.code, 1);
    assert.equal(result.calls.length, 1);
  }
});

const exactCanary = "2026.1009.0-canary.1";
const canaryTarball = `https://registry.npmjs.org/@paperclipai/server/-/server-${exactCanary}.tgz`;
const tarballFailure = (url = canaryTarball, prefix = "npm error") => `${prefix} code E404\n${prefix} 404 Not Found - GET ${url} - Not found\n${prefix} 404\n${prefix} 404  The requested resource '@paperclipai/server@${url}' could not be found or you do not have permission to access it.\n`;

function tarballProof({ config = {}, manifest = {}, status = 200, redirected = false, responseUrl, rawBody } = {}) {
  const calls = [];
  const dist = { tarball: canaryTarball, integrity: `sha512-${Buffer.alloc(64).toString("base64")}`, ...manifest.dist };
  const data = { name: "@paperclipai/server", version: exactCanary, ...manifest, dist };
  return { calls, options: {
    signal: new AbortController().signal, prefix: "/fixture/install", env: {},
    runConfig: async (command, args, options) => {
      calls.push({ kind: "config", command, args, options });
      return { code: 0, stdout: "registry=https://registry.npmjs.org/\n@paperclipai:registry=undefined\n", ...config };
    },
    fetchMetadata: async (url, options) => {
      calls.push({ kind: "metadata", url, options });
      return { status, redirected, url: responseUrl ?? url, body: new Response(rawBody ?? JSON.stringify(data)).body };
    },
  } };
}

test("exact published tarball proof uses quiet effective config and fresh public metadata without credentials", async () => {
  for (const prefix of ["npm error", "npm ERR!"]) {
    const fixture = tarballProof();
    assert.equal(await canRetryPublishedTarball404(tarballFailure(canaryTarball, prefix), exactCanary, fixture.options), true);
    const [config, metadata] = fixture.calls;
    assert.deepEqual(config.args, ["config", "get", "registry", "@paperclipai:registry", "--prefix", "/fixture/install"]);
    assert.equal(config.options.env, fixture.options.env);
    assert.equal(config.options.quiet, true);
    assert.equal(config.options.captureStdout, true);
    assert.equal(config.options.captureStderr, true);
    assert.equal(config.options.outputLimit, 4096);
    assert.equal(metadata.url, `https://registry.npmjs.org/%40paperclipai%2Fserver/${exactCanary}`);
    assert.equal(metadata.options.signal, config.options.signal);
    assert.equal(metadata.options.redirect, "error");
    assert.equal(metadata.options.credentials, "omit");
    assert.deepEqual(metadata.options.headers, { accept: "application/json", "cache-control": "no-cache" });
  }
});

test("mixed npm errors and noncanonical or unreleased tarballs never read config or metadata", async () => {
  const errors = [
    "npm error code ETARGET\n", "npm error code E401\n", "npm error code ECONNRESET\n",
    `${tarballFailure()}npm error code E401\n`, `${tarballFailure()}npm error code 1\n`,
    `npm error code ENEEDAUTH\n${tarballFailure()}`, `${tarballFailure()}${tarballFailure()}`,
    `${tarballFailure()}npm error 404 Not Found - GET https://registry.example.invalid/a.tgz - Not found\n`,
    `${tarballFailure()}npm error 404 The requested resource 'other@${canaryTarball}' could not be found\n`,
    tarballFailure().replace(" - GET ", " - PUT "),
    ...[
      canaryTarball + "?token=fixture", canaryTarball + "#fragment", canaryTarball.replace("npmjs.org/", "npmjs.org:443/"),
      canaryTarball.replace("npmjs.org/", "npmjs.org.example.invalid/"), canaryTarball.replace("npmjs.org/", "fixture@npmjs.org/"),
      canaryTarball.replace("https:", "http:"), canaryTarball.replace("@paperclipai/server", "%40paperclipai%2Fserver"),
      canaryTarball.replace("@paperclipai/server", "@paperclipai/../server"), canaryTarball.replace(exactCanary, "2026.1009.0-canary.2"),
      "https://registry.npmjs.org/@paperclipai/not-released/-/not-released-2026.1009.0-canary.1.tgz",
      "https://registry.npmjs.org/@paperclipai/plugin-createos/-/plugin-createos-2026.1009.0-canary.1.tgz",
      "https://registry.npmjs.org/%40paperclipai%2Fserver/2026.1009.0-canary.1",
    ].map((url) => tarballFailure(url)),
  ];
  for (const error of errors) {
    const fixture = tarballProof();
    assert.equal(await canRetryPublishedTarball404(error, exactCanary, fixture.options), false, error);
    assert.deepEqual(fixture.calls, [], error);
  }
  for (const version of ["2026.1009.0", "2026.1009.0-nightly.1", "2026.1009.0-beta.1", "2026.1009.0-canary.1+build"]) {
    const fixture = tarballProof();
    assert.equal(await canRetryPublishedTarball404(tarballFailure(canaryTarball.replace(exactCanary, version)), version, fixture.options), false);
    assert.deepEqual(fixture.calls, []);
  }
});

test("effective registry authority respects scope precedence and rejects unknown or private config", async () => {
  const configs = [
    [{ stdout: "registry=https://registry.example.invalid/\n@paperclipai:registry=https://registry.npmjs.org/\n" }, true],
    [{ stdout: "registry=undefined\n@paperclipai:registry=https://registry.npmjs.org/\n" }, true],
    [{ stdout: "registry=https://registry.npmjs.org/\n@paperclipai:registry=https://registry.example.invalid/\n" }, false],
    [{ stdout: "registry=https://registry.example.invalid/\n@paperclipai:registry=undefined\n" }, false],
    [{ stdout: "registry=undefined\n@paperclipai:registry=undefined\n" }, false],
    [{ stdout: "registry=https://registry.npmjs.org/\n@paperclipai:registry=\n" }, false],
    [{ stdout: "registry=https://fixture:secret@registry.npmjs.org/\n@paperclipai:registry=undefined\n" }, false],
    [{ stdout: "unexpected output\n" }, false], [{ code: 1 }, false], [{ signal: "SIGTERM" }, false], [{ outputTruncated: true }, false],
  ];
  for (const [config, expected] of configs) {
    const fixture = tarballProof({ config });
    assert.equal(await canRetryPublishedTarball404(tarballFailure(), exactCanary, fixture.options), expected);
    assert.equal(fixture.calls.filter((call) => call.kind === "metadata").length, expected ? 1 : 0);
  }
});

test("metadata must identify the exact published artifact with valid SHA512 syntax, not merely exist", async () => {
  const fixtures = [
    { status: 404 }, { status: 401 }, { status: 403 }, { status: 302 }, { redirected: true },
    { responseUrl: "https://registry.example.invalid/metadata" }, { rawBody: "not json" },
    { manifest: { name: "@paperclipai/shared" } }, { manifest: { version: "2026.1009.0-canary.2" } },
    { manifest: { dist: { tarball: canaryTarball + "?query=1" } } },
    ...[undefined, "", "sha512-A===", `sha256-${Buffer.alloc(32).toString("base64")}`, `sha512-${Buffer.alloc(64).toString("base64")} extra`]
      .map((integrity) => ({ manifest: { dist: { integrity } } })),
    { rawBody: JSON.stringify({ padding: "x".repeat(65 * 1024) }) },
  ];
  for (const input of fixtures) {
    const fixture = tarballProof(input);
    assert.equal(await canRetryPublishedTarball404(tarballFailure(), exactCanary, fixture.options), false, JSON.stringify(input).slice(0, 200));
  }
});

test("rejected and oversized native response bodies cancel without waiting for slow or rejected cleanup", async () => {
  for (const [status, pending] of [[200, true], [200, false], [404, true], [404, false]]) {
    let cancelled = false;
    const fixture = tarballProof();
    fixture.options.fetchMetadata = async (url) => ({ status, redirected: false, url, body: new Response(new ReadableStream({
      start(controller) { controller.enqueue(new Uint8Array(65 * 1024)); },
      cancel() { cancelled = true; return pending ? new Promise(() => {}) : Promise.reject(new Error("fixture rejected cancellation")); },
    })).body });
    const started = Date.now();
    assert.equal(await canRetryPublishedTarball404(tarballFailure(), exactCanary, fixture.options), false);
    assert.equal(cancelled, true);
    assert.ok(Date.now() - started < 1000, "cancellation acknowledgement must not extend a rejection");
  }
});

test("aborting a stalled metadata read cancels its reader without waiting for acknowledgement", { timeout: 2000 }, async () => {
  let cancelled = false;
  const fixture = tarballProof();
  fixture.options.fetchMetadata = async (url) => ({ status: 200, redirected: false, url,
    body: new Response(new ReadableStream({
      cancel() { cancelled = true; return new Promise(() => {}); },
    })).body });
  const budget = new AbortController();
  const timer = setTimeout(() => budget.abort(new Error("fixture body budget expired")), 100);
  const started = Date.now();
  try {
    await assert.rejects(canRetryPublishedTarball404(tarballFailure(), exactCanary, {
      ...fixture.options, signal: budget.signal,
    }), /fixture body budget expired/);
    assert.equal(cancelled, true);
    assert.ok(Date.now() - started < 1000);
  } finally { clearTimeout(timer); }
});

test("metadata headers cannot outlive the budget and late response bodies are cancelled", { timeout: 2000 }, async () => {
  let headersArrive;
  let cancelled = false;
  const fixture = tarballProof();
  fixture.options.fetchMetadata = (url) => new Promise((resolve) => {
    headersArrive = () => resolve({ status: 200, redirected: false, url,
      body: new Response(new ReadableStream({ cancel() { cancelled = true; } })).body });
  });
  const budget = new AbortController();
  const timer = setTimeout(() => budget.abort(new Error("fixture headers budget expired")), 100);
  const started = Date.now();
  try {
    await assert.rejects(canRetryPublishedTarball404(tarballFailure(), exactCanary, {
      ...fixture.options, signal: budget.signal,
    }), /fixture headers budget expired/);
    assert.ok(Date.now() - started < 1000);
    headersArrive();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(cancelled, true);
  } finally { clearTimeout(timer); }
});

test("real npm effective registry reads isolated prefix, user, and environment configuration", async () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "canary-config-proof-"));
  mkdirSync(path.join(root, "prefix"));
  writeFileSync(path.join(root, "global.npmrc"), "");
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.toLowerCase().startsWith("npm_config_")));
  env.npm_config_userconfig = path.join(root, "user.npmrc");
  env.npm_config_globalconfig = path.join(root, "global.npmrc");
  try {
    for (const [user, project, override, expected] of [
      ["registry=https://registry.npmjs.org/\n", "", undefined, true],
      ["registry=https://registry.example.invalid/\n", "@paperclipai:registry=https://registry.npmjs.org/\n", undefined, true],
      ["registry=https://registry.npmjs.org/\n", "@paperclipai:registry=https://registry.example.invalid/\n", undefined, false],
      ["registry=https://registry.npmjs.org/\n", "", "https://registry.example.invalid/", false],
    ]) {
      writeFileSync(path.join(root, "user.npmrc"), user);
      writeFileSync(path.join(root, "prefix", ".npmrc"), project);
      const fixture = tarballProof();
      delete fixture.options.runConfig;
      assert.equal(await canRetryPublishedTarball404(tarballFailure(), exactCanary, {
        ...fixture.options, prefix: path.join(root, "prefix"), env: { ...env, ...(override ? { npm_config_registry: override } : {}) },
      }), expected);
      assert.equal(fixture.calls.length, expected ? 1 : 0);
    }
  } finally { rmSync(root, { recursive: true, force: true }); }
});

async function stalledMetadata(run) {
  let closed = false;
  let closing = false;
  let observeClose;
  const connectionClosed = new Promise((resolve) => { observeClose = resolve; });
  const server = createServer((_request, response) => {
    response.on("close", () => { closed = true; observeClose(); });
    response.writeHead(200, { "content-type": "application/json" });
    response.write('{"name":');
  });
  // An HTTP client can replace an aborted pooled connection while shutdown
  // starts. Own late connections too, so server.close cannot wait on them.
  server.on("connection", (socket) => { if (closing) socket.destroy(); });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const fixture = tarballProof();
  fixture.options.fetchMetadata = async (url, options) => {
    const response = await fetch(`http://127.0.0.1:${server.address().port}/metadata`, options);
    Object.defineProperty(response, "url", { value: url });
    return response;
  };
  let closeDeadline;
  try {
    await run(fixture.options);
    await Promise.race([connectionClosed, new Promise((_, reject) => {
      closeDeadline = setTimeout(() => reject(new Error("metadata connection outlived cancellation")), 1000);
    })]);
    assert.equal(closed, true, "metadata connection must close before fixture-forced cleanup");
  }
  finally {
    clearTimeout(closeDeadline);
    closing = true;
    await new Promise((resolve) => {
      server.close(resolve);
      server.closeAllConnections();
    });
  }
}

test("shared acquisition cancellation aborts the metadata body before the lookup deadline", async () => {
  await stalledMetadata(async (options) => {
    const budget = new AbortController();
    const timer = setTimeout(() => budget.abort(new Error("fixture shared budget expired")), 250);
    try {
      await assert.rejects(canRetryPublishedTarball404(tarballFailure(), exactCanary, { ...options, signal: budget.signal }), /fixture shared budget expired/);
    } finally { clearTimeout(timer); }
  });
});

test("the short config and metadata deadline also covers a stalled real HTTP body", { timeout: 10_000 }, async () => {
  assert.equal(TARBALL_METADATA_TIMEOUT_MS, 5000);
  const started = Date.now();
  await stalledMetadata(async (options) => {
    assert.equal(await canRetryPublishedTarball404(tarballFailure(), exactCanary, options), false);
  });
  assert.ok(Date.now() - started < 7000, "metadata must not consume the 120s acquisition budget");
});

test("a verified canary tarball404 retries acquisition and runs onboarding once", async () => {
  const result = await canaryStartup({ mode: "tarball-recover" });
  assert.equal(result.code, 0, result.output);
  assert.equal(result.calls.filter((call) => call.kind === "npm").length, 2);
  assert.equal(result.calls.filter((call) => call.kind === "config").length, 1);
  assert.equal(result.calls.filter((call) => call.kind === "metadata").length, 1);
  assert.equal(result.calls.filter((call) => call.kind === "onboard").length, 1);
  assert.doesNotMatch(result.output, /fixture config diagnostic/);
});

test("persistent promised tarball404s stop at three installs without a final lookup or onboarding", async () => {
  const result = await canaryStartup({ mode: "tarball-permanent" });
  assert.equal(result.code, 1);
  assert.equal(result.calls.filter((call) => call.kind === "npm").length, 3);
  assert.equal(result.calls.filter((call) => call.kind === "config").length, 2);
  assert.equal(result.calls.filter((call) => call.kind === "metadata").length, 2);
  assert.equal(result.calls.filter((call) => call.kind === "onboard").length, 0);
  assert.match(result.output, /installation failed.*onboarding was not started/);
});

test("private or oversized config and unpublished metadata404 fail without retry or leaking config", async () => {
  for (const input of [{ config: "private" }, { config: "large" }, { metadata: "missing" }]) {
    const result = await canaryStartup({ mode: "tarball-permanent", ...input });
    assert.equal(result.code, 1);
    assert.equal(result.calls.filter((call) => call.kind === "npm").length, 1);
    assert.equal(result.calls.filter((call) => call.kind === "onboard").length, 0);
    assert.doesNotMatch(result.output, /fixture-user|fixture-password|fixture config diagnostic/);
  }
});

test("truncated npm output cannot hide an earlier auth failure and qualify a tarball retry", async () => {
  const result = await canaryStartup({ mode: "tarball-large" });
  assert.equal(result.code, 1);
  assert.equal(result.calls.filter((call) => call.kind === "npm").length, 1);
  assert.equal(result.calls.filter((call) => call.kind === "config").length, 0);
  assert.equal(result.calls.filter((call) => call.kind === "onboard").length, 0);
});

test("tarball recovery does not replay onboarding failures or change ETARGET for other exact versions", async () => {
  const failed = await canaryStartup({ mode: "tarball-recover", onboard: "fail" });
  assert.equal(failed.code, 1);
  assert.equal(failed.calls.filter((call) => call.kind === "npm").length, 2);
  assert.equal(failed.calls.filter((call) => call.kind === "onboard").length, 1);
  assert.match(failed.output, /onboarding failed \(exit 17\)/);
  const other = await canaryStartup({ mode: "recover", version: "2026.1009.0-nightly.1" });
  assert.equal(other.code, 0, other.output);
  assert.equal(other.calls.filter((call) => call.kind === "npm").length, 2);
  assert.equal(other.calls.filter((call) => call.kind === "onboard").length, 1);
  assert.equal(other.calls.filter((call) => call.kind === "config").length, 0);
});

test("existing ETARGET character-tail behavior survives long multibyte npm diagnostics", async () => {
  const result = await canaryStartup({ mode: "multibyte-recover" });
  assert.equal(result.code, 0, result.output);
  assert.equal(result.calls.filter((call) => call.kind === "npm").length, 2);
  assert.equal(result.calls.filter((call) => call.kind === "onboard").length, 1);
  assert.equal(result.calls.filter((call) => call.kind === "config").length, 0);
});

test("metadata lookup and backoff both consume the unchanged shared acquisition budget", async () => {
  for (const input of [{ metadata: "hang" }, { delay: 5000 }]) {
    const result = await canaryStartup({ mode: "tarball-permanent", budget: 1000, ...input });
    assert.equal(result.code, 1);
    assert.equal(result.calls.filter((call) => call.kind === "npm").length, 1);
    assert.equal(result.calls.filter((call) => call.kind === "onboard").length, 0);
    assert.match(result.output, /exceeded its startup budget/);
    assert.ok(result.elapsed < 4000, `shared-budget cancellation took ${result.elapsed}ms`);
  }
});

test("cancellation reaps a config process and its descendant that ignore SIGTERM", async () => {
  const result = await canaryStartup({ mode: "tarball-permanent", config: "hang", cancelAtKind: "config-descendant" });
  assert.equal(result.code, 1);
  assert.equal(result.calls.filter((call) => call.kind === "npm").length, 1);
  assert.equal(result.calls.filter((call) => call.kind === "onboard").length, 0);
  for (const child of result.calls.filter((call) => ["config", "config-descendant"].includes(call.kind))) {
    assert.throws(() => process.kill(child.pid, 0), { code: "ESRCH" });
  }
  assert.match(result.output, /stopped by SIGTERM/);
});
