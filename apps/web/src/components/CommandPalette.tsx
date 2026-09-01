import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../lib/api'
import { useStore } from '../lib/store'
import type { View } from './Nav'
import { SourceTag } from './common'

interface Entry {
  group: string
  label: string
  hint?: string
  run: () => void
  node?: React.ReactNode
}

interface Props {
  mode: 'command' | 'switcher'
  onClose: () => void
  onNavigate: (view: View) => void
  onNewAgent: () => void
}

export function CommandPalette({ mode, onClose, onNavigate, onNewAgent }: Props) {
  const { sessions } = useStore()
  const [query, setQuery] = useState('')
  const [cursor, setCursor] = useState(0)
  const [remote, setRemote] = useState<any>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { inputRef.current?.focus() }, [])

  useEffect(() => {
    if (query.length < 2) { setRemote(null); return }
    const timer = window.setTimeout(() => {
      api.search(query).then(setRemote).catch(() => setRemote(null))
    }, 180)
    return () => window.clearTimeout(timer)
  }, [query])

  const entries = useMemo<Entry[]>(() => {
    const needle = query.toLowerCase()
    const out: Entry[] = []

    if (mode === 'command' && !query) {
      out.push(
        { group: 'Commands', label: 'New agent', run: onNewAgent },
        { group: 'Commands', label: 'Issues', run: () => onNavigate({ kind: 'issues' }) },
        { group: 'Commands', label: 'Pull requests', run: () => onNavigate({ kind: 'pulls' }) },
        { group: 'Commands', label: 'Repositories', run: () => onNavigate({ kind: 'repos' }) },
        { group: 'Commands', label: 'Activity', run: () => onNavigate({ kind: 'activity' }) },
        { group: 'Commands', label: 'Usage limits', run: () => onNavigate({ kind: 'usage' }) },
        { group: 'Commands', label: 'Diagnostics', run: () => onNavigate({ kind: 'diagnostics' }) },
      )
    } else if (mode === 'command') {
      const commands: [string, () => void][] = [
        ['New agent', onNewAgent],
        ['Open issues', () => onNavigate({ kind: 'issues' })],
        ['Open pull requests', () => onNavigate({ kind: 'pulls' })],
        ['Open repositories', () => onNavigate({ kind: 'repos' })],
        ['Open activity feed', () => onNavigate({ kind: 'activity' })],
        ['Open usage limits', () => onNavigate({ kind: 'usage' })],
        ['Open diagnostics', () => onNavigate({ kind: 'diagnostics' })],
      ]
      for (const [label, run] of commands) {
        if (label.toLowerCase().includes(needle)) out.push({ group: 'Commands', label, run })
      }
    }

    for (const session of sessions) {
      const haystack = `${session.title ?? ''} ${session.repository ?? ''} ${session.branch ?? ''}`.toLowerCase()
      if (!needle || haystack.includes(needle)) {
        out.push({
          group: 'Sessions',
          label: session.title ?? 'Untitled session',
          hint: `${session.repository ?? ''} ${session.branch ?? ''}`.trim(),
          node: <SourceTag session={session} />,
          run: () => onNavigate({ kind: 'session', id: session.id }),
        })
      }
      if (out.length > 60) break
    }

    for (const issue of remote?.issues ?? []) {
      out.push({
        group: 'Issues', label: `#${issue.number} ${issue.title}`,
        run: () => onNavigate({ kind: 'issue', number: issue.number }),
      })
    }
    for (const repo of remote?.repositories ?? []) {
      out.push({
        group: 'Repositories', label: repo.name,
        run: () => onNavigate({ kind: 'repo', name: repo.name }),
      })
    }
    for (const project of remote?.codexProjects ?? []) {
      out.push({
        group: 'Codex projects', label: project.name,
        run: () => onNavigate({ kind: 'project', id: project.id }),
      })
    }
    return out.slice(0, 60)
  }, [query, mode, sessions, remote, onNavigate, onNewAgent])

  useEffect(() => { setCursor(0) }, [query])

  const choose = (entry: Entry) => { entry.run(); onClose() }

  let lastGroup = ''
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="palette">
        <input
          ref={inputRef} className="input" value={query} placeholder={
            mode === 'switcher' ? 'Jump to a session, repo or issue…' : 'Search or run a command…'}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') onClose()
            if (e.key === 'ArrowDown') { e.preventDefault(); setCursor((c) => Math.min(c + 1, entries.length - 1)) }
            if (e.key === 'ArrowUp') { e.preventDefault(); setCursor((c) => Math.max(c - 1, 0)) }
            if (e.key === 'Enter' && entries[cursor]) { e.preventDefault(); choose(entries[cursor]) }
          }}
        />
        <div className="results">
          {entries.map((entry, index) => {
            const header = entry.group !== lastGroup ? entry.group : null
            lastGroup = entry.group
            return (
              <div key={index}>
                {header && <div className="group">{header}</div>}
                <button className="result" aria-selected={index === cursor}
                        onMouseEnter={() => setCursor(index)} onClick={() => choose(entry)}>
                  {entry.node}
                  <span className="truncate">{entry.label}</span>
                  {entry.hint && <span className="faint mono truncate" style={{ marginLeft: 'auto' }}>{entry.hint}</span>}
                </button>
              </div>
            )
          })}
          {entries.length === 0 && <div className="empty" style={{ height: 120 }}>No matches</div>}
        </div>
      </div>
    </div>
  )
}
