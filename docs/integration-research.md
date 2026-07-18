# Integration Research — Phase 0

Findings are empirical, produced on this Mac on 2026-08-31 by probing the installed
software. Every claim below is backed by a probe recorded in `docs/probes/`. Nothing
here is taken from a blog post or inferred from documentation alone.

Environment measured:

| Component | Version | Path |
|---|---|---|
| Codex Desktop (ships as `ChatGPT.app`) | `26.803.41515`, bundle id `com.openai.codex` | `/Applications/ChatGPT.app` |
| Codex core bundled **inside** Desktop | `codex-cli 0.147.0-alpha.6.5` | `/Applications/ChatGPT.app/Contents/Resources/codex` |
| Codex CLI on `$PATH` | `codex-cli 0.133.0` | `/usr/local/bin/codex` |
| Claude Code | `2.1.251` | `/Users/ken/.local/bin/claude` |
| Tailscale | **not installed** | — |
| Git | present | `/usr/bin/git` |

> **Two different Codex cores are installed and they do not speak the same protocol
> revision.** The `$PATH` binary exposes 81 app-server methods; the Desktop-bundled
> binary exposes 95. AI Control resolves the Desktop binary from
> `CODEX_CLI_PATH` in `~/.codex/config.toml` (falling back to the app bundle path)
> and never assumes `$PATH` is representative. Both versions are reported in
> `/diagnostics`.

---

## 1. The headline result

**Codex Desktop is a first-class, officially-supported integration target on this
machine.** It is not a black box and it does not require GUI automation.

