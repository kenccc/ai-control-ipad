import type { Session, StatusId } from '../lib/types'

export function Dot({ status }: { status: StatusId | 'online' | 'offline' | 'connecting' }) {
  return <span className={`dot ${status}`} aria-hidden />
}

/** The source label is load-bearing: Codex App, Codex CLI and Claude Code are
 *  different products and are never allowed to look alike. */
export function SourceTag({ session }: { session: Session }) {
  return <span className={`source-tag ${session.source}`}>{session.sourceLabel}</span>
}

export function relativeTime(seconds: number | null | undefined): string {
  if (!seconds) return ''
  const delta = Date.now() / 1000 - seconds
  if (delta < 60) return `${Math.max(0, Math.floor(delta))}s`
  if (delta < 3600) return `${Math.floor(delta / 60)}m`
  if (delta < 86400) return `${Math.floor(delta / 3600)}h`
  return `${Math.floor(delta / 86400)}d`
}

export function clockTime(seconds: number | null | undefined): string {
  if (!seconds) return ''
  return new Date(seconds * 1000).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit',
  })
}

export function statusText(session: Session): string {
  if (session.currentAction) return session.currentAction
  return session.status.replace(/_/g, ' ')
}

export function DiffBadge({ session }: { session: Session }) {
  const stats = session.diffStats
  if (!stats || stats.files_changed === 0) return null
  return (
    <span className="row" style={{ gap: 6 }}>
      {stats.insertions > 0 && <span className="badge add">+{stats.insertions}</span>}
      {stats.deletions > 0 && <span className="badge del">-{stats.deletions}</span>}
      <span className="faint mono">{stats.files_changed} files</span>
    </span>
  )
}

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="empty">
      <div style={{ fontSize: 13 }}>{title}</div>
      {hint && <div style={{ fontSize: 12, maxWidth: 400 }}>{hint}</div>}
    </div>
  )
}
