import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { useStore } from '../lib/store'
import type { Session } from '../lib/types'
import { AgentTab } from './AgentTab'
import { ChangesTab } from './ChangesTab'
import { TerminalTab } from './TerminalTab'
import { GitTab } from './GitTab'
import { IssueTab } from './IssueTab'
import { Dot, DiffBadge, SourceTag, relativeTime } from './common'

type Tab = 'agent' | 'terminal' | 'changes' | 'issue' | 'git'

/** True when the agent was started in a mode that never stops to ask. Read from what
 *  the agent itself recorded, not from what we asked for, so a session started outside
 *  AI Control is labelled just as accurately. */
function isUnattended(session: Session): boolean {
  const meta = session.metadata as Record<string, unknown>
  return meta.approvalPolicy === 'never'
    || meta.sandboxPolicy === 'danger-full-access'
    || meta.permissionMode === 'bypassPermissions'
}

export function SessionView({ session }: { session: Session }) {
  const { notify, refresh } = useStore()
  const [tab, setTab] = useState<Tab>('agent')
  const [busy, setBusy] = useState(false)

  useEffect(() => { setTab('agent') }, [session.id])

  const act = async (name: string, run: () => Promise<unknown>) => {
    setBusy(true)
    try {
      await run()
      notify({ kind: 'info', text: `${name} — done` })
      await refresh()
    } catch (e) {
      notify({ kind: 'error', text: e instanceof ApiError ? e.message : String(e) })
    } finally {
      setBusy(false)
    }
  }

  const caps = session.capabilities

  return (
    <div className="main">
      <div className="session-head">
        <div className="row" style={{ marginBottom: 4 }}>
          <SourceTag session={session} />
          {isUnattended(session) && (
            <span className="bypass-badge" title="Running without approval prompts">
              no approvals
            </span>
          )}
          <Dot status={session.status} />
          <span className="dim" style={{ fontSize: 12 }}>{session.currentAction ?? session.status}</span>
          <span className="faint" style={{ fontSize: 12 }}>· {relativeTime(session.lastActivity)} ago</span>
          <div className="row" style={{ marginLeft: 'auto', gap: 6 }}>
            {caps.interrupt && (
              <button className="btn small danger" disabled={busy}
                      onClick={() => act('Interrupt', () => api.interrupt(session.id))}>
                Interrupt
              </button>
            )}
            {caps.resume && !session.isActive && (
              <button className="btn small" disabled={busy}
                      onClick={() => act('Resume', () => api.resume(session.id))}>
                Resume
              </button>
            )}
            {caps.archive && (
              <button className="btn small ghost" disabled={busy}
                      onClick={() => act('Archive', () => api.archive(session.id, true))}>
                Archive
              </button>
            )}
          </div>
        </div>
        <h2>{session.title ?? 'Untitled session'}</h2>
        <div className="meta">
          <span className="mono">{session.repository ?? '—'}</span>
          {session.branch && <><span className="sep">·</span><span className="mono">{session.branch}</span></>}
          {session.model && <><span className="sep">·</span><span>{session.model}</span></>}
          {session.forgejoIssue && <><span className="sep">·</span><span>#{session.forgejoIssue}</span></>}
          <span style={{ marginLeft: 'auto' }}><DiffBadge session={session} /></span>
        </div>
      </div>

      <div className="toolbar">
        <div className="tabs">
          <button className="tab" aria-selected={tab === 'agent'} onClick={() => setTab('agent')}>Agent</button>
          <button className="tab" aria-selected={tab === 'terminal'} onClick={() => setTab('terminal')}
                  disabled={!caps.terminal}
                  title={caps.terminal ? undefined : 'This session runs in a process AI Control does not own'}>
            Terminal
          </button>
          <button className="tab" aria-selected={tab === 'changes'} onClick={() => setTab('changes')}
                  disabled={!caps.diff}>Changes</button>
          <button className="tab" aria-selected={tab === 'issue'} onClick={() => setTab('issue')}>Issue</button>
          <button className="tab" aria-selected={tab === 'git'} onClick={() => setTab('git')}>Git</button>
        </div>
        <span className="faint mono truncate" style={{ marginLeft: 'auto', fontSize: 11 }}>
          {session.workingDirectory}
        </span>
      </div>

      {tab === 'agent' && <AgentTab session={session} />}
      {tab === 'terminal' && <TerminalTab session={session} />}
      {tab === 'changes' && <ChangesTab session={session} />}
      {tab === 'issue' && <IssueTab session={session} />}
      {tab === 'git' && <GitTab session={session} />}
    </div>
  )
}
