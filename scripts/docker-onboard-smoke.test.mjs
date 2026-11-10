import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { accessSync, constants, readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

// Pins the diagnostics wiring of the Docker leg of the release smoke.
//
// The smoke itself only runs post-merge, against a published artifact, so its
// own failures are the only signal it ever sends — and for a long time that
// signal arrived with no container logs attached: the workflow learned the
// container's name from the harness's output, which a failing harness never
// produced, and the harness ran the container with `--rm` so stopping it
// deleted the logs anyway. These assertions keep both halves fixed.

const repoRoot = new URL("..", import.meta.url).pathname.replace(/\/$/, "");
const scriptPath = join(repoRoot, "scripts", "docker-onboard-smoke.sh");
const script = readFileSync(scriptPath, "utf8");
test("smoke script is executable and parses", () => {
  accessSync(scriptPath, constants.X_OK);
  execFileSync("bash", ["-n", scriptPath]);
});

test("container name can be fixed by the caller", () => {
  // A name the caller chose is a name it still has when this script fails.
  assert.match(script, /CONTAINER_NAME="\$\{SMOKE_CONTAINER_NAME:-\$IMAGE_NAME\}"/);
  // And it is still sanitized into something Docker will accept.
  assert.match(script, /CONTAINER_NAME="\$\{CONTAINER_NAME\/\/\[\^a-zA-Z0-9_\.-\]\/-\}"/);
});

test("the container does not remove itself", () => {
  // `--rm` deletes the container the instant its process exits, so a crash
  // takes the logs with it before any cleanup can read them.
  assert.match(script, /^docker run -d \\$/m);
  assert.doesNotMatch(script, /docker run [^\n]*--rm/);
});

test("cleanup dumps the container logs before it tears the container down", () => {
  const cleanup = script.match(/^cleanup\(\) \{$[\s\S]*?^\}$/m)?.[0];
  assert.ok(cleanup, "cleanup() must exist");
  const dumpAt = cleanup.indexOf("dump_container_logs");
  const stopAt = cleanup.indexOf("docker stop");
  const removeAt = cleanup.indexOf("docker rm");
  assert.ok(dumpAt !== -1, "cleanup() must dump the container logs");
  assert.ok(stopAt !== -1, "cleanup() must still stop the container");
  assert.ok(
    removeAt !== -1,
    "cleanup() must remove the container now that it no longer removes itself",
  );
  assert.ok(
    dumpAt < stopAt && dumpAt < removeAt,
    "cleanup() must dump the logs before the teardown, or the teardown deletes them first",
  );
});

test("the log dump has a destination, and callers are told where it is", () => {
  assert.match(script, /SMOKE_LOG_FILE="\$\{SMOKE_LOG_FILE:-/);
  assert.match(script, /docker logs "\$CONTAINER_NAME" >"\$SMOKE_LOG_FILE"/);
  assert.match(script, /printf 'SMOKE_LOG_FILE=%q\\n' "\$SMOKE_LOG_FILE"/);
});

test("the log dump starts empty on every run", () => {
  // A caller that reuses one path — the default does, for a fixed container
  // name — must not be handed the previous run's logs as this run's evidence
  // when this run fails before a container exists.
  const truncateAt = script.search(/^\s*: >"\$SMOKE_LOG_FILE"/m);
  const dumpAt = script.indexOf("dump_container_logs() {");
  assert.ok(truncateAt !== -1, "the script must truncate SMOKE_LOG_FILE at startup");
  assert.ok(
    truncateAt < dumpAt,
    "the truncation must happen before anything can write the dump",
  );
});

test("the harness uses its configurable readiness timeout for the health check", () => {
  assert.match(
    script,
    /SMOKE_READY_TIMEOUT_SECONDS="\$\{SMOKE_READY_TIMEOUT_SECONDS:-\d+\}"/,
  );
  assert.match(
    script,
    /wait_for_http "\$PAPERCLIP_PUBLIC_URL\/api\/health" "\$SMOKE_READY_TIMEOUT_SECONDS" 1/,
  );
});
