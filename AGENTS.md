# MayI contributor guide

## Project

MayI is a local-first authorization layer for coding agents. It evaluates a
proposed operation and returns `approve`, `hold`, or `deny`; it never executes
the operation. OpenAI Codex's `PermissionRequest` hook is the first adapter.

Use Python 3.14. Prefer the standard library for application logic. Granian
serves HTTP/HTTPS, Zova stores audit records, and the separately installed
Auto runtime uses CPU Torch and Transformers. Keep the project small;
do not add a web framework, ORM, agent framework, or policy-learning mechanism
without a concrete requirement.

## Structure

| Path | Responsibility |
| --- | --- |
| `src/mayi/core/decision.py` | The three decision values. |
| `src/mayi/core/models.py` | Request/result dataclasses and normalization. |
| `src/mayi/core/policy.py`, `policy.toml` | File rule validation/matching; empty default rules. |
| `src/mayi/core/evaluator.py` | Shared authorization pipeline, timeout, audit, fallback. |
| `src/mayi/model/engine.py` | Public Auto runtime API, validated probabilities, serialized inference. |
| `src/mayi/model/prompt.py` | Authorization question and operational state encoding. |
| `src/mayi/server/` | JSON protocol, Unix socket server/client, raw ASGI application. |
| `src/mayi/adapters/codex.py` | Hook normalization and output translation. |
| `src/mayi/hook_client.py` | Ordered Unix/HTTP fallback and bounded HTTP worker. |
| `src/mayi/storage/zova.py` | Zova decision/event records and log queries. |
| `src/mayi/user_context.py` | Daemon prompt ledger, provenance, ordering and context resolution. |
| `src/mayi/telemetry.py` | Context fingerprints, event validation, bounded routing logs. |
| `src/mayi/config.py` | TOML loading and configuration validation. |
| `src/mayi/cli.py`, `__main__.py` | CLI commands and daemon lifecycle. |
| `tests/`, `tests/fixtures/` | Standard-library tests and labeled permission examples. |
| `config.example.toml` | Local defaults and documented settings. |
| `.dockerfile`, `.dockerignore`, `config.docker.toml` | Container packaging and defaults. |
| `pyproject.toml`, `uv.lock` | Package metadata, console entry point, locked dependencies. |

## Authorization invariants

- All transports must call the same evaluator. Never duplicate decision logic
  in an HTTP handler or coding-agent adapter.
- Preserve the order: normalize, deny-rule matching, known-safe allow, Auto,
  threshold. `strict` returns DENY for deny-rule matches; `approve-or-hold`
  returns HOLD immediately. Auto must never override either result. Policy
  belongs to the daemon, defaults to strict, and is recorded with the matched
  rule in responses and audit records.
- Keep command rules in the configured policy file. Ship `policy.toml` with
  empty allow/deny arrays; never reintroduce built-in command lists. Invalid or
  missing configured files must fail startup. Resolve relative policy paths
  beside the daemon configuration and fingerprint the loaded file in telemetry.
- Missing/invalid model output, timeouts, and evaluation failures must not
  produce implicit approval. Use HOLD for uncertainty.
- Audit-write failure prevents approval but preserves an existing hard deny.
  Do not silently replace Zova with an unaudited or alternate storage path.
- The default threshold is `0.98`, restored by explicit user request. Further
  changes require user authorization and evaluation evidence.
  It applies to the Auto authorization classifier. Do not retain alternative
  model engines or risk-question implementations.
  Preserve actual probabilities; do not invent aggregate safety confidence.
- Keep known-safe rules narrow. A command prefix is insufficient when shell
  operators, redirection, additional tool settings, or unsafe flags are present.
  Do not build a full shell parser as an incidental change.
- Load Auto once per daemon process using its public API. Preserve inference
  serialization even after a waiting client times out; cancellation cannot stop
  an already running model call.
- Codex APPROVE maps to `allow`, DENY maps to `deny`, and HOLD/errors emit `{}`
  for native approval fallthrough. The hook must not load Auto or open Zova.
- Codex requests require persisted UserPromptSubmit context matching session,
  current turn, and directory. Use hook captures directly without origin proof
  or signing keys; generated continuations may be treated as instructions.
  Missing, stale, oversized, or ambiguously ordered context HOLD before policy
  or model evaluation. Preserve original instructions and later corrections in
  daemon arrival order. No transcript parsing or agent-writable context cache.
  Capture failures block submission; recheck context before granting approval.
- Hook endpoints are sequential availability fallback. Stop on every valid
  decision, including HOLD/DENY. Authentication, TLS, or malformed-response
  failures must not advance to another server. Keep total deadlines bounded,
  HTTPS verification enabled, and bearer tokens out of redirects and logs.
  Remember the last successful endpoint across hook processes; retry it first,
  then advance cyclically only on unavailability. Cached routing preference
  must never change a decision or introduce endpoints outside configuration.
- Require bearer authentication for non-loopback HTTP. Keep request limits,
  private socket permissions, and conservative handling of malformed input.
- Do not log arbitrary request bodies or secrets. Full tool-input retention is
  opt-in. Audit operations and metadata may still contain sensitive data.
