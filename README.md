# AI Control

A self-hosted command center that turns an iPad into a supervision console for the AI
coding agents running on your Mac — **Codex Desktop**, **Codex CLI** and **Claude
Code** — alongside Forgejo issues, pull requests, repository diffs and review flows.

The Mac does all the work. The iPad assigns, observes, redirects, reviews and approves.

## What makes this more than a terminal wrapper

Codex Desktop is a first-class provider, not an afterthought. A task you start by hand
in the Codex desktop app appears on the iPad automatically, with its real title, its
Codex project, its repository, the branch it was working on, its transcript and its
diff — through officially supported interfaces, with no GUI automation.

The three sources are never conflated. A Codex CLI process is never labelled "Codex
App", and every control the UI shows is backed by a capability the provider has
actually verified it can perform.

See **[docs/integration-research.md](docs/integration-research.md)** for exactly what
each integration can and cannot do, and how each claim was proven.

## Quick start

```bash
./setup.sh
```

Then open the printed Tailscale URL on your iPad and add it to the Home Screen.
Full instructions in **[docs/setup.md](docs/setup.md)**.

## Docs

| | |
|---|---|
| [integration-research.md](docs/integration-research.md) | What Codex Desktop, Codex CLI and Claude Code actually support, measured |
| [architecture.md](docs/architecture.md) | How discovery, capability gating and reconciliation work |
| [security.md](docs/security.md) | Auth, CSRF, allowlist, secrets, audit, chain-of-thought handling |
| [setup.md](docs/setup.md) | Install, configure, Tailscale, and what works while the Mac sleeps |

## Layout

```
apps/server/aicontrol/     FastAPI daemon
  codex/                   rollout store, app-server JSON-RPC, desktop app state
  providers/               CodexDesktop · CodexCLI · ClaudeCode
  services/                git · forgejo · pty · worktrees · review
  api/                     routes
apps/web/                  React + TypeScript PWA
macos/launchd/             LaunchAgent template
scripts/                   secrets, LaunchAgent, optional Codex daemon
```

## Tests

```bash
.venv/bin/python -m pytest apps/server/tests -q   # 50 tests
cd apps/web && npx vitest run                     #  8 tests
```
