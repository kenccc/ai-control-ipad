import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { useStore } from '../lib/store'
import type { CodexProject, ProviderUsage, Session } from '../lib/types'
import { Dot, DiffBadge, SourceTag, relativeTime, statusText } from './common'
import { meterTone } from './Usage'

export type View =
  | { kind: 'sessions' }
  | { kind: 'session'; id: string }
  | { kind: 'issues' }
  | { kind: 'issue'; number: number }
  | { kind: 'pulls' }
  | { kind: 'pull'; number: number }
  | { kind: 'repos' }
  | { kind: 'repo'; name: string }
  | { kind: 'activity' }
  | { kind: 'diagnostics' }
  | { kind: 'usage' }
  | { kind: 'project'; id: string }

interface Props {
  view: View
  onNavigate: (view: View) => void
  onNewAgent: () => void
}

export function Nav({ view, onNavigate, onNewAgent }: Props) {
  const { sessions, connection, latencyMs } = useStore()
  const [projects, setProjects] = useState<CodexProject[]>([])
  const [usage, setUsage] = useState<ProviderUsage[]>([])
  const [showAll, setShowAll] = useState(false)

  useEffect(() => {
    api.codexProjects().then((d) => setProjects(d.projects)).catch(() => {})
    const loadUsage = () =>
      api.usage().then((d) => setUsage(d.providers)).catch(() => {})
    loadUsage()
    const timer = window.setInterval(loadUsage, 60_000)
    return () => window.clearInterval(timer)
  }, [])

  const active = sessions.filter((s) => s.isActive)
  const recent = sessions.filter((s) => !s.isActive)
  const shown = showAll ? recent : recent.slice(0, 12)
  const currentId = view.kind === 'session' ? view.id : null

  return (
    <nav className="nav">
      <div className="nav-head">
        <span className="brand">AI Control</span>
        <button className="btn small" onClick={onNewAgent} title="New agent (⌘N)">+ Agent</button>
      </div>

      <div className="grow scroll">
        {active.length > 0 && (
          <div className="nav-section">
            <div className="nav-label">Active <span>{active.length}</span></div>
            {active.map((s) => (
              <SessionRow key={s.id} session={s} current={s.id === currentId}
                          onClick={() => onNavigate({ kind: 'session', id: s.id })} />
            ))}
          </div>
        )}

        <div className="nav-section">
          <div className="nav-label">
            Recent
            {recent.length > 12 && (
              <button className="btn ghost small" onClick={() => setShowAll((v) => !v)}>
                {showAll ? 'Less' : `All ${recent.length}`}
              </button>
            )}
          </div>
          {shown.map((s) => (
            <SessionRow key={s.id} session={s} current={s.id === currentId}
                        onClick={() => onNavigate({ kind: 'session', id: s.id })} />
          ))}
          {sessions.length === 0 && (
            <div className="faint" style={{ padding: '6px 8px', fontSize: 12 }}>
              No agent sessions found yet.
            </div>
          )}
        </div>

        {projects.length > 0 && (
          <div className="nav-section">
            <div className="nav-label">Codex Projects</div>
            {projects.slice(0, 10).map((p) => (
              <button key={p.id} className="nav-item"
                      aria-current={view.kind === 'project' && view.id === p.id}
                      onClick={() => onNavigate({ kind: 'project', id: p.id })}>
                <span className="truncate">{p.name}</span>
                <span className="count">{p.sessions.length}</span>
              </button>
            ))}
          </div>
        )}

        <div className="nav-section">
          <div className="nav-label">Forgejo</div>
          <button className="nav-item" aria-current={view.kind === 'issues'}
                  onClick={() => onNavigate({ kind: 'issues' })}>Issues</button>
          <button className="nav-item" aria-current={view.kind === 'pulls'}
                  onClick={() => onNavigate({ kind: 'pulls' })}>Pull Requests</button>
          <button className="nav-item" aria-current={view.kind === 'repos'}
                  onClick={() => onNavigate({ kind: 'repos' })}>Repositories</button>
        </div>

        <div className="nav-section">
          <div className="nav-label">System</div>
          <button className="nav-item" aria-current={view.kind === 'activity'}
                  onClick={() => onNavigate({ kind: 'activity' })}>Activity</button>
          <button className="nav-item" aria-current={view.kind === 'usage'}
                  onClick={() => onNavigate({ kind: 'usage' })}>Usage</button>
          <button className="nav-item" aria-current={view.kind === 'diagnostics'}
                  onClick={() => onNavigate({ kind: 'diagnostics' })}>Diagnostics</button>
        </div>
      </div>

      <UsageGauge usage={usage} onOpen={() => onNavigate({ kind: 'usage' })} />

      <div className="conn">
        <Dot status={connection} />
        <span>MacBook</span>
        <span className="faint" style={{ marginLeft: 'auto' }}>
          {connection === 'online'
            ? (latencyMs !== null ? `${latencyMs} ms` : 'connected')
            : connection === 'connecting' ? 'connecting…' : 'offline'}
        </span>
      </div>
    </nav>
  )
}

/** The tightest window per provider, so limits are visible without navigating. */
function UsageGauge({ usage, onOpen }:
                    { usage: ProviderUsage[]; onOpen: () => void }) {
  const rows = usage
    .map((p) => ({
      label: p.label,
      // The window closest to its ceiling is the one that will stop you first.
      window: p.windows.slice().sort((a, b) => b.usedPercent - a.usedPercent)[0],
      plan: p.plan,
    }))
    .filter((r) => r.window)
  if (rows.length === 0) return null

  return (
    <button className="nav-usage" onClick={onOpen} style={{ background: 'none', border: 0,
            borderTop: '1px solid var(--border)', cursor: 'pointer', textAlign: 'left' }}>
      {rows.map((row) => {
        const pct = Math.max(0, Math.min(100, row.window!.usedPercent))
        return (
          <div key={row.label} className="nav-usage-row">
            <span className="who truncate">{row.label}</span>
            <div className="meter">
              <div className={`meter-fill ${meterTone(pct)}`} style={{ width: `${pct}%` }} />
            </div>
            <span className="pct">{pct.toFixed(0)}%</span>
          </div>
        )
      })}
    </button>
  )
}

function SessionRow({ session, current, onClick }:
                    { session: Session; current: boolean; onClick: () => void }) {
  return (
    <button className="session-item" aria-current={current} onClick={onClick}>
      <div className="line1">
        <Dot status={session.status} />
        <SourceTag session={session} />
        <span className="title truncate">{session.title ?? 'Untitled session'}</span>
      </div>
      <div className="line2">
        <span className="truncate">{session.repository ?? '—'}</span>
        <span className="truncate">{statusText(session)}</span>
        <span style={{ marginLeft: 'auto' }}>{relativeTime(session.lastActivity)}</span>
      </div>
    </button>
  )
}

export { DiffBadge }
