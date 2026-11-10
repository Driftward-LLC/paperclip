import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { runnerMatrix } from "./catalog.js";
import { requiresEverydayArtifactOracle } from "./everyday-cases.js";
import { requiresCodexCiSandbox } from "./codex-ci-sandbox.js";

const root = path.resolve(import.meta.dirname, "../..");

describe("Codex CI sandbox trust boundary", () => {
  it.each([
    ["everyday-workflows.runner-codex.local.build-revise", true],
    ["everyday-workflows.runner-codex-mini.local.build-revise", true],
    ["everyday-workflows.runner-acpx-claude.local.build-revise", false],
    ["everyday-workflows.runner-codex.daytona.build-revise", false],
    ["core-compatibility.legacy-codex.local.message-marker", false],
    ["core-compatibility.runner-acpx-codex.local.message-marker", true],
    ["case-handoffs.runner-codex-subscription.local.same-task-roundtrip", true],
    ["case-handoffs.runner-codex-subscription.local.receipt-smoke", true],
    ["case-handoffs.runner-codex.local.receipt-smoke", true],
  ] as const)("qualifies the native local Codex sandbox for %s: %s", (id, expected) => {
    const execution = runnerMatrix.find((cell) => cell.id === id);
    expect(execution, id).toBeDefined();
    expect(requiresCodexCiSandbox(execution!)).toBe(expected);
  });

  it("keeps privileged policy changes out of target-controlled tests", async () => {
    const source = await readFile(
      path.join(root, "tests/runner-e2e/codex-ci-sandbox.ts"),
      "utf8",
    );
    expect(source).not.toMatch(
      /execFileSync\(["']sudo|apparmor_parser|flags=\(unconfined\)/,
    );
    expect(source).toMatch(/"sandbox",\s*"--permission-profile",\s*"paperclip-e2e-probe"/);
    expect(source).toContain(
      "permissions.paperclip-e2e-probe.network.enabled=false",
    );
    expect(source).toContain(
      'permissions.paperclip-e2e-probe.filesystem={":root"="read"}',
    );
    expect(source).toContain("Codex sandbox preflight failed");
    expect(source).not.toContain("...process.env");
  });
});

it("retains the restart story and artifact oracle cases", () => {
  expect(runnerMatrix.some((cell) => cell.id === "daytona-restart-continuity.runner-codex.daytona.recover-controller")).toBe(true);
  expect(requiresEverydayArtifactOracle("agent-review-handoff")).toBe(true);
  expect(requiresEverydayArtifactOracle("create-skill-studio")).toBe(false);
});
