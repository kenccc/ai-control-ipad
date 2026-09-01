import { useCallback, useEffect, useState } from 'react'
import { useStore } from './lib/store'
import { Nav, type View } from './components/Nav'
import { SessionView } from './components/SessionView'
import { ContextPanel } from './components/ContextPanel'
import { CommandPalette } from './components/CommandPalette'
import { NewAgent } from './components/NewAgent'
import { DiagnosticsView } from './components/Diagnostics'
import { UsageView } from './components/Usage'
import {
  ActivityView, IssuesView, ProjectView, PullView, PullsView, RepoView, ReposView,
} from './components/Lists'
import { IssueDetail } from './components/IssueDetail'
import { Empty } from './components/common'

export function App() {
  const { authenticated, sessionsById, sessions, notifications, dismiss, login } = useStore()
  const [view, setView] = useState<View>({ kind: 'sessions' })
  const [palette, setPalette] = useState<'command' | 'switcher' | null>(null)
  const [newAgentIssue, setNewAgentIssue] = useState<number | null | undefined>(undefined)
  const [navOpen, setNavOpen] = useState(false)

  const navigate = useCallback((next: View) => {
    setView(next)
    setNavOpen(false)
  }, [])

  const openNewAgent = useCallback((issue: number | null = null) => setNewAgentIssue(issue), [])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const meta = event.metaKey || event.ctrlKey
      if (meta && event.key.toLowerCase() === 'k') { event.preventDefault(); setPalette('command') }
      else if (meta && event.key.toLowerCase() === 'p') { event.preventDefault(); setPalette('switcher') }
      else if (meta && event.key.toLowerCase() === 'n') { event.preventDefault(); openNewAgent(null) }
      else if (meta && event.key === '\\') { event.preventDefault(); setNavOpen((v) => !v) }
      else if (event.key === 'Escape') { setPalette(null) }
      // ⌘1..⌘9 jumps between agents without leaving the keyboard.
      else if (meta && /^[1-9]$/.test(event.key)) {
        const target = sessions[Number(event.key) - 1]
        if (target) { event.preventDefault(); navigate({ kind: 'session', id: target.id }) }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [sessions, navigate, openNewAgent])

  useEffect(() => {
    if (authenticated && 'Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission().catch(() => {})
    }
  }, [authenticated])

  if (!authenticated) return <Login onLogin={login} />

  const session = view.kind === 'session' ? sessionsById[view.id] : null

  return (
    <div className={`app${session ? ' with-context' : ''}${navOpen ? ' nav-open' : ''}`}>
      <Nav view={view} onNavigate={navigate} onNewAgent={() => openNewAgent(null)} />

      {view.kind === 'sessions' && <Dashboard onNavigate={navigate} onNewAgent={() => openNewAgent(null)} />}
      {view.kind === 'session' && (session
        ? <SessionView session={session} />
        : <div className="main"><Empty title="That session is no longer available"
                                      hint="It may have been archived or removed." /></div>)}
      {view.kind === 'issues' && (
        <IssuesView onOpen={(n) => navigate({ kind: 'issue', number: n })}
                    onStartAgent={(n) => openNewAgent(n)} />)}
      {view.kind === 'issue' && <IssueDetail number={view.number} onNavigate={navigate}
                                             onStartAgent={() => openNewAgent(view.number)} />}
      {view.kind === 'pulls' && <PullsView onOpen={(n) => navigate({ kind: 'pull', number: n })} />}
      {view.kind === 'pull' && <PullView number={view.number} onNavigate={navigate} />}
      {view.kind === 'repos' && <ReposView onOpen={(name) => navigate({ kind: 'repo', name })} />}
      {view.kind === 'repo' && <RepoView name={view.name} onNavigate={navigate} />}
      {view.kind === 'activity' && <ActivityView />}
      {view.kind === 'usage' && <UsageView />}
      {view.kind === 'diagnostics' && <DiagnosticsView />}
      {view.kind === 'project' && <ProjectView id={view.id} onNavigate={navigate} />}

      {session && <ContextPanel session={session} />}

      {palette && (
        <CommandPalette mode={palette} onClose={() => setPalette(null)}
                        onNavigate={navigate} onNewAgent={() => openNewAgent(null)} />
      )}
      {newAgentIssue !== undefined && (
        <NewAgent issue={newAgentIssue} onClose={() => setNewAgentIssue(undefined)}
                  onCreated={(id) => { setNewAgentIssue(undefined); navigate({ kind: 'session', id }) }} />
      )}

      <div className="toasts">
        {notifications.map((notice) => (
          <div key={notice.id} className={`toast ${notice.kind}`} onClick={() => dismiss(notice.id)}>
            <span className="grow">{notice.text}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function Dashboard({ onNavigate, onNewAgent }:
                   { onNavigate: (v: View) => void; onNewAgent: () => void }) {
  const { sessions, lastReconcile } = useStore()
  const active = sessions.filter((s) => s.isActive)
  const recent = sessions.filter((s) => !s.isActive).slice(0, 40)

  return (
    <div className="main">
      <div className="toolbar">
        <h1>Sessions</h1>
        <span className="faint" style={{ fontSize: 11.5 }}>
          {active.length} active · {sessions.length} total
          {lastReconcile && ` · synced ${Math.round(Date.now() / 1000 - lastReconcile)}s ago`}
        </span>
        <button className="btn small primary" style={{ marginLeft: 'auto' }} onClick={onNewAgent}>
          + New agent
        </button>
      </div>
      <div className="pane-body">
        {sessions.length === 0 && (
          <Empty title="No agent sessions found"
                 hint="Start a task in Codex Desktop, Codex CLI or Claude Code on your Mac and it will appear here automatically." />
        )}
        {active.length > 0 && <GroupHeader label={`Active — ${active.length}`} />}
        {active.map((s) => <Card key={s.id} session={s} onOpen={() => onNavigate({ kind: 'session', id: s.id })} />)}
        {recent.length > 0 && <GroupHeader label="Recent" />}
        {recent.map((s) => <Card key={s.id} session={s} onOpen={() => onNavigate({ kind: 'session', id: s.id })} />)}
      </div>
    </div>
  )
}

function GroupHeader({ label }: { label: string }) {
  return (
    <div style={{ padding: '14px 16px 6px', fontSize: 10, fontWeight: 650,
                  letterSpacing: '.08em', textTransform: 'uppercase', color: 'var(--text-faint)' }}>
      {label}
    </div>
  )
}

import { Dot, DiffBadge, SourceTag, relativeTime } from './components/common'
import type { Session } from './lib/types'

function Card({ session, onOpen }: { session: Session; onOpen: () => void }) {
  return (
    <button className="list-row" onClick={onOpen}>
      <div className="col grow" style={{ gap: 4 }}>
        <div className="row">
          <Dot status={session.status} />
          <SourceTag session={session} />
          <span className="truncate" style={{ fontWeight: 500 }}>
            {session.title ?? 'Untitled session'}
          </span>
          <span className="faint" style={{ marginLeft: 'auto', flex: 'none', fontSize: 11.5 }}>
            {relativeTime(session.lastActivity)}
          </span>
        </div>
        <div className="row" style={{ gap: 12, fontSize: 12 }}>
          <span className="mono dim">{session.repository ?? '—'}</span>
          {session.branch && <span className="mono faint truncate">{session.branch}</span>}
          {session.forgejoIssue && <span className="faint">#{session.forgejoIssue}</span>}
        </div>
        <div className="row" style={{ gap: 12, fontSize: 12 }}>
          <span className="dim truncate">{session.currentAction ?? session.status}</span>
          <span style={{ marginLeft: 'auto', flex: 'none' }}><DiffBadge session={session} /></span>
        </div>
      </div>
    </button>
  )
}

function Login({ onLogin }: { onLogin: (token: string) => Promise<void> }) {
  const [token, setToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  return (
    <div className="login">
      <form onSubmit={async (event) => {
        event.preventDefault()
        setBusy(true)
        try { await onLogin(token) }
        catch { setError('That token was not accepted.') }
        finally { setBusy(false) }
      }}>
        <h1>AI Control</h1>
        <input className="input" type="password" autoFocus value={token}
               placeholder="Access token" autoComplete="current-password"
               onChange={(e) => setToken(e.target.value)} />
        {error && <div className="notice error" style={{ margin: 0 }}>{error}</div>}
        <button className="btn primary" type="submit" disabled={busy || !token}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
        <div className="faint" style={{ fontSize: 11.5, textAlign: 'center', lineHeight: 1.5 }}>
          The token is stored in the macOS Keychain on your Mac.
          Run <code className="mono">./setup.sh</code> if you do not have one.
        </div>
      </form>
    </div>
  )
}
