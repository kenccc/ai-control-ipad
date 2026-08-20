import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { ActivityEntry, Session } from '../lib/types'
import { clockTime, relativeTime } from './common'

const CAPABILITY_LABELS: [keyof Session['capabilities'], string][] = [
  ['read_conversation', 'Read conversation'],
  ['stream_events', 'Live updates'],
  ['diff', 'Git changes'],
  ['send_message', 'Send message'],
  ['steer', 'Steer turn'],
  ['interrupt', 'Interrupt'],
  ['terminal', 'Terminal'],
  ['approvals', 'Approvals'],
]

export function ContextPanel({ session }: { session: Session }) {
  const [activity, setActivity] = useState<ActivityEntry[]>([])

  useEffect(() => {
    api.activity(25, session.id).then((d) => setActivity(d.activity)).catch(() => {})
  }, [session.id, session.status])

  const git = session.gitStatus
  const meta = session.metadata as Record<string, any>

  return (
    <aside className="context scroll">
      <div className="context-section">
        <h3>Session</h3>
        <Row k="Source" v={session.sourceLabel} />
        <Row k="Model" v={session.model ?? 'unknown'} />
        <Row k="Repository" v={session.repository ?? '—'} />
        <Row k="Branch" v={session.branch ?? '—'} />
        {meta.currentBranch && meta.currentBranch !== session.branch && (
          <Row k="Repo now on" v={meta.currentBranch} />
        )}
        <Row k="Worktree" v={session.worktree ?? session.workingDirectory ?? '—'} />
        {meta.codexProjectName && <Row k="Codex project" v={meta.codexProjectName} />}
        {meta.approvalPolicy && <Row k="Approvals" v={String(meta.approvalPolicy)} />}
        {meta.sandboxPolicy && <Row k="Sandbox" v={String(meta.sandboxPolicy)} />}
        <Row k="Started" v={relativeTime(session.createdAt) + ' ago'} />
        <Row k="Last activity" v={relativeTime(session.lastActivity) + ' ago'} />
        {session.forgejoIssue && <Row k="Issue" v={`#${session.forgejoIssue}`} />}
      </div>

      {git && (
        <div className="context-section">
          <h3>Git</h3>
          <Row k="Modified" v={String(git.modified)} />
          <Row k="Added" v={String(git.added + git.untracked)} />
          <Row k="Deleted" v={String(git.deleted)} />
          <Row k="Ahead / behind" v={`${git.ahead} / ${git.behind}`} />
          {git.sha && <Row k="HEAD" v={git.sha.slice(0, 8)} />}
        </div>
      )}

      <div className="context-section">
        <h3>Capabilities</h3>
        <div className="caps">
          {CAPABILITY_LABELS.map(([key, label]) => {
            const on = Boolean(session.capabilities[key])
            return (
              <div key={key} className={`cap ${on ? 'on' : 'off'}`}>
                <span className="mark">{on ? '✓' : '✗'}</span>
                <span>{label}</span>
              </div>
            )
          })}
        </div>
        {session.capabilities.write_blocked_reason && (
          <div className="faint" style={{ marginTop: 9, fontSize: 11.5, lineHeight: 1.5 }}>
            {session.capabilities.write_blocked_reason}
          </div>
        )}
      </div>

      {activity.length > 0 && (
        <div className="context-section">
          <h3>Activity</h3>
          {activity.map((entry) => (
            <div key={entry.id} className="row" style={{ padding: '3px 0', fontSize: 12 }}>
              <span className="faint mono" style={{ flex: 'none' }}>{clockTime(entry.timestamp)}</span>
              <span className="dim truncate">{entry.text ?? entry.kind}</span>
            </div>
          ))}
        </div>
      )}
    </aside>
  )
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className="v" title={v}>{v}</span>
    </div>
  )
}
