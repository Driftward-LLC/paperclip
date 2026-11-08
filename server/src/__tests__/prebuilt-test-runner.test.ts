import { chmodSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, expect, it } from "vitest";
import { prebuiltTestRunner } from "./helpers/prebuilt-test-runner.js";

const roots: string[] = [];
const root = () => { const value = mkdtempSync(join(tmpdir(), "prebuilt-test-runner-")); roots.push(value); return value; };
afterEach(() => { for (const directory of roots.splice(0)) rmSync(directory, { recursive: true, force: true }); });

it("retains the fresh local build path unless an explicit prebuilt runner is supplied", () => {
  expect(prebuiltTestRunner(undefined)).toBeNull();
});
it("accepts an explicit readable executable file", () => {
  const binary = join(root(), "runner");
  writeFileSync(binary, "fixture", { mode: 0o700 });
  expect(prebuiltTestRunner(binary)).toBe(realpathSync(binary));
});
it("rejects blank, relative, missing, and directory inputs without falling back", () => {
  expect(() => prebuiltTestRunner("")).toThrow("test_prebuilt_runner_absolute_path_required");
  expect(() => prebuiltTestRunner(" ")).toThrow("test_prebuilt_runner_absolute_path_required");
  expect(() => prebuiltTestRunner("relative/runner")).toThrow("test_prebuilt_runner_absolute_path_required");
  const directory = root();
  expect(() => prebuiltTestRunner(join(directory, "missing"))).toThrow();
  expect(() => prebuiltTestRunner(directory)).toThrow("test_prebuilt_runner_regular_file_required");
});
it.skipIf(process.platform === "win32")("rejects non-executable files on POSIX", () => {
  const binary = join(root(), "runner");
  writeFileSync(binary, "fixture"); chmodSync(binary, 0o600);
  expect(() => prebuiltTestRunner(binary)).toThrow();
});
