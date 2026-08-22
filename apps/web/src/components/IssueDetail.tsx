import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { useStore } from '../lib/store'
import type { Issue, Session } from '../lib/types'
import type { View } from './Nav'
import { Dot, SourceTag } from './common'

export function IssueDetail({ number, onNavigate, onStartAgent }: {
  number: number; onNavigate: (v: View) => void; onStartAgent: () => void
}) {
  const { notify } = useStore()
  const [issue, setIssue] = useState<Issue | null>(null)
  const [comments, setComments] = useState<any[]>([])
  const [agents, setAgents] = useState<Session[]>([])
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState('')

  const load = () => api.issue(number)
    .then((d) => { setIssue(d.issue); setComments(d.comments); setAgents(d.agents) })
    .catch((e) => setError(e instanceof ApiError ? e.message : String(e)))

  useEffect(() => { load() }, [number])

  const comment = async () => {
    if (!draft.trim()) return
    try {
      await api.commentIssue(number, draft.trim())
      setDraft('')
      load()
      notify({ kind: 'info', text: 'Comment posted' })
    } catch (e) {
      notify({ kind: 'error', text: e instanceof ApiError ? e.message : String(e) })
    }
  }

  if (error) return <div className="main"><div className="notice error">{error}</div></div>
  if (!issue) return <div className="main"><div className="notice info">Loading…</div></div>

  return (
    <div className="main">
      <div className="toolbar">
        <h1>#{issue.number}</h1>
        <span className="label-chip">{issue.state}</span>
        <button className="btn small primary" style={{ marginLeft: 'auto' }} onClick={onStartAgent}>
          Start agent
        </button>
      </div>
      <div className="pane-body" style={{ padding: '14px 16px' }}>
        <h2 style={{ margin: '0 0 8px', fontSize: 16 }}>{issue.title}</h2>
        <div className="row" style={{ gap: 6, marginBottom: 14 }}>
          {issue.labels?.map((l) => <span key={l.name} className="label-chip">{l.name}</span>)}
        </div>

        {agents.length > 0 && (
          <div className="context-section" style={{ padding: 0, border: 0, marginBottom: 16 }}>
            <h3>Agents</h3>
            {agents.map((agent) => (
              <button key={agent.id} className="list-row"
                      onClick={() => onNavigate({ kind: 'session', id: agent.id })}>
                <Dot status={agent.status} /><SourceTag session={agent} />
                <span className="truncate grow">{agent.title}</span>
                <span className="faint" style={{ flex: 'none' }}>{agent.status}</span>
              </button>
            ))}
          </div>
        )}

        <div style={{ whiteSpace: 'pre-wrap', color: 'var(--text-dim)', lineHeight: 1.65 }}>
          {issue.body}
        </div>

        {comments.map((c) => (
          <div key={c.id} style={{ borderTop: '1px solid var(--border)', padding: '12px 0', marginTop: 12 }}>
            <div className="faint" style={{ fontSize: 11.5, marginBottom: 4 }}>{c.user?.login}</div>
            <div style={{ whiteSpace: 'pre-wrap', color: 'var(--text-dim)' }}>{c.body}</div>
          </div>
        ))}
      </div>
      <div className="composer">
        <div className="field">
          <textarea className="textarea" rows={2} value={draft} placeholder="Comment…"
                    onChange={(e) => setDraft(e.target.value)} />
          <button className="btn" onClick={comment} disabled={!draft.trim()}>Comment</button>
        </div>
      </div>
    </div>
  )
}
