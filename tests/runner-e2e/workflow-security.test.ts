import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";

const repositoryRoot = path.resolve(import.meta.dirname, "../..");

describe("Runner CI boundaries", () => {
  it("keeps provider credentials out of fork CI workflows", async () => {
    const workflowDirectory = path.join(repositoryRoot, ".github/workflows");
    const names = (await readdir(workflowDirectory)).filter((name) =>
      /\.ya?ml$/.test(name),
    );

    for (const name of names) {
      const contents = await readFile(
        path.join(workflowDirectory, name),
        "utf8",
      );
      const providerSecretReferences = [
        ...contents.matchAll(
          /secrets(?:\.(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|OPENROUTER_API_KEY|XAI_API_KEY|GROK_AUTH_JSON|DAYTONA_API_KEY|COPILOT_GITHUB_TOKEN)\b|\[['"](?:OPENAI_API_KEY|ANTHROPIC_API_KEY|OPENROUTER_API_KEY|XAI_API_KEY|GROK_AUTH_JSON|DAYTONA_API_KEY|COPILOT_GITHUB_TOKEN)['"]\])/g,
        ),
      ];
      expect(providerSecretReferences, `${name} must not receive provider credentials`).toHaveLength(0);
    }
  });

  it("keeps the Chrome channel and local executable override mutually exclusive", async () => {
    const config = await readFile(
      path.join(repositoryRoot, "tests/runner-e2e/playwright.config.ts"),
      "utf8",
    );

    expect(config).toContain(
      "process.env.PAPERCLIP_PLAYWRIGHT_CHANNEL?.trim()",
    );
    expect(config).toContain("{ channel: playwrightChannel }");
    expect(config).toContain(
      "process.env.PAPERCLIP_RUNNER_E2E_CHROMIUM_EXECUTABLE?.trim()",
    );
    expect(config).toContain(
      "PAPERCLIP_PLAYWRIGHT_CHANNEL and PAPERCLIP_RUNNER_E2E_CHROMIUM_EXECUTABLE are mutually exclusive",
    );
  });
});
