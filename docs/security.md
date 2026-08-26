# Security

AI Control can run commands on a development Mac. It is built to be safe when exposed
only to a private network, and to still be safe if that assumption fails.

## Authentication

- **Mandatory.** `AuthService` raises at startup if no token is configured; there is no
  unauthenticated mode and no "trusted network" bypass.
- The token is compared with `hmac.compare_digest`.
- A successful login issues an HMAC-SHA256-signed session cookie: `HttpOnly`,
  `SameSite=Strict`, `Secure` when served over HTTPS, 30-day expiry (an iPad
  Home Screen app should not be logged out daily).
- Login is rate limited to 10 attempts per 5 minutes per client; failures are audited.

## CSRF

Every mutating request requires a double-submit token: a non-`HttpOnly` cookie plus a
matching `x-aicontrol-csrf` header, compared with `compare_digest`. Reads need the
session cookie only.

## WebSocket origin validation

WebSocket upgrades bypass CORS, so both sockets validate by hand before accepting:
the session cookie must verify **and** the `Origin` must be allowlisted, `localhost`,
`*.ts.net`, or inside Tailscale's `100.64.0.0/10`. Anything else is closed with 4403.

## Repository allowlist

Configured in `~/.ai-control/config.yaml`. Every agent launch, terminal, and git
operation resolves its path and checks it against the allowlist.

**An empty allowlist allows nothing.** Failing closed is the only safe default for
something that can run commands. Paths are resolved before comparison, so `..`
traversal cannot escape.

## Secrets

| Secret | Storage | Reaches the browser |
|---|---|---|
| Access token | macOS Keychain (`ai-control` service) | no |
| Session signing key | macOS Keychain | no |
| Forgejo API token | macOS Keychain | **no** — all Forgejo calls are proxied |
| OpenAI / Anthropic credentials | owned by Codex and Claude Code, never read by AI Control | no |

`Config.to_dict()` — the only config that reaches the browser — is asserted secret-free
by a test. Logs are passed through a redactor that strips anything resembling an
`Authorization` header, a token assignment or an `sk-` key.

## Agent security controls are not silently bypassed

Approval requests are surfaced, not answered on your behalf. With no handler attached
the app-server client replies **denied**. Approval policy and sandbox policy are
displayed as the agent recorded them; AI Control never quietly lowers them.

Where an approval can only be granted in the Codex desktop app, the UI says
*"Approval required on Mac"* rather than presenting a control that does nothing.

### Unattended mode

The **Bypass permissions** toggle on the New Agent screen runs an agent in its own
documented unattended mode:

| Provider | What is set |
|---|---|
| Codex CLI | `approvalPolicy: never` **and** `sandbox: danger-full-access` |
| Claude Code | `--permission-mode bypassPermissions` |

Both halves are set together for Codex deliberately: turning approvals off while
leaving a restrictive sandbox produces an agent that still stalls, which is worse than
not offering the mode at all.

Constraints on it:

- **Never a default.** It is off unless chosen for that specific session, and choosing
  it disables the approval-policy buttons so the two cannot disagree.
- **Explained before it is used.** The UI states that the agent gets full disk and
  network access inside the repository and will not stop to ask.
- **Audited twice** — in the `session_created` entry and as its own
  `permissions_bypassed` row, so it is greppable on its own — plus an activity entry.
- **Still confined to the repository allowlist.** Bypassing approvals does not widen
  which directories an agent may be started in.
- **Visible afterwards.** A session running this way carries a `no approvals` badge in
  its header, derived from what the agent itself recorded rather than from what we
  asked for — so a session started unattended outside AI Control is labelled just as
  accurately.

## Rendering untrusted content

Agent output is model-generated and Forgejo issues and comments are written by other
people, so all of it is treated as untrusted. Markdown is parsed, then URLs are
rewritten, then the result is sanitized with DOMPurify — sanitization runs last and
unconditionally. Scripts, event handlers, `javascript:` URLs, `<iframe>`, `<svg>`,
`<style>` and inline `style` attributes are all stripped; tests assert each one.

External links get `rel="noopener noreferrer nofollow"` and open in a new tab.

## Forgejo attachment proxy

Forgejo serves `/attachments/<uuid>` only to authenticated callers, and the API token
must never reach the browser — so images in issues cannot be loaded by an `<img>` tag
directly. `GET /api/forgejo/attachment/{id}` fetches them server-side instead:

- It takes **only an attachment UUID**, never a caller-supplied URL, and builds the
  request from the configured base URL. It cannot be used as an SSRF primitive.
- The UUID is regex-validated, so path traversal is impossible by construction.
- **Only image content types are returned.** Proxying attacker-supplied HTML or SVG
  from our own origin would be a same-origin scripting hole, so those are refused with
  415 rather than sanitized.
- Responses carry `nosniff`, `Content-Disposition: inline` with no passed-through
  filename, and `default-src 'none'; sandbox`.
- Size is capped at 25 MB, and a session is required like any other endpoint.

Images that are not Forgejo attachments are not proxied: the page CSP blocks remote
images, and the renderer degrades them to a link rather than leaving a broken image
that would also leak the viewer's IP to a third party.

## Audit log

Recorded to SQLite with timestamp, actor and session: session created, message sent,
agent interrupted/resumed/terminated, terminal opened, command executed, worktree
created/removed, review sent, Forgejo comment submitted, login, login failed.

## Chain-of-thought is never exposed

Codex `agent_reasoning` / `reasoning` and Claude `thinking` / `redacted_thinking`
blocks are dropped **at the parser**, before they can reach the API or the browser.
Two tests assert that reasoning text does not appear in any produced event.

## Transport

Intended to run behind Tailscale. Setting `host: tailscale` binds this machine's
tailnet address only: reachable from your other tailnet devices, not from the local
network, and not on loopback. If Tailscale is not up, the server **refuses to start**
rather than falling back to `0.0.0.0` — a silent fallback would widen exposure at
exactly the moment the operator believes it is narrow.

`host` otherwise takes a literal address and defaults to `127.0.0.1`; binding
`0.0.0.0` on an untrusted network is your decision, and the auth, CSRF and origin
checks above all still apply. Responses carry `X-Content-Type-Options`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and a CSP that forbids
external script and frame sources.

## What is deliberately absent

No AppleScript, no Accessibility automation, no screen scraping, no synthetic clicks.
A supported local API exists, so the brief's last-resort tier was never needed.
AI Control never writes to `~/.codex/.codex-global-state.json` or the rollout store.
