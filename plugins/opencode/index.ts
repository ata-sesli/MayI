import { execFile } from "node:child_process"
import { Plugin } from "@opencode/plugin"

type HookReply = { stored?: boolean; effect?: "allow" | "ask" | "deny"; message?: string }

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

export default Plugin.define({
  id: "mayi",
  async setup(ctx) {
    const executable = typeof ctx.options.mayiExecutable === "string" ? ctx.options.mayiExecutable : "mayi"
    const config = typeof ctx.options.mayiConfig === "string" ? ctx.options.mayiConfig : undefined
    const timeout = ctx.options.timeoutMs ?? 30_000
    if (typeof timeout !== "number" || !Number.isFinite(timeout) || timeout < 100 || timeout > 120_000) {
      throw new Error("MayI: timeoutMs must be between 100 and 120000")
    }

    function query(event: Record<string, unknown>, capture = false): Promise<HookReply> {
      const args = [...(config ? ["--config", config] : []), "hook", "opencode", ...(capture ? ["--user-prompt"] : [])]
      const body = JSON.stringify(event)
      if (Buffer.byteLength(body) > 65536) return Promise.reject(new Error("MayI: request too large"))
      return new Promise((resolve, reject) => {
        // argv and stdin are separate; tool commands are data, never shell code.
        const child = execFile(executable, args, { timeout, killSignal: "SIGKILL", maxBuffer: 65536 }, (error, stdout) => {
          if (error) return reject(new Error("MayI: hook client failed"))
          try {
            const value: unknown = JSON.parse(stdout)
            if (!record(value)) throw new Error("Invalid reply")
            if (event.hook_event_name === "PostToolUse") {
              resolve({})
            } else if (capture) {
              if (value.stored !== true) throw new Error("Capture failed")
              resolve({ stored: true })
            } else {
              const effect = value.effect
              if (effect !== "allow" && effect !== "ask" && effect !== "deny") throw new Error("Invalid decision")
              resolve({ effect, message: typeof value.message === "string" ? value.message : undefined })
            }
          } catch {
            reject(new Error("MayI: invalid hook reply"))
          }
        })
        child.stdin?.on("error", () => reject(new Error("MayI: hook input failed")))
        child.stdin?.end(body)
      })
    }

    await ctx.session.hook("prompt", async (event) => {
      const session = await ctx.session.get({ sessionID: event.sessionID })
      await query({
        hook_event_name: "UserPromptSubmit", session_id: event.sessionID,
        turn_id: event.messageID, cwd: session.location.directory, prompt: event.prompt.text,
      }, true)
    })

    await ctx.permission.hook("evaluate", async (event) => {
      if (event.effect === "deny") return
      // HOLD or any adapter failure requests native review, even for native allow.
      event.effect = "ask"
      event.message = "MayI: human review required"
      try {
        if (event.source?.type !== "tool") return
        const [session, messages] = await Promise.all([
          ctx.session.get({ sessionID: event.sessionID }),
          ctx.session.context({ sessionID: event.sessionID }),
        ])
        const source = event.source
        const message = messages.find((m) => m.id === source.messageID)
        if (message?.type !== "assistant") return
        const call = message.content.find((part) => part.type === "tool" && part.id === source.id)
        if (call?.type !== "tool" || call.state.status !== "running") return
        const turns = messages.filter((m) => m.type === "user").map((m) => m.id)
        if (!turns.length) return
        const reply = await query({
          hook_event_name: "PermissionRequest", session_id: event.sessionID,
          turn_id: turns.at(-1), context_turn_ids: turns, tool_use_id: source.id,
          cwd: session.location.directory, tool_name: call.name === "bash" ? "Bash" : call.name,
          tool_input: call.state.input,
          permission: { action: event.action, resources: event.resources, metadata: event.metadata },
        })
        // A new admitted prompt while MayI evaluated invalidates this approval.
        if (reply.effect === "allow") {
          const current = await ctx.session.context({ sessionID: event.sessionID })
          if (JSON.stringify(current.filter((m) => m.type === "user").map((m) => m.id)) !== JSON.stringify(turns)) return
        }
        event.effect = reply.effect ?? "ask"
        event.message = reply.message
      } catch {
        // No tool input, tokens, user prompts or subprocess stderr in diagnostics.
      }
    })

    await ctx.tool.hook("execute.after", async (event) => {
      try {
        const metadata = event.status === "completed" ? event.result.metadata : undefined
        await query({
          hook_event_name: "PostToolUse", session_id: event.sessionID,
          tool_use_id: event.id, tool_name: event.tool,
          // Only structured metrics when present; never send inputs or outputs.
          tool_response: {
            exit_code: metadata?.exit_code,
            wall_time_seconds: metadata?.wall_time_seconds,
          },
        })
      } catch {
        // Telemetry cannot change the tool's result or the permission decision.
      }
    })
  },
})
