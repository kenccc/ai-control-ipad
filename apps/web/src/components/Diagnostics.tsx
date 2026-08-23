import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { Diagnostics as Diag } from '../lib/types'

export function DiagnosticsView() {
  const [data, setData] = useState<Diag | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const load = () => api.diagnostics().then(setData).catch((e) => setError(String(e)))
    load()
    const timer = window.setInterval(load, 10000)
    return () => window.clearInterval(timer)
  }, [])

  if (error) return <div className="main"><div className="notice error">{error}</div></div>
  if (!data) return <div className="main"><div className="notice info">Loading diagnostics…</div></div>

  const desktop = data.codexDesktop ?? {}
  const caps = desktop.capabilities ?? {}

  return (
    <div className="main">
      <div className="toolbar"><h1>Diagnostics</h1></div>
      <div className="pane-body">
        <Section title="Mac agent">
          <Item label="Server" ok={data.server.ok} detail={`${data.server.wsSubscribers} clients connected`} />
          <Item label="Reconciliation" ok={!data.server.reconcileError}
                detail={data.server.reconcileError
                  ?? (data.server.lastReconcile
                    ? `last run ${Math.round(Date.now() / 1000 - data.server.lastReconcile)}s ago`
                    : 'not run yet')} />
          <Item label="Git" ok={data.git.detected} detail={data.git.detected ? 'detected' : 'not found'} />
          <Item label="Tailscale" ok={Boolean(data.tailscale.connected)}
                detail={data.tailscale.connected
                  ? `${data.tailscale.dnsName ?? ''} ${(data.tailscale.addresses ?? []).join(' ')}`
                  : (data.tailscale.hint ?? data.tailscale.error ?? 'not connected')} />
        </Section>

        <Section title="Codex Desktop">
          <Item label="Detected" ok={Boolean(desktop.ok)}
                detail={desktop.binary ?? 'no codex binary found'} />
          <Item label="Core version" ok={Boolean(desktop.version)}
                detail={`${desktop.version ?? '—'}${desktop.desktopBundled ? ' (bundled in the app)' : ''}`} />
          <Item label="Session store" ok={Boolean(desktop.sessionsDirPresent)} detail={desktop.sessionsDir} />
          <Item label="App state file" ok={Boolean(desktop.globalStateAvailable)}
                detail={desktop.globalStateAvailable ? `${desktop.projects ?? 0} projects` : 'not readable'} />
          <Item label="Integration mode" ok detail={String(desktop.writeControl ?? 'read-only')} />
          <div style={{ marginTop: 10 }}>
            <h4 style={{ fontSize: 10, letterSpacing: '.08em', textTransform: 'uppercase',
                         color: 'var(--text-faint)', margin: '0 0 6px' }}>
              Codex Desktop capabilities
            </h4>
            <div className="caps">
              <Cap on={caps.discoverSessions} label="Discover sessions" />
              <Cap on={caps.readConversation} label="Read conversation" />
              <Cap on={caps.readGitChanges} label="Read git changes" />
              <Cap on={caps.continueIdleSession} label="Continue an idle session" />
              <Cap on={caps.continueLiveSession} label="Continue a session with a turn in flight" />
              <Cap on={caps.interrupt} label="Interrupt" />
              <Cap on={caps.approveActions} label="Approve actions remotely" />
              <Cap on={caps.startNewSessionInApp} label="Start a new session inside the app" />
            </div>
          </div>
          {!caps.continueLiveSession && (
            <div className="notice" style={{ margin: '10px 0 0' }}>
              Continuing a Codex Desktop session that has a turn in flight needs the shared
              Codex app-server daemon, which is not installed.
              Run <code className="mono">./scripts/enable-codex-daemon.sh</code> on the Mac to add it.
              Everything else — discovery, reading, diffs, and replying to idle
              sessions — works without it.
            </div>
          )}
        </Section>

        <Section title="Codex CLI">
          <Item label="Detected" ok={Boolean(data.codexCli.ok)} detail={data.codexCli.binary ?? '—'} />
          <Item label="Version" ok={Boolean(data.codexCli.version)} detail={data.codexCli.version ?? '—'} />
        </Section>

        <Section title="Claude Code">
          <Item label="Detected" ok={Boolean(data.claudeCode.ok)} detail={data.claudeCode.binary ?? '—'} />
          <Item label="Version" ok={Boolean(data.claudeCode.version)} detail={data.claudeCode.version ?? '—'} />
          <Item label="Session store" ok={Boolean(data.claudeCode.projectsDirPresent)}
                detail={data.claudeCode.projectsDir} />
        </Section>

        <Section title="Forgejo">
          <Item label="Configured" ok={Boolean(data.forgejo.configured)}
                detail={data.forgejo.url ?? data.forgejo.hint ?? '—'} />
          <Item label="Connected" ok={Boolean(data.forgejo.connected)}
                detail={data.forgejo.connected ? `as ${data.forgejo.user}`
                  : (data.forgejo.error ?? 'not connected')} />
        </Section>

        <Section title="Installed Codex cores">
          {data.codexBinaries.map((binary) => (
            <div key={binary.path} className="kv">
              <span className="k mono">{binary.version}{binary.desktopBundled ? ' · desktop' : ''}</span>
              <span className="v mono">{binary.path}</span>
            </div>
          ))}
        </Section>

        <Section title="Sessions">
          {Object.entries(data.sessionCounts).map(([source, count]) => (
            <div key={source} className="kv">
              <span className="k">{source}</span><span className="v">{count}</span>
            </div>
          ))}
          <div className="kv"><span className="k">Active</span><span className="v">{data.activeSessions}</span></div>
          <div className="kv"><span className="k">Open terminals</span><span className="v">{data.terminals}</span></div>
        </Section>
      </div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return <div className="context-section"><h3>{title}</h3>{children}</div>
}

function Item({ label, ok, detail }: { label: string; ok: boolean; detail?: string }) {
  return (
    <div className="kv">
      <span className="k row" style={{ gap: 7 }}>
        <span className={`dot ${ok ? 'running' : 'failed'}`} style={{ animation: 'none' }} />
        {label}
      </span>
      <span className="v mono" title={detail}>{detail}</span>
    </div>
  )
}

function Cap({ on, label }: { on: boolean | undefined; label: string }) {
  return (
    <div className={`cap ${on ? 'on' : 'off'}`}>
      <span className="mark">{on ? '✓' : '✗'}</span><span>{label}</span>
    </div>
  )
}
