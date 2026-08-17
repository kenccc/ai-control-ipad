import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { Session } from '../lib/types'
import { Empty } from './common'

export function GitTab({ session }: { session: Session }) {
  const [commits, setCommits] = useState<any[]>([])
  const [branches, setBranches] = useState<string[]>([])

  useEffect(() => {
    if (!session.repository) return
    api.repoCommits(session.repository).then((d) => setCommits(d.commits)).catch(() => {})
    api.repoBranches(session.repository).then((d) => setBranches(d.branches)).catch(() => {})
  }, [session.repository])

  const git = session.gitStatus
  if (!git) return <Empty title="No git information for this session" />

  return (
    <div className="pane-body">
      <div className="context-section">
        <h3>Working tree</h3>
        <div className="row" style={{ gap: 16, fontSize: 12.5 }}>
          <span className="dim">{git.modified} modified</span>
          <span className="dim">{git.added + git.untracked} added</span>
          <span className="dim">{git.deleted} deleted</span>
          <span className="dim">ahead {git.ahead}</span>
          <span className="dim">behind {git.behind}</span>
        </div>
      </div>

      {branches.length > 0 && (
        <div className="context-section">
          <h3>Branches</h3>
          <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
            {branches.map((b) => (
              <span key={b} className="label-chip mono"
                    style={b === session.branch ? { borderColor: 'var(--accent)', color: 'var(--text)' } : undefined}>
                {b}
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="context-section">
        <h3>Commits</h3>
        {commits.map((commit) => (
          <div key={commit.sha} className="row" style={{ padding: '5px 0', alignItems: 'flex-start' }}>
            <span className="mono faint" style={{ flex: 'none' }}>{commit.sha.slice(0, 7)}</span>
            <span className="truncate">{commit.subject}</span>
            <span className="faint" style={{ marginLeft: 'auto', flex: 'none' }}>{commit.author}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
