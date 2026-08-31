import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { ActivityEntry, Issue, PullRequest, Repository, Session } from '../lib/types'
import type { View } from './Nav'
import { Dot, Empty, SourceTag, clockTime, relativeTime } from './common'
import { Markdown } from './Markdown'

function useAsync<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    load()
      .then((d) => { if (!cancelled) { setData(d); setError(null) } })
      .catch((e) => { if (!cancelled) setError(e instanceof ApiError ? e.message : String(e)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return { data, error, loading }
}

function Frame({ title, error, loading, children }: {
  title: string; error: string | null; loading: boolean; children: React.ReactNode
}) {
  return (
    <div className="main">
      <div className="toolbar"><h1>{title}</h1></div>
      <div className="pane-body">
        {loading && <div className="notice info">Loading…</div>}
        {error && <div className="notice error">{error}</div>}
        {!loading && !error && children}
      </div>
    </div>
  )
}

export function IssuesView({ onOpen, onStartAgent }:
                           { onOpen: (n: number) => void; onStartAgent: (n: number) => void }) {
  const { data, error, loading } = useAsync(() => api.issues(), [])
  return (
    <Frame title="Issues" error={error} loading={loading}>
      {data?.issues?.length === 0 && <Empty title="No open issues" />}
      {data?.issues?.map((issue: Issue) => (
        <div key={issue.number} className="list-row" style={{ cursor: 'default' }}>
          <span className="mono faint" style={{ flex: 'none', width: 46 }}>#{issue.number}</span>
          <div className="grow">
            <button className="truncate" style={{ background: 'none', border: 0, color: 'var(--text)',
                     padding: 0, cursor: 'pointer', textAlign: 'left', display: 'block', width: '100%' }}
                    onClick={() => onOpen(issue.number)}>
              {issue.title}
            </button>
            <div className="row" style={{ gap: 6, marginTop: 4 }}>
              {issue.labels?.map((l) => <span key={l.name} className="label-chip">{l.name}</span>)}
              {issue.agents?.map((a) => (
                <span key={a.id} className="row" style={{ gap: 4 }}>
                  <Dot status={a.status} /><SourceTag session={a} />
                </span>
              ))}
            </div>
          </div>
          <button className="btn small" style={{ flex: 'none' }}
                  onClick={() => onStartAgent(issue.number)}>Start agent</button>
        </div>
      ))}
    </Frame>
  )
}

export function PullsView({ onOpen }: { onOpen: (n: number) => void }) {
  const { data, error, loading } = useAsync(() => api.pulls(), [])
  return (
    <Frame title="Pull Requests" error={error} loading={loading}>
      {data?.pulls?.length === 0 && <Empty title="No open pull requests" />}
      {data?.pulls?.map((pull: PullRequest) => (
        <button key={pull.number} className="list-row" onClick={() => onOpen(pull.number)}>
          <span className="mono faint" style={{ flex: 'none', width: 46 }}>#{pull.number}</span>
          <div className="grow">
            <div className="truncate">{pull.title}</div>
            <div className="faint mono" style={{ fontSize: 11.5, marginTop: 3 }}>
              {pull.head?.ref} → {pull.base?.ref}
            </div>
          </div>
          <span className="label-chip" style={{ flex: 'none' }}>{pull.state}</span>
        </button>
      ))}
    </Frame>
  )
}

export function PullView({ number, onNavigate }:
                         { number: number; onNavigate: (v: View) => void }) {
  const { data, error, loading } = useAsync(() => api.pull(number), [number])
  return (
    <Frame title={`Pull request #${number}`} error={error} loading={loading}>
      {data && (
        <div style={{ padding: '14px 16px' }}>
          <h2 style={{ margin: '0 0 8px', fontSize: 16 }}>{data.pull.title}</h2>
          <div className="meta" style={{ marginBottom: 14 }}>
            <span className="mono">{data.pull.head?.ref} → {data.pull.base?.ref}</span>
            <span className="sep">·</span><span>{data.files?.length ?? 0} files</span>
            <span className="sep">·</span><span>{data.commits?.length ?? 0} commits</span>
          </div>
          {data.agents?.length > 0 && (
            <div className="context-section" style={{ padding: 0, border: 0, marginBottom: 14 }}>
              <h3>Agents on this branch</h3>
              {data.agents.map((agent: Session) => (
                <button key={agent.id} className="list-row"
                        onClick={() => onNavigate({ kind: 'session', id: agent.id })}>
                  <Dot status={agent.status} /><SourceTag session={agent} />
                  <span className="truncate">{agent.title}</span>
                </button>
              ))}
            </div>
          )}
          <Markdown className="dim">{data.pull.body ?? ''}</Markdown>
          <h3 style={{ fontSize: 10, letterSpacing: '.08em', textTransform: 'uppercase',
                       color: 'var(--text-faint)', marginTop: 22 }}>Files</h3>
          {data.files?.map((file: any) => (
            <div key={file.filename} className="change" style={{ cursor: 'default' }}>
              <span className="truncate">{file.filename}</span>
              <span className="nums">
                <span className="plus">+{file.additions}</span>
                <span className="minus">-{file.deletions}</span>
              </span>
            </div>
          ))}
        </div>
      )}
    </Frame>
  )
}

export function ReposView({ onOpen }: { onOpen: (name: string) => void }) {
  const { data, error, loading } = useAsync(() => api.repos(), [])
  return (
    <Frame title="Repositories" error={error} loading={loading}>
      {data?.repositories?.map((repo: Repository) => (
        <button key={repo.name} className="list-row" onClick={() => onOpen(repo.name)}>
          <div className="grow">
            <div className="row">
              <span>{repo.name}</span>
              {!repo.exists && <span className="label-chip" style={{ color: 'var(--fail)' }}>missing</span>}
            </div>
            <div className="faint mono" style={{ fontSize: 11.5, marginTop: 3 }}>{repo.path}</div>
          </div>
          <div className="col" style={{ alignItems: 'flex-end', flex: 'none', gap: 3 }}>
            {repo.git && <span className="mono dim" style={{ fontSize: 11.5 }}>{repo.git.branch}</span>}
            <span className="faint" style={{ fontSize: 11.5 }}>
              {repo.activeSessions ?? 0} active · {repo.sessions ?? 0} sessions
            </span>
          </div>
        </button>
      ))}
    </Frame>
  )
}

export function RepoView({ name, onNavigate }:
                         { name: string; onNavigate: (v: View) => void }) {
  const { data, error, loading } = useAsync(() => api.repo(name), [name])
  return (
    <Frame title={name} error={error} loading={loading}>
      {data && (
        <div style={{ padding: '14px 16px' }}>
          <div className="meta" style={{ marginBottom: 14 }}>
            <span className="mono">{data.git?.branch}</span>
            <span className="sep">·</span>
            <span>{data.git?.modified ?? 0} modified, {(data.git?.added ?? 0) + (data.git?.untracked ?? 0)} added</span>
            <span className="sep">·</span>
            <span>ahead {data.git?.ahead ?? 0} / behind {data.git?.behind ?? 0}</span>
          </div>
          {data.worktrees?.length > 0 && (
            <>
              <h3 style={{ fontSize: 10, letterSpacing: '.08em', textTransform: 'uppercase',
                           color: 'var(--text-faint)' }}>AI worktrees</h3>
              {data.worktrees.map((wt: any) => (
                <div key={wt.path} className="change" style={{ cursor: 'default' }}>
                  <span className="truncate">{wt.path}</span>
                  <span className="nums mono">{wt.branch}</span>
                </div>
              ))}
            </>
          )}
          <h3 style={{ fontSize: 10, letterSpacing: '.08em', textTransform: 'uppercase',
                       color: 'var(--text-faint)', marginTop: 18 }}>Agents</h3>
          {data.sessions?.map((session: Session) => (
            <button key={session.id} className="list-row"
                    onClick={() => onNavigate({ kind: 'session', id: session.id })}>
              <Dot status={session.status} /><SourceTag session={session} />
              <span className="truncate grow">{session.title}</span>
              <span className="faint" style={{ flex: 'none' }}>{relativeTime(session.lastActivity)}</span>
            </button>
          ))}
        </div>
      )}
    </Frame>
  )
}

export function ActivityView() {
  const { data, error, loading } = useAsync(() => api.activity(200), [])
  return (
    <Frame title="Activity" error={error} loading={loading}>
      {data?.activity?.map((entry: ActivityEntry) => (
        <div key={entry.id} className="row" style={{ padding: '7px 16px', gap: 12,
             borderBottom: '1px solid var(--border)' }}>
          <span className="mono faint" style={{ flex: 'none' }}>{clockTime(entry.timestamp)}</span>
          <span className="dim truncate">{entry.text ?? entry.kind}</span>
        </div>
      ))}
      {data?.activity?.length === 0 && <Empty title="No activity recorded yet" />}
    </Frame>
  )
}

export function ProjectView({ id, onNavigate }:
                            { id: string; onNavigate: (v: View) => void }) {
  const { data, error, loading } = useAsync(() => api.codexProjects(), [])
  const project = data?.projects?.find((p) => p.id === id)
  return (
    <Frame title={project?.name ?? 'Codex project'} error={error} loading={loading}>
      {project && (
        <>
          <div className="meta" style={{ padding: '12px 16px' }}>
            <span className="mono faint">{project.rootPaths.join(', ')}</span>
          </div>
          {project.sessions.map((session) => (
            <button key={session.id} className="list-row"
                    onClick={() => onNavigate({ kind: 'session', id: session.id })}>
              <Dot status={session.status} /><SourceTag session={session} />
              <span className="truncate grow">{session.title}</span>
              <span className="faint" style={{ flex: 'none' }}>{relativeTime(session.lastActivity)}</span>
            </button>
          ))}
          {project.sessions.length === 0 && (
            <Empty title="No sessions in this Codex project"
                   hint="Project membership comes from the Codex desktop app's own state." />
          )}
        </>
      )}
    </Frame>
  )
}
