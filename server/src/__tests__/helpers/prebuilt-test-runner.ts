import { accessSync, constants, realpathSync, statSync } from "node:fs";
import { isAbsolute } from "node:path";

/** Explicit test-only input: CI binds this to its checked revision's build artifact. */
export function prebuiltTestRunner(value: string | undefined): string | null {
  if (value === undefined) return null;
  if (!isAbsolute(value)) throw new Error("test_prebuilt_runner_absolute_path_required");
  const binary = realpathSync(value);
  if (!statSync(binary).isFile()) throw new Error("test_prebuilt_runner_regular_file_required");
  accessSync(binary, constants.R_OK | constants.X_OK);
  return binary;
}