Codex Desktop stores its threads in exactly the same on-disk session store as Codex
CLI (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`), and that store is served by the
**official, versioned `codex app-server` JSON-RPC protocol** — a supported surface
with a machine-readable schema the vendor ships in the binary itself
(`codex app-server generate-json-schema`).

Proven end to end:

```
$ codex app-server                       # official JSON-RPC over stdio
→ initialize
→ thread/list       → 150 real threads, with cwd, git sha/branch/originUrl, title
→ thread/read {threadId: <a thread created by Codex Desktop>}
  ← {"name":"Audit foto dokumentu gaps",
     "cwd":"/Users/ken/SpecialGuard_DEV",
     "gitInfo":{"branch":"foto-dokumenty","sha":"01d92b84…",
                "originUrl":"https://projekt.assetgov.cz/Special1/SpecialGuard_DEV"},
     "path":"/Users/ken/.codex/sessions/2026/08/29/rollout-…jsonl"}
```

That is the mandatory Phase 2 requirement — a session created by hand in the Codex
desktop application, discovered and read by an external process, with its repository,
branch and working directory resolved — satisfied through a supported API.

---

## 2. Telling the three sources apart honestly

This was the single most delicate question, because the obvious field is wrong.

The protocol's `SessionSource` enum is:

```
cli | vscode | exec | appServer | unknown | {custom: string} | {subAgent: …}
```

**There is no `desktop` member, and Codex Desktop reports itself as `vscode`.** A
thread created in the desktop app and one created by the VS Code extension are
indistinguishable by the `source` field. Any product that keys off `source` will
mislabel desktop sessions. AI Control does not.

The authoritative discriminator is the `originator` string that the writing client
stamps into the `session_meta` record on the first line of every rollout file.
Measured across a stratified sample of the real session history:

| `originator` | `source` | count | meaning |
|---|---|---|---|
| `Codex Desktop` | `vscode` | 41 | Codex desktop application |
| `codex-tui` | `cli` | 3 | Codex CLI interactive |
| `codex_work_desktop` | `vscode` | 1 | Codex desktop, older build |

`AI Control` therefore classifies a Codex thread by reading the first line of its
rollout file — a single `head -1`, no parsing of the full transcript — and matching
`originator` against a maintained prefix table, with a corroborating signal from
`thread-project-assignments` in `~/.codex/.codex-global-state.json` (a file only the
Electron desktop app writes). Unknown originators are surfaced as
`codex_unknown`, never silently bucketed into a known source.

---

## 3. Codex Desktop project & workspace metadata

`~/.codex/.codex-global-state.json` is the desktop app's own state file. It carries
the real Codex-app-side relationships the product spec asks for — so projects do not
have to be faked from directory names:

| Key | Contents |
|---|---|
| `local-projects` | The Projects sidebar: `{id, name, rootPaths[], createdAt, updatedAt}` |
| `project-order` | Sidebar ordering, including remote (cloud) project ids |
| `thread-project-assignments` | 206 entries — thread id → `{projectKind, projectId, cwd}` |
| `thread-workspace-root-hints` | thread id → workspace root, when it differs from cwd |
| `thread-writable-roots` | Per-thread writable sandbox roots |
| `selected-project`, `active-workspace-roots` | Current desktop focus |
| `queued-follow-ups` | Messages the desktop app has queued but not yet sent |
| `codex-mobile-has-connected-device` | Whether Codex mobile remote control has ever paired |

This is read-only for AI Control. We never write this file.

---

## 4. The live-status problem, and how it is actually solved

**The failure mode to avoid:** spawning your own `codex app-server` gives you a
private process with private in-memory state. Every thread it reports reads
`status: {"type":"notLoaded"}` and `thread/loaded/list` returns `[]` — because the
threads the *desktop app* has open live inside the *desktop app's* process. A naive
implementation reports "150 sessions, all idle" forever and calls it integration.

Three routes to genuine live state were tested:

| Route | Result |
|---|---|
| `~/.codex/ipc/ipc.sock` (held by the ChatGPT process, 4 fds) | **Not app-server.** `app-server proxy --sock` connects then dies with `Broken pipe`. This is Electron-private IPC. Not used. |
| `codex app-server daemon start` (shared local daemon) | **Blocked by a missing dependency, not by design.** Errors with `managed standalone Codex install not found at ~/.codex/packages/standalone/current/codex`. Fixable via the official installer; offered as an opt-in in `setup.sh`. |
| `codex remote-control start` (official remote surface, what Codex mobile uses) | Available; currently `status: disabled` (`serverName: macbook`, `installationId: ae849c0e-…`). Left **off**; enabling it is an explicit opt-in because it opens a control channel. |

**The route that always works, and that AI Control ships as its floor:** the rollout
JSONL is written live, append-only, by whichever client owns the thread — including
the desktop app. Tailing it with a filesystem watcher yields a complete, real-time
event stream, entirely read-only, with no dependency on which process owns the
session and no coupling to a protocol revision.

Event vocabulary confirmed present in real desktop-written rollouts:

| Payload | Used for |
|---|---|
| `task_started` / `task_complete` / `turn_aborted` | `running` / `completed` / `interrupted`, turn ids, runtime |
| `user_message` / `agent_message` | The Agent tab conversation |
| `custom_tool_call` (`name:"exec"`, with `cmd` + `workdir`) | `currentAction` — "Running pytest", "Executing docker compose" |
| `patch_apply_end` | `currentAction` — "Editing inventory/api.py"; changed-file tracking |
| `mcp_tool_call_end` | Tool activity |
| `token_count` | Context usage |
| `agent_reasoning` | **Deliberately never rendered** — this is model reasoning, not user-facing output |
| `session_meta`, `turn_context`, `world_state` | Origin, model, cwd, sandbox policy |

So live status for Codex Desktop is **Supported**, via file-watching rather than via
an RPC subscription. That is a deliberate architectural choice, not a workaround: it
survives Codex updates, requires no permissions, and cannot perturb a running desktop
session.

---

## 5. Write control into a Codex Desktop thread

`ThreadResumeParams` documents the behaviour that makes this possible:

> *"If `thread_id` identifies a **running** thread, app-server **rejoins** that thread
> and treats a non-empty path as a consistency check against the active rollout path."*

Rejoin — not fork, not a new conversation. Combined with `turn/start`, `turn/steer`
(which takes an `expectedTurnId` precondition, so it can only steer the turn you
actually observed) and `turn/interrupt`, the protocol has a genuine write path into an
existing thread.

### Verified, not assumed

Continuation was tested end to end on a throwaway thread in a scratch git repository,
because advertising this capability without executing it is exactly the failure the
brief forbids:

1. `thread/start` in a temp repo → thread `01a057d5-…`; `turn/start` "reply ALPHA".
2. The app-server process was **killed**, and a **new** one started — the same
   discontinuity as a daemon restart or a different client picking the thread up.
3. `thread/resume {threadId}` → returned the *same* id. `turn/start` "reply BRAVO".

Result:

| Assertion | Outcome |
|---|---|
| Rollout files for that thread | **1** — no fork |
| Same file grew | 136,529 → 195,579 bytes |
| Both turns in one transcript | ALPHA ✓ and BRAVO ✓ |
| Any other rollout written during the test | none |
| `session_meta` records in the file | **1** — resume appends no new one |

That last row matters for attribution: because resume does not write a fresh
`session_meta`, replying to a Codex Desktop thread from AI Control **cannot** overwrite
its `originator: Codex Desktop` and cannot reclassify it out of the Codex App list. A
continued desktop session stays a desktop session.

Two things the test also settled:

- `thread/resume` **rejects the `path` parameter** unless the client negotiates the
  `experimentalApi` capability (`thread/resume.path requires experimentalApi
  capability`). `threadId` alone is the documented preferred form and is what ships.
- Our client writes `originator: ai-control` on threads **it** creates, which keeps
  AI-Control-started Codex sessions honestly distinguishable from desktop ones.

### The condition write control is gated on

Rejoin only reaches a thread running inside the app-server you are connected to.
Without the shared daemon (§4), our app-server is a different process from the desktop
app's, so a thread with a turn **currently in flight** there cannot be steered — two
writers appending to one rollout file is not something we will do to real work.

Capability is therefore decided per session on an observed condition:

- **no turn in flight** → `thread/resume` + `turn/start` continues the same thread id.
  Proven above. Advertised as available.
- **turn in flight, shared daemon reachable** → rejoin, steer, interrupt. Advertised
  only when the daemon actually answers.
- **turn in flight, no shared daemon** → `send_message: false` with the reason shown to
  the user, and *"Approval required on Mac"* for approvals. No CLI conversation is
  started and passed off as the desktop thread.

The liveness test is the presence of an open turn (`task_started` with no matching
`task_complete`), deliberately **independent of the staleness heuristic**: a desktop
thread that has been thinking for four minutes still owns its rollout, and treating
"quiet" as "free to write" would have produced exactly the double-writer case above.

## 6. Capability matrix

Legend: **S** supported and implemented · **E** experimental / opt-in · **U** unsupported · **?** unknown, not yet provable

### Codex Desktop (`codex_desktop`)

| Operation | State | Mechanism |
|---|---|---|
| `discoverSessions` | **S** | `thread/list` + rollout scan, `originator == "Codex Desktop"` |
| `getSession` / `getConversation` | **S** | `thread/read {includeTurns: true}`; rollout parse |
| `getProjects` | **S** | `local-projects` + `thread-project-assignments` |
| `getWorkingDirectory` / `getRepository` / `getBranch` | **S** | `thread.cwd`, `thread.gitInfo{branch,sha,originUrl}` |
| `getDiff` | **S** | GitService against the discovered cwd/worktree |
| `getStatus` / `streamUpdates` | **S** | rollout file watch + event inference |
| `resume` / `sendMessage` (no turn in flight) | **S** | `thread/resume` → `turn/start`, same thread id — verified end to end, §5 |
| `sendMessage` (turn in flight in the desktop app) | **?** | Needs the shared daemon, which is not installed here, so this path is **untested**. Advertised as unavailable until it can be proven. |
| `interrupt` | **?** | `turn/interrupt` exists; same untested precondition |
| `steer` | **?** | `turn/steer` with `expectedTurnId`; same untested precondition |
| `getPermissionRequest` / approve | **?** | `ExecCommandApproval`, `ApplyPatchApproval`, `PermissionsRequestApproval` are server→client requests, reachable only on a thread we host. Untested on this machine. |
| Start a new session *inside the desktop app* | **U** | No documented mechanism. UI states this plainly. |
| GUI / Accessibility automation | **not used** | Unnecessary — a supported API exists |

### Codex CLI (`codex_cli`)
`thread/list`/`read`/`resume`, `turn/*`, `command/exec*`, approvals, `review/start`
— all **S**, because we own the app-server process hosting them. External CLI
sessions started outside AI Control are discovered from the same rollout store
(`originator: codex-tui`) — **S** for read, resume-on-idle for write.

### Claude Code (`claude_code`)
Sessions in `~/.claude/projects/<slug>/<uuid>.jsonl`; resume via `claude --resume`;
streaming via `--output-format stream-json`. All **S**. External discovery **S**.
Notably Codex itself already imports these (`externalAgentConfig/import`,
`~/.codex/external_agent_session_imports.json`) — that mapping is read to
**deduplicate**, so one Claude session that Codex has imported is not shown twice.

### Forgejo
REST API, token held server-side only. **S**.

---

## 7. Open / unknown

| Question | Status |
|---|---|
| Does the desktop app attach to the shared managed daemon once the standalone install exists? | **?** — untestable until the standalone install is present. `setup.sh` offers it; `/diagnostics` reports the answer at runtime. |
| Does `remote-control start` surface desktop threads as `loaded`? | **?** — requires explicit opt-in; not enabled without the user's say-so. |
| Is `originator` stable across Codex releases? | Treated as unstable. Unknown values degrade to `codex_unknown` rather than being guessed. |
| Do `turn/steer`, `turn/interrupt` and remote approvals work against a live desktop thread? | **?** — requires the shared daemon. The code paths exist and are gated off until `/diagnostics` observes the daemon answering. |

## 8. What we deliberately did not do

- No invented API endpoints.
- No calls to authenticated OpenAI cloud endpoints to impersonate the desktop client.
- No AppleScript, no Accessibility API, no screen scraping, no coordinate clicking.
  A supported local API exists, so the last-resort tier in the brief was never reached.
- No writing to `~/.codex/.codex-global-state.json` or the rollout store.
- No labelling a freshly-spawned CLI process as "Codex App".
