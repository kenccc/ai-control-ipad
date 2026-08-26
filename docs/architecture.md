# Architecture

```
iPad (PWA, React + TypeScript)
    │  HTTPS + WebSocket over Tailscale
    ▼
Mac Agent Control Server  (FastAPI, launchd: com.aicontrol.agent)
    │
    ├── SessionRegistry ──── reconciles every 2s, deduplicates, emits deltas
    │       ├── CodexDesktopProvider   ← ~/.codex rollout store + app-server + app state
    │       ├── CodexCLIProvider       ← same store, separated by `originator`
    │       └── ClaudeCodeProvider     ← ~/.claude/projects/*/*.jsonl
    │
    ├── GitService        git CLI, per-directory caches
    ├── ForgejoProvider   REST, token server-side only
    ├── PTYService        pty.fork + WebSocket, allowlisted directories
    ├── WorktreeManager   one worktree per independently-writing agent
    ├── EventBus          in-process pub/sub → /api/stream
    └── Database          SQLite (WAL)

The Mac holds every repository, every secret, and every agent process.
The iPad holds a rendering of them.
```

## Why discovery is file-first

The product's hardest requirement is that a Codex Desktop task started by hand shows
up on the iPad. That rules out any design where AI Control must have created the
session.

Codex Desktop, Codex CLI and the VS Code extension all append to the same rollout
store (`~/.codex/sessions/**/rollout-*.jsonl`) as they work. Reading it gives a live
view of any session regardless of which process owns it, needs no permissions, cannot
perturb a running agent, and does not break when Codex updates its RPC protocol. So
discovery and status are file-first, and the `codex app-server` JSON-RPC API is used
for the things files cannot do: continuing a thread, interrupting a turn, answering an
approval.

Claude Code is symmetrical: `~/.claude/projects/<slug>/<uuid>.jsonl` for discovery and
transcripts, `claude --resume <id>` for continuation.

### Cost of the naive version

A first cut parsed every rollout in full on every reconcile: **9.2 s** per pass over
150 Codex sessions, against a 2-second interval. Three changes fixed it:

| Change | Reason |
|---|---|
| Tail reads (256 KB) for status | Status depends only on recent events; transcripts run to megabytes |
| `(path, mtime)` parse cache | A file that has not been written to cannot have changed state |
| Per-directory git caches | 150 sessions in one repo ran the same `git diff` 150 times |

Result: **0.57 s** for 342 sessions across all three providers, warm. Full transcripts
are parsed only when a session is actually opened.

## Source attribution

`SessionSource` in the Codex protocol is `cli | vscode | exec | appServer | unknown`.
There is no desktop member, and **Codex Desktop reports `vscode`**. Attribution
therefore comes from `session_meta.originator` on the first line of each rollout
(`Codex Desktop`, `codex-tui`, …), corroborated by `thread-project-assignments` in
`~/.codex/.codex-global-state.json`, which only the Electron app writes.

An unrecognised originator becomes `codex_unknown` and is shown as plain "Codex". It
is never guessed into `Codex App`.

## Capability gating

`Capabilities` is computed **per session**, not per provider, because two Codex Desktop
threads differ: one idle on disk can be continued, one with a turn in flight cannot.
The UI renders controls from that object alone, so a button never appears for an
operation that would fail. When a write is unavailable, the provider supplies the
reason and the UI shows it verbatim.

The gate for Codex Desktop is the presence of an open turn — `task_started` with no
matching `task_complete` — deliberately independent of the staleness heuristic. A
thread that has been thinking for four minutes still owns its rollout file; treating
"quiet" as "free to write" would put two writers on one file.

## Deduplication

Sessions are merged only on stable identity: `(provider, external_session_id)`, or an
explicit import mapping such as `~/.codex/external_agent_session_imports.json`, which
records Claude Code sessions that Codex has imported. When two providers expose the
same session, the more capable view wins so control is never lost. **Titles are never
used for matching** — two agents given the same instruction produce near-identical
titles and are still two different sessions.

## Real-time

One WebSocket at `/api/stream` carries `session.discovered`, `session.status`,
`session.git_changed`, `session.permission`, `session.completed`, `session.failed`,
`session.removed`, plus a 20-second `ping` that doubles as the latency measurement
shown in the sidebar. Terminals get their own binary socket per PTY. Transcript text
is fetched over REST when a session is open, so the event socket stays small.

Reconnection uses exponential backoff and reconnects immediately on
`visibilitychange`, because iPadOS suspends sockets when the app is backgrounded —
that is the normal case, not an error.

## Concurrency safety

- One worktree per independently-writing agent, under `~/.ai-control/worktrees/`.
- Starting an agent in a directory another agent is registered as writing in is
  refused with a 409 naming the holder.
- Continuing a Codex thread that has a turn in flight elsewhere is refused.
- Approvals default to **denied** if no handler is attached. Nothing auto-approves.
