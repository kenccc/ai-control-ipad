import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { useStore } from '../lib/store'
import type { Issue, Session } from '../lib/types'
import { Empty } from './common'
import { Markdown } from './Markdown'
import { Lightbox } from './Lightbox'

export function IssueTab({ session }: { session: Session }) {
  const { notify, refresh } = useStore()
  const [issue, setIssue] = useState<Issue | null>(null)
  const [comments, setComments] = useState<any[]>([])
  const [error, setError] = useState<string | null>(null)
  const [linkDraft, setLinkDraft] = useState('')
  const [commentDraft, setCommentDraft] = useState('')
  const [zoomed, setZoomed] = useState<{ src: string; alt: string } | null>(null)

  useEffect(() => {
    setIssue(null); setError(null)
    if (!session.forgejoIssue) return
    api.issue(session.forgejoIssue, session.repository ?? undefined)
      .then((d) => { setIssue(d.issue); setComments(d.comments) })
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)))
  }, [session.forgejoIssue, session.repository])

  const link = async () => {
    const number = Number(linkDraft.replace('#', '').trim())
    if (!number) return
    try {
      await api.linkIssue(session.id, number)
      await refresh()
    } catch (e) {
      notify({ kind: 'error', text: e instanceof ApiError ? e.message : String(e) })
    }
  }

  const comment = async () => {
    if (!issue || !commentDraft.trim()) return
    try {
      await api.commentIssue(issue.number, commentDraft.trim(), session.repository ?? undefined)
      setCommentDraft('')
      const d = await api.issue(issue.number, session.repository ?? undefined)
      setComments(d.comments)
      notify({ kind: 'info', text: 'Comment posted to Forgejo' })
    } catch (e) {
      notify({ kind: 'error', text: e instanceof ApiError ? e.message : String(e) })
    }
  }

  if (!session.forgejoIssue) {
    return (
      <div className="pane-body">
        <div style={{ padding: 16, maxWidth: 420 }}>
          <Empty title="No issue linked to this session" />
          <div className="row" style={{ marginTop: 12 }}>
            <input className="input" placeholder="Issue number, e.g. 428" value={linkDraft}
                   onChange={(e) => setLinkDraft(e.target.value)}
                   onKeyDown={(e) => e.key === 'Enter' && link()} />
            <button className="btn" onClick={link}>Link</button>
          </div>
        </div>
      </div>
    )
  }

  if (error) return <div className="notice error">{error}</div>
  if (!issue) return <div className="notice info">Loading issue…</div>

  return (
    <div className="pane">
      <div className="pane-body" style={{ padding: '14px 16px' }}>
        <div className="row" style={{ marginBottom: 8 }}>
          <span className="mono faint">#{issue.number}</span>
          <span className="label-chip">{issue.state}</span>
          {issue.labels?.map((l) => <span key={l.name} className="label-chip">{l.name}</span>)}
        </div>
        <h2 style={{ margin: '0 0 12px', fontSize: 16 }}>{issue.title}</h2>
        <Markdown className="dim" onImageClick={(src, alt) => setZoomed({ src, alt })}>
          {issue.body ?? ''}
        </Markdown>

        {comments.length > 0 && (
          <div style={{ marginTop: 22 }}>
            <h3 style={{ fontSize: 10, letterSpacing: '.08em', textTransform: 'uppercase',
                         color: 'var(--text-faint)' }}>Comments</h3>
            {comments.map((c) => (
              <div key={c.id} style={{ borderTop: '1px solid var(--border)', padding: '10px 0' }}>
                <div className="faint" style={{ fontSize: 11.5, marginBottom: 4 }}>
                  {c.user?.login}
                </div>
                <Markdown className="dim" onImageClick={(src, alt) => setZoomed({ src, alt })}>
                  {c.body ?? ''}
                </Markdown>
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="composer">
        <div className="field">
          <textarea className="textarea" rows={2} value={commentDraft}
                    placeholder="Comment on this issue…"
                    onChange={(e) => setCommentDraft(e.target.value)} />
          <button className="btn" onClick={comment} disabled={!commentDraft.trim()}>Comment</button>
        </div>
      </div>
      {zoomed && <Lightbox src={zoomed.src} alt={zoomed.alt}
                           onClose={() => setZoomed(null)} />}
    </div>
  )
}
