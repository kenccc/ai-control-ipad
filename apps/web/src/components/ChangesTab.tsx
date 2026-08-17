import { useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { useStore } from '../lib/store'
import type { FileChange, ReviewComment, Session } from '../lib/types'
import { DiffView, type DiffMode } from './DiffView'
import { Empty } from './common'

export function ChangesTab({ session }: { session: Session }) {
  const { notify } = useStore()
  const [files, setFiles] = useState<FileChange[]>([])
  const [stats, setStats] = useState<{ files_changed: number; insertions: number; deletions: number } | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [diff, setDiff] = useState('')
  const [mode, setMode] = useState<DiffMode>('unified')
  const [comments, setComments] = useState<ReviewComment[]>([])
  const [pendingLine, setPendingLine] = useState<number | null>(null)
  const [commentDraft, setCommentDraft] = useState('')
  const [error, setError] = useState<string | null>(null)

  const loadComments = () =>
    api.reviewComments(session.id).then((d) => setComments(d.comments)).catch(() => {})

  useEffect(() => {
    setSelected(null); setDiff(''); setError(null)
    api.sessionChanges(session.id).then((d) => {
      setFiles(d.files); setStats(d.stats)
      if (d.files.length) setSelected(d.files[0].path)
    }).catch((e) => setError(e instanceof ApiError ? e.message : String(e)))
    loadComments()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.id, session.diffStats?.files_changed])

  useEffect(() => {
    if (!selected) return
    api.sessionDiff(session.id, selected).then((d) => setDiff(d.diff)).catch(() => setDiff(''))
  }, [session.id, selected])

  const fileComments = useMemo(
    () => comments.filter((c) => c.file_path === selected), [comments, selected])
  const unsent = comments.filter((c) => !c.sent_at)

  const addComment = async () => {
    if (!selected || pendingLine === null || !commentDraft.trim()) return
    await api.addReviewComment(session.id, selected, pendingLine, commentDraft.trim())
    setCommentDraft(''); setPendingLine(null)
    loadComments()
  }

  const sendFeedback = async () => {
    try {
      const result = await api.sendReview(session.id)
      notify({ kind: 'info', text: `Sent ${result.sent} review comments to ${session.sourceLabel}` })
      loadComments()
    } catch (e) {
      notify({ kind: 'error', text: e instanceof ApiError ? e.message : String(e) })
    }
  }

  const crossReview = async (reviewer: string) => {
    try {
      const result = await api.crossReview(session.id, reviewer)
      notify({ kind: 'info', text: `Review started (${result.reviewSessionId})` })
    } catch (e) {
      notify({ kind: 'error', text: e instanceof ApiError ? e.message : String(e) })
    }
  }

  if (error) return <div className="notice error">{error}</div>
  if (!files.length) {
    return <Empty title="No changes in this working tree"
                  hint={session.workingDirectory ?? undefined} />
  }

  return (
    <div className="pane">
      <div className="toolbar" style={{ borderTop: 0 }}>
        <span className="mono dim">
          {stats?.files_changed ?? files.length} files
          {stats && stats.insertions > 0 && <> <span className="badge add">+{stats.insertions}</span></>}
          {stats && stats.deletions > 0 && <> <span className="badge del">-{stats.deletions}</span></>}
        </span>
        <div className="tabs" style={{ marginLeft: 'auto' }}>
          <button className="tab" aria-selected={mode === 'unified'}
                  onClick={() => setMode('unified')}>Unified</button>
          <button className="tab" aria-selected={mode === 'split'}
                  onClick={() => setMode('split')}>Split</button>
        </div>
        <button className="btn small" disabled={!session.capabilities.send_message}
                onClick={() => crossReview(session.provider === 'anthropic_claude' ? 'codex_cli' : 'claude_code')}>
          {session.provider === 'anthropic_claude' ? 'Review with Codex' : 'Review with Claude'}
        </button>
        {unsent.length > 0 && (
          <button className="btn primary small" onClick={sendFeedback}
                  disabled={!session.capabilities.send_message}>
            Send {unsent.length} comment{unsent.length > 1 ? 's' : ''}
          </button>
        )}
      </div>

      <div className="changes-list">
        {files.map((file) => {
          const count = comments.filter((c) => c.file_path === file.path).length
          return (
            <button key={file.path} className="change" aria-current={file.path === selected}
                    onClick={() => setSelected(file.path)}>
              <span className={`st ${file.status}`}>{file.status === '??' ? 'A' : file.status}</span>
              <span className="truncate">{file.path}</span>
              {count > 0 && <span className="badge">{count}</span>}
              <span className="nums">
                {file.binary
                  ? <span className="faint">binary</span>
                  : <>
                      {file.insertions > 0 && <span className="plus">+{file.insertions}</span>}
                      {file.deletions > 0 && <span className="minus">-{file.deletions}</span>}
                    </>}
              </span>
            </button>
          )
        })}
      </div>

      <div className="pane-body">
        {selected && <DiffView diff={diff} mode={mode} comments={fileComments}
                               onComment={setPendingLine} />}
      </div>

      {pendingLine !== null && (
        <div className="composer">
          <div className="faint" style={{ marginBottom: 6, fontSize: 12 }}>
            {selected} · line {pendingLine}
          </div>
          <div className="field">
            <textarea className="textarea" rows={2} autoFocus value={commentDraft}
                      placeholder="Review comment…"
                      onChange={(e) => setCommentDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); addComment() }
                        if (e.key === 'Escape') { setPendingLine(null); setCommentDraft('') }
                      }} />
            <button className="btn primary" onClick={addComment} disabled={!commentDraft.trim()}>Add</button>
            <button className="btn ghost" onClick={() => { setPendingLine(null); setCommentDraft('') }}>Cancel</button>
          </div>
          <div className="hint">Double-tap any line in the diff to comment on it.</div>
        </div>
      )}
    </div>
  )
}
