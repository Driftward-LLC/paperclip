import { randomUUID } from "node:crypto";
import { setTimeout } from "node:timers/promises";
import { context, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { InMemorySpanExporter, SimpleSpanProcessor, TracerProvider } from "@opentelemetry/sdk-trace";
import { eq } from "drizzle-orm";
import { agents, authUsers, companyMemberships, createDb, heartbeatRuns, issueComments, issues } from "@paperclipai/db";
import type { NativeRunIdentity, NativeSession, NativeSessionBackend, PrpEvent } from "@paperclipai/paperclip-runner";
import { describe, expect, it, vi } from "vitest";
import { heartbeatService, type HeartbeatServiceOptions } from "../services/heartbeat.js";
import * as durableChatWakeup from "../services/durable-chat-wakeup.js";
import { instanceSettingsService } from "../services/instance-settings.js";
import { closeIdleWarmNativeSessionsForRestart } from "../services/native-runtime/native-session-executor.js";
import { nativeRestartFixture, waitForCondition } from "./helpers/native-restart-fixture.js";
import { getRecoveryPostgresSupport } from "./helpers/recovery-postgres-fixture.js";

const support = await getRecoveryPostgresSupport({});

function completingBackend(db: ReturnType<typeof createDb>): NonNullable<HeartbeatServiceOptions["nativeSessionBackendFactory"]> {
  return execution => {
    let identity: NativeRunIdentity;
    let sourceInstanceId = "";
    let turn = Promise.withResolvers<string>();
    const capabilities = { resume: true, typedEvents: true, steering: false, interruption: true, structuredResult: true };
    const session: NativeSession = {
      identity: () => identity,
      capabilities: async () => capabilities,
      attachRun: async input => { identity = input.identity; },
      startTurn: async () => {
        const turnId = randomUUID();
        turn.resolve(turnId);
        return { turnId };
      },
      async *events() {
        const turnId = await turn.promise;
        const event = (sourceSeq: number, eventType: PrpEvent["eventType"], payload: Record<string, unknown>): PrpEvent => ({
          schema: "paperclip.prp.event.v1", sourceKind: "runner", sourceInstanceId,
          sourceEventId: `${sourceInstanceId}:${identity.runId}:${sourceSeq}`, sourceSeq,
          runId: identity.runId, normalizedSessionId: identity.sessionId, turnId,
          schemaVersion: 1, priority: 0, emittedAt: new Date().toISOString(), eventType, payload,
        });
        yield event(1, "turn.started", {});
        yield event(2, "item.completed", { kind: "agentMessage", channel: "final", text: "Done." });
        turn = Promise.withResolvers<string>();
        yield event(3, "turn.completed", { status: "completed" });
      },
      result: async () => null,
      snapshot: async () => ({ backendKind: "mock", sessionId: "gap-spans-session", identity,
        providerSessionId: "gap-spans-provider-session", cursor: null, activeTurnId: null, pendingRuntimeRequests: [], lineage: [] }),
      close: async () => {},
    };
    const backend: NativeSessionBackend = {
      descriptor: async () => {
        const [run] = await db.select({ runnerInstanceId: heartbeatRuns.runnerInstanceId }).from(heartbeatRuns)
          .where(eq(heartbeatRuns.id, execution.binding.runId));
        if (!run?.runnerInstanceId) throw new Error("Normal native admission did not persist a runner identity");
        sourceInstanceId = run.runnerInstanceId;
        return { kind: "mock", name: "preparation-gap-spans", version: "1", capabilities,
          runtimeContextCapabilities: { instructions: "native", skills: "native", mcp: "native" } };
      },
      openSession: async input => { identity = input.identity; return session; },
      recoverSession: async snapshot => { identity = snapshot.identity; return { recovered: true, session }; },
    };
    return backend;
  };
}

describe.skipIf(!support.supported)("heartbeat preparation gap spans", () => {
  const fixture = nativeRestartFixture({});

  it("emits spans for the work between the named preparation phases", async () => {
    const { companyId, agentId, temporary } = fixture.current;
    const db = createDb(temporary.connectionString, { maxConnections: 4 });
    const endpoint = process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
    vi.stubEnv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test");
    const manager = new AsyncLocalStorageContextManager().enable();
    const exporter = new InMemorySpanExporter();
    const provider = new TracerProvider({ spanProcessors: [new SimpleSpanProcessor({ exporter })] });
    expect(context.setGlobalContextManager(manager)).toBe(true);
    expect(trace.setGlobalTracerProvider(provider)).toBe(true);
    // Slow preparation is also replayed into the native lifecycle trace once it
    // crosses the run-log threshold. Exercise both producers on every run.
    const authorizeRetry = durableChatWakeup.authorizeFailedChatRunRetryWake;
    let delayedAdmission = false;
    const admission = vi.spyOn(durableChatWakeup, "authorizeFailedChatRunRetryWake").mockImplementation(async (...args) => {
      const result = await authorizeRetry(...args);
      if (args[2].phase === "execution" && !delayedAdmission) {
        delayedAdmission = true;
        await setTimeout(75);
      }
      return result;
    });
    const heartbeat = heartbeatService(db, { nativeSessionBackendFactory: completingBackend(db) });
    try {
      const userId = randomUUID();
      await db.insert(authUsers).values({ id: userId, name: "Task owner", email: `${userId}@example.invalid`,
        createdAt: new Date(), updatedAt: new Date() });
      await db.insert(companyMemberships).values({ companyId, principalType: "user", principalId: userId, membershipRole: "owner" });
      await db.update(agents).set({ status: "idle", adapterConfig: { provider: "codex", lifecycleMode: "warm", idleTimeoutMs: 60_000 } })
        .where(eq(agents.id, agentId));
      await instanceSettingsService(db).updateExperimental({ enableAgentChat: true, enableNativeRunner: true });
      const [issue] = await db.insert(issues).values({ companyId, title: "Trace the preparation gaps", status: "in_progress",
        assigneeAgentId: agentId, responsibleUserId: userId, conversationAgentId: agentId, conversationUserId: userId,
        conversationState: "active" }).returning();
      const [message] = await db.insert(issueComments).values({ companyId, issueId: issue!.id,
        authorType: "user", authorUserId: userId, body: "Start." }).returning();
      const run = await heartbeat.wakeup(agentId, { source: "on_demand", triggerDetail: "manual", reason: "issue_commented",
        requestedByActorType: "user", requestedByActorId: userId, manualUserWake: true,
        payload: { issueId: issue!.id, commentId: message!.id },
        contextSnapshot: { issueId: issue!.id, taskKey: issue!.id, wakeCommentId: message!.id } });
      expect(run).not.toBeNull();
      await waitForCondition("the run to settle", () => heartbeat.getTaskDrainStatus().quiescent);
      expect((await db.select().from(heartbeatRuns).where(eq(heartbeatRuns.id, run!.id)))[0])
        .toMatchObject({ status: "succeeded" });

      expect(delayedAdmission).toBe(true);
      await provider.forceFlush();
      const spans = exporter.getFinishedSpans();
      const preparation = [
        "heartbeat.prepare.chat_retry_admission",
        "heartbeat.prepare.workspace_selection",
        "heartbeat.prepare.dependency_readiness",
        "heartbeat.prepare.instance_settings",
        "heartbeat.prepare.workspace_context",
        "heartbeat.prepare.plan_wake_routing",
        "heartbeat.prepare.routine_environment",
        "heartbeat.prepare.responsible_user",
        "heartbeat.prepare.execution_start",
      ];
      const controller = [
        "native.controller.attachment_staging",
        "native.controller.warm_restore",
        "native.controller.span_persistence",
        "native.controller.state_migration",
      ];
      const livePreparation = spans.filter(span => span.instrumentationScope.name === "paperclip.heartbeat-preparation");
      const liveController = spans.filter(span => span.instrumentationScope.name === "paperclip.native-controller");
      for (const name of preparation) expect(livePreparation.map(span => span.name), name).toContain(name);
      for (const name of controller) expect(liveController.map(span => span.name), name).toContain(name);
      const attempts = spans.filter(span => span.name === "task.execution_attempt");
      expect(attempts).toHaveLength(1);
      for (const span of livePreparation.filter(span => preparation.includes(span.name))) {
        expect(span.parentSpanContext?.spanId, span.name).toBe(attempts[0]!.spanContext().spanId);
        expect(span.spanContext().traceId, span.name).toBe(attempts[0]!.spanContext().traceId);
      }
      // Historical copies have the same names but belong to the native lifecycle,
      // whose preparation parent is task.prepare rather than the live attempt.
      const nativeSpans = spans.filter(span => span.instrumentationScope.name === "paperclip.native-runner");
      const taskPreparation = nativeSpans.filter(span => span.name === "task.prepare");
      expect(taskPreparation).toHaveLength(1);
      const recordedPreparation = nativeSpans.filter(span => preparation.includes(span.name));
      expect(recordedPreparation.map(span => span.name)).toContain("heartbeat.prepare.chat_retry_admission");
      for (const span of recordedPreparation) {
        expect(span.parentSpanContext?.spanId, span.name).toBe(taskPreparation[0]!.spanContext().spanId);
        expect(span.spanContext().traceId, span.name).toBe(taskPreparation[0]!.spanContext().traceId);
      }
    } finally {
      admission.mockRestore();
      await heartbeat.drainActiveRunExecutions();
      await closeIdleWarmNativeSessionsForRestart();
      await db.$client.end();
      vi.stubEnv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint);
      trace.disable();
      context.disable();
      await provider.shutdown();
    }
  }, 60_000);
});
