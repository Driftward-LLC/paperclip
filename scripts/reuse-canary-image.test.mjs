import assert from "node:assert/strict";
import test from "node:test";
import { reuseCanaryImage } from "./reuse-canary-image.mjs";

const sha = "a".repeat(40);
const digest = `sha256:${"b".repeat(64)}`;

test("reuses only an exact-source, master-signed multi-platform image by digest", async () => {
  const calls = [];
  const reused = await reuseCanaryImage(sha, {
    resolve: async source => { assert.equal(source, sha); return digest; },
    run: async (...args) => calls.push(args),
  });
  assert.equal(reused, true);
  assert.deepEqual(calls, [
    ["gh", ["attestation", "verify", `oci://ghcr.io/paperclipai/paperclip@${digest}`,
      "--repo", "paperclipai/paperclip", "--bundle-from-oci",
      "--signer-workflow", "paperclipai/paperclip/.github/workflows/docker.yml",
      "--source-ref", "refs/heads/master", "--source-digest", sha,
      "--signer-digest", sha, "--deny-self-hosted-runners"]],
    ["docker", ["buildx", "imagetools", "create", "-t",
      "ghcr.io/paperclipai/paperclip:sha-aaaaaaa", `ghcr.io/paperclipai/paperclip@${digest}`]],
  ]);
});

test("a missing master image enables the fallback without retagging", async () => {
  assert.equal(await reuseCanaryImage(sha, {
    resolve: async () => null,
    run: () => assert.fail("A missing image must not be promoted"),
  }), false);
});

test("registry and provenance failures stop promotion rather than bypass verification", async () => {
  await assert.rejects(reuseCanaryImage(sha, {
    resolve: async () => { throw new Error("HTTP 403"); },
    run: () => assert.fail("A registry failure must not be promoted"),
  }), /HTTP 403/);
  let commands = 0;
  await assert.rejects(reuseCanaryImage(sha, {
    resolve: async () => digest,
    run: command => {
      assert.equal(command, "gh");
      commands++;
      throw new Error("Untrusted source or signature");
    },
  }), /Untrusted source/);
  assert.equal(commands, 1);
});

test("invalid source refs and digests cannot reach external commands", async () => {
  for (const invalid of ["master", "a".repeat(7), "a".repeat(40) + "\n"]) {
    await assert.rejects(reuseCanaryImage(invalid, {
      resolve: () => assert.fail("Invalid source accepted"),
    }));
  }
  await assert.rejects(reuseCanaryImage(sha, {
    resolve: async () => "mutable:tag",
    run: () => assert.fail("Invalid digest accepted"),
  }));
});
