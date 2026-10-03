import { expect, test } from "bun:test"
import plugin from "./index"

// The external OpenCode hook registry is the boundary; MayI CLI/daemon are real.
function host(options = {}) {
  const callbacks = new Map<string, (event: any) => Promise<void>>()
  const messages: any[] = [
    { type: "user", id: "msg_user", text: "Run git status.", time: { created: 1 } },
    { type: "assistant", id: "msg_agent", content: [{
      type: "tool", id: "call_1", name: "bash",
      state: { status: "running", input: { command: "git status", description: "Check status" } },
    }] },
  ]
  const ctx: any = {
    options,
    session: {
      get: async () => ({ location: { directory: "/project" } }),
      context: async () => messages,
      hook: async (name: string, callback: any) => {
        callbacks.set(name, callback)
        return { dispose: async () => callbacks.delete(name) }
      },
    },
    permission: { hook: async (name: string, callback: any) => {
      callbacks.set(name, callback)
      return { dispose: async () => callbacks.delete(name) }
    } },
    tool: { hook: async (name: string, callback: any) => {
      callbacks.set(name, callback)
      return { dispose: async () => callbacks.delete(name) }
    } },
  }
  return { ctx, messages, callbacks }
}

test("unavailable MayI asks, missing tool association asks, configured deny stays deny", async () => {
  const { ctx, callbacks } = host({ mayiExecutable: "/does/not/exist" })
  await plugin.setup(ctx)
  const permission: any = { sessionID: "ses_1", action: "bash", resources: ["git status"],
    source: { type: "tool", messageID: "msg_agent", id: "call_1" }, effect: "allow" }
  await callbacks.get("evaluate")!(permission)
  expect(permission.effect).toBe("ask")
  permission.effect = "deny"
  await callbacks.get("evaluate")!(permission)
  expect(permission.effect).toBe("deny")
  permission.effect = "allow"
  delete permission.source
  await callbacks.get("evaluate")!(permission)
  expect(permission.effect).toBe("ask")
})

test("capture failure rejects prompt admission", async () => {
  const { ctx, callbacks } = host({ mayiExecutable: "/does/not/exist" })
  await plugin.setup(ctx)
  await expect(callbacks.get("prompt")!({ sessionID: "ses_1", messageID: "msg_user",
    prompt: { text: "Run git status." }, delivery: "steer" })).rejects.toThrow("MayI")
})

test("outcome client failure cannot change a completed tool result", async () => {
  const { ctx, callbacks } = host({ mayiExecutable: "/does/not/exist" })
  await plugin.setup(ctx)
  const result = { content: [{ type: "text", text: "sensitive output" }], metadata: { exit_code: 0 } }
  await callbacks.get("execute.after")!({ sessionID: "ses_1", messageID: "msg_agent", id: "call_1",
    tool: "bash", input: { command: "git status" }, status: "completed", result })
  expect(result.content[0].text).toBe("sensitive output")
})

const bridgeTest = process.env.MAYI_TEST_CONFIG ? test : test.skip
bridgeTest("bridge captures prompt and translates real audited approve/hold/deny", async () => {
  // Set by tests/test_agent_adapters.py while its isolated daemon is serving.
  const { ctx, callbacks, messages } = host({
    mayiExecutable: process.env.MAYI_TEST_EXECUTABLE,
    mayiConfig: process.env.MAYI_TEST_CONFIG,
  })
  await plugin.setup(ctx)
  await callbacks.get("prompt")!({ sessionID: "ses_1", messageID: "msg_user",
    prompt: { text: "Run git status." }, delivery: "steer" })
  for (const [command, expected] of [["git status", "allow"], ["unknown", "ask"], ["sudo x", "deny"]]) {
    messages[1].content[0].state.input.command = command
    const permission: any = { sessionID: "ses_1", action: "bash", resources: [command],
      source: { type: "tool", messageID: "msg_agent", id: "call_1" }, effect: "ask" }
    await callbacks.get("evaluate")!(permission)
    expect(permission.effect).toBe(expected)
  }
  await callbacks.get("execute.after")!({ sessionID: "ses_1", messageID: "msg_agent", id: "call_1",
    tool: "bash", status: "completed", input: { command: "git status" },
    result: { content: [{ type: "text", text: "secret" }], metadata: { exit_code: 0, wall_time_seconds: 0.1 } } })
})