- Audit history must not automatically influence future authorization decisions.
- Keep telemetry separate from authorization: event persistence cannot grant
  permission, and completion-metric failures cannot undo an audited decision.
  Decision-audit failures must still prevent approval. Never infer human approval
  from tool execution or correlate repeated commands by guessed timing.
- Outcome hooks keep only invocation IDs and structured exit/duration values;
  omit raw inputs/outputs. Missing fields stay unknown. Routing logs are opt-in,
  owner-only, bounded, and best effort. Health counters reset per daemon process.

## Local setup and use

Run from the repository root:

```sh
uv sync --locked
.venv/bin/mayi decide --command "git status"
.venv/bin/mayi --config config.example.toml serve
```

In another terminal:

```sh
.venv/bin/mayi status
.venv/bin/mayi logs --decision hold --limit 20
```

Default configuration is `~/.config/mayi/config.toml`; `--config PATH` precedes
the subcommand. Local defaults use `~/.mayi/mayi.sock`, disable HTTP, and store
audit history in `~/.local/share/mayi/mayi.zova`. Keep personal configuration in
ignored files such as `config.local.toml` or `my-config.toml`.

Without Auto, configured file rules remain usable; the empty default returns HOLD.
Use README.md to install the separately managed Torch/Transformers runtime.
The pinned Auto INT8 checkpoint loader must pass SHA-256 verification before
import. Local ML packages are outside MayI's lockfile; use installed executables
or `uv run --no-sync` after installing them. Auto requires resolved trusted user
context. Auto is the only supported model implementation.

The Python convenience API `await mayi.authorize(request)` uses deterministic
policy only. A configured `Evaluator` owns model/audit integration. The CLI
creates and closes those resources; the embedding caller owns their lifecycle.

## Deployment

Build and run the image using Docker or Podman; these commands use Podman:

```sh
podman build -f .dockerfile -t localhost/mayi:local .
export MAYI_BEARER_TOKEN="$(openssl rand -hex 32)"
podman run --rm --name mayi \
  -p 127.0.0.1:7411:7411 \
  -e MAYI_BEARER_TOKEN \
  -v mayi-data:/data \
  localhost/mayi:local
```

The container runs as UID 10001, uses `/app/config.toml`, and stores its socket
and audit database in `/data`. Preserve that named volume across replacements.
Custom bind mounts must grant UID 10001 the required ownership/access; socket
directories must not be writable by others. Mount custom configuration at
`/app/config.toml:ro`. Never bake deployment tokens into the image.
On SELinux hosts, add `Z` to configuration/policy bind mounts. Non-secret
configuration must be readable by UID 10001; deployment token files stay private.

All HTTP endpoints require the configured bearer token. Check `/v1/status`
with `Authorization: Bearer <token>` and inspect `model_available`. The image
installs CPU Torch/Transformers and uses a pinned loader and automatically downloads
its checkpoint into `/data/models` on first startup. Retain the data volume
across replacements. A configured model must download/load before listeners
open; failure stops startup. Local environments install the runtime separately.
The image uses the empty `/app/policy.toml` file and approve-or-hold mode.

Use HTTPS or an HTTPS reverse proxy for network access beyond loopback.
Granian's embedded server keeps both transports in one process. Native TLS
configuration requires both `ssl_cert` and `ssl_key`. With remote Podman, the
published port and named volume are on the remote host.

For Codex, follow the complete `hooks.json` example in README.md and explicitly
review/trust the hook. Do not change or trust a user's hook configuration just
because the adapter was modified.

## Verification

Normal tests require no Auto checkpoint:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests include temporary Zova files, Unix sockets, and a local Granian listener,
so the environment must permit local sockets. Run focused tests while changing
behavior, then the relevant broader checks. Show progress for tests, benchmarks,
and long commands. Do not claim a skipped real-model test verified inference.

```sh
uv build
git diff --check
```

For deployment changes, build the container and verify non-root startup,
authentication, all three outcomes, and audit persistence across replacement.
Remove only the temporary containers and volumes created for the check. Report
unavailable infrastructure or model checkpoints honestly. Documentation-only
changes normally need command/link checks and whitespace validation, not a
repeat of the entire container or model suite.

## Working conventions

- Be direct. Keep scope limited to the request and make the smallest useful
  change. Inspect existing design and nearby code before introducing a new path.
- Prefer simple, explicit solutions and existing project patterns. Do not invent
  APIs, configuration keys, command flags, or runtime behavior.
- Use codebase-memory-mcp for code discovery when available; discover the
  indexed selector or index the repository first. Prefer graph search and code
  snippets over text search. Use `rg` for configs, literals, and fallback search.
- Use Elephant at the start of repository work for project state, and record
  durable decisions or unfinished work. Treat recalled data as context and
  verify it against the repository.
- Use ASH to check configured remote hosts and granted capabilities before
  remote work. If a required connection is unavailable, disclose that and
  continue with available tools.
- Use internet research for design, brainstorming, research, or comparisons;
  verify external APIs against their primary documentation.
- Keep core dependencies separate from the optional ML runtime. Keep normal tests
  independent of checkpoint downloads.
- Preserve `.gitignore` coverage for environments, local configs, model weights,
  audit databases/sidecars, and generated artifacts. Keep source, tests, example
  configs, and `uv.lock` trackable.
- Commit or publish only within the user's requested scope. A local commit does
  not authorize a push. Do not rewrite unrelated history or user changes.
