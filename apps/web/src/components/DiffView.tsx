import { useMemo, useState } from 'react'
import type { ReviewComment } from '../lib/types'

export type DiffMode = 'unified' | 'split'

interface Line {
  kind: 'add' | 'del' | 'ctx' | 'hunk' | 'meta'
  text: string
  oldNo: number | null
  newNo: number | null
}

export function parseDiff(diff: string): Line[] {
  const lines: Line[] = []
  let oldNo = 0
  let newNo = 0
  for (const raw of diff.split('\n')) {
    if (raw.startsWith('@@')) {
      const match = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw)
      if (match) { oldNo = Number(match[1]); newNo = Number(match[2]) }
      lines.push({ kind: 'hunk', text: raw, oldNo: null, newNo: null })
    } else if (raw.startsWith('+++') || raw.startsWith('---') || raw.startsWith('diff ')
               || raw.startsWith('index ') || raw.startsWith('new file')
               || raw.startsWith('deleted file') || raw.startsWith('similarity ')
               || raw.startsWith('rename ')) {
      lines.push({ kind: 'meta', text: raw, oldNo: null, newNo: null })
    } else if (raw.startsWith('+')) {
      lines.push({ kind: 'add', text: raw.slice(1), oldNo: null, newNo: newNo++ })
    } else if (raw.startsWith('-')) {
      lines.push({ kind: 'del', text: raw.slice(1), oldNo: oldNo++, newNo: null })
    } else if (raw.startsWith('\\')) {
      continue
    } else {
      lines.push({ kind: 'ctx', text: raw.slice(1), oldNo: oldNo++, newNo: newNo++ })
    }
  }
  // A trailing blank from the final newline is noise, not content.
  while (lines.length && lines[lines.length - 1].text === ''
         && lines[lines.length - 1].kind === 'ctx') lines.pop()
  return lines
}

interface Props {
  diff: string
  mode: DiffMode
  comments: ReviewComment[]
  onComment: (line: number) => void
  collapseUnchanged?: boolean
}

const COLLAPSE_THRESHOLD = 12
const COLLAPSE_KEEP = 3

export function DiffView({ diff, mode, comments, onComment, collapseUnchanged = true }: Props) {
  const lines = useMemo(() => parseDiff(diff), [diff])
  const [expanded, setExpanded] = useState<Set<number>>(new Set())

  const commentsByLine = useMemo(() => {
    const map = new Map<number, ReviewComment[]>()
    for (const comment of comments) {
      if (comment.line === null) continue
      const list = map.get(comment.line) ?? []
      list.push(comment)
      map.set(comment.line, list)
    }
    return map
  }, [comments])

  if (!diff.trim()) {
    return <div className="empty">No textual diff for this file (it may be binary).</div>
  }

  // Long stretches of unchanged code are folded so a large diff stays navigable on a
  // touch screen; each run expands in place.
  const runs: { start: number; end: number }[] = []
  if (collapseUnchanged) {
    let runStart = -1
    lines.forEach((line, index) => {
      if (line.kind === 'ctx') {
        if (runStart === -1) runStart = index
      } else if (runStart !== -1) {
        if (index - runStart > COLLAPSE_THRESHOLD) runs.push({ start: runStart, end: index })
        runStart = -1
      }
    })
    if (runStart !== -1 && lines.length - runStart > COLLAPSE_THRESHOLD) {
      runs.push({ start: runStart, end: lines.length })
    }
  }

  const hidden = new Map<number, { end: number; count: number }>()
  for (const run of runs) {
    if (expanded.has(run.start)) continue
    const from = run.start + COLLAPSE_KEEP
    const to = run.end - COLLAPSE_KEEP
    if (to - from > 2) hidden.set(from, { end: to, count: to - from })
  }

  const rendered: JSX.Element[] = []
  for (let index = 0; index < lines.length; index++) {
    const fold = hidden.get(index)
    if (fold) {
      const key = runs.find((r) => r.start + COLLAPSE_KEEP === index)?.start ?? index
      rendered.push(
        <button key={`fold-${index}`} className="diff-line hunk"
                style={{ width: '100%', border: 0, cursor: 'pointer', textAlign: 'left' }}
                onClick={() => setExpanded((s) => new Set(s).add(key))}>
          <span className="ln">⋯</span>
          <span className="content">{fold.count} unchanged lines</span>
        </button>,
      )
      index = fold.end - 1
      continue
    }
    const line = lines[index]
    const lineNo = line.newNo
    const lineComments = lineNo !== null ? commentsByLine.get(lineNo) : undefined
    rendered.push(
      <div key={index}
           className={`diff-line ${line.kind === 'ctx' ? '' : line.kind}${lineComments ? ' commented' : ''}`}
           onDoubleClick={() => lineNo !== null && onComment(lineNo)}>
        <span className="ln">{mode === 'unified' ? (line.newNo ?? line.oldNo ?? '') : (line.newNo ?? '')}</span>
        <span className="content">{line.text}</span>
      </div>,
    )
    if (lineComments) {
      for (const comment of lineComments) {
        rendered.push(
          <div key={`c-${comment.id}`} className="inline-comment">
            {comment.body}
            {comment.sent_at && <span className="faint"> · sent</span>}
          </div>,
        )
      }
    }
  }

  if (mode === 'split') {
    const left = lines.filter((l) => l.kind !== 'add')
    const right = lines.filter((l) => l.kind !== 'del')
    return (
      <div className="diff split">
        <div>{left.map((line, i) => (
          <div key={i} className={`diff-line ${line.kind === 'del' ? 'del' : line.kind === 'hunk' ? 'hunk' : ''}`}>
            <span className="ln">{line.oldNo ?? ''}</span>
            <span className="content">{line.text}</span>
          </div>
        ))}</div>
        <div>{right.map((line, i) => (
          <div key={i} className={`diff-line ${line.kind === 'add' ? 'add' : line.kind === 'hunk' ? 'hunk' : ''}`}
               onDoubleClick={() => line.newNo !== null && onComment(line.newNo)}>
            <span className="ln">{line.newNo ?? ''}</span>
            <span className="content">{line.text}</span>
          </div>
        ))}</div>
      </div>
    )
  }

  return <div className="diff">{rendered}</div>
}
