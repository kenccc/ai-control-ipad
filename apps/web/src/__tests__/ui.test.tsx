import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { parseDiff, DiffView } from '../components/DiffView'
import { SourceTag, relativeTime, statusText } from '../components/common'
import { ContextPanel } from '../components/ContextPanel'
import type { Capabilities, Session } from '../lib/types'

vi.mock('../lib/api', () => ({
  api: {
    activity: () => Promise.resolve({ activity: [] }),
    sessionEvents: () => Promise.resolve({ events: [] }),
  },
  ApiError: class extends Error {},
}))

vi.mock('../lib/store', () => ({
  useStore: () => ({ notify: vi.fn() }),
}))

const caps = (overrides: Partial<Capabilities> = {}): Capabilities => ({
  read_sessions: true, read_conversation: true, stream_events: true,
  send_message: false, interrupt: false, steer: false, resume: false,
  terminate: false, terminal: false, diff: true, approvals: false,
  fork: false, archive: false, write_blocked_reason: null, ...overrides,
})

const session = (overrides: Partial<Session> = {}): Session => ({
  id: 'codex_desktop:abc', source: 'codex_desktop', sourceLabel: 'Codex App',
  provider: 'openai_codex', externalSessionId: 'abc', title: 'Inventory rewrite',
  repository: 'inventory', workingDirectory: '/repo', branch: 'feature/428',
  worktree: '/repo', forgejoIssue: 428, status: 'idle', isActive: false,
  currentAction: 'Waiting for your input', createdAt: 1, lastActivity: 2,
  model: 'gpt-5.6', archived: false, capabilities: caps(),
  gitStatus: null, diffStats: null, metadata: {}, ...overrides,
})

describe('source labelling', () => {
  it('keeps the three agent sources visually distinct', () => {
    const { container: desktop } = render(<SourceTag session={session()} />)
    expect(desktop.textContent).toBe('Codex App')
    expect(desktop.firstElementChild?.className).toContain('codex_desktop')

    const { container: cli } = render(
      <SourceTag session={session({ source: 'codex_cli', sourceLabel: 'Codex CLI' })} />)
    expect(cli.textContent).toBe('Codex CLI')
    expect(cli.firstElementChild?.className).toContain('codex_cli')

    const { container: claude } = render(
      <SourceTag session={session({ source: 'claude_code', sourceLabel: 'Claude Code' })} />)
    expect(claude.textContent).toBe('Claude Code')
    expect(claude.firstElementChild?.className).toContain('claude_code')
  })
})

describe('capability-driven controls', () => {
  it('marks unavailable operations as off and shows the reason', async () => {
    render(<ContextPanel session={session({
      capabilities: caps({
        send_message: false,
        write_blocked_reason: 'This thread has a turn in progress in the Codex desktop app.',
      }),
    })} />)
    expect(await screen.findByText(/turn in progress in the Codex desktop app/))
      .toBeTruthy()
    const sendRow = screen.getByText('Send message').closest('.cap')
    expect(sendRow?.className).toContain('off')
    const readRow = screen.getByText('Read conversation').closest('.cap')
    expect(readRow?.className).toContain('on')
  })

  it('shows the session branch separately from the branch the repo is on now', async () => {
    render(<ContextPanel session={session({
      branch: 'feature/428', metadata: { currentBranch: 'main' },
    })} />)
    expect(await screen.findByText('feature/428')).toBeTruthy()
    expect(screen.getByText('Repo now on')).toBeTruthy()
    expect(screen.getByText('main')).toBeTruthy()
  })
})

describe('diff parsing', () => {
  const diff = [
    'diff --git a/api.py b/api.py',
    '--- a/api.py',
    '+++ b/api.py',
    '@@ -10,3 +10,4 @@',
    ' context line',
    '-removed line',
    '+added line',
    '+another added',
  ].join('\n')

  it('assigns the correct old and new line numbers', () => {
    const lines = parseDiff(diff)
    const added = lines.filter((l) => l.kind === 'add')
    expect(added.map((l) => l.text)).toEqual(['added line', 'another added'])
    expect(added.map((l) => l.newNo)).toEqual([11, 12])
    // The context line consumes old line 10, so the deletion is old line 11.
    expect(lines.find((l) => l.kind === 'del')?.oldNo).toBe(11)
    expect(lines.find((l) => l.kind === 'ctx')?.oldNo).toBe(10)
  })

  it('renders additions, deletions and inline comments', () => {
    const { container } = render(
      <DiffView diff={diff} mode="unified" onComment={() => {}} comments={[{
        id: 1, session_id: 's', file_path: 'api.py', line: 11,
        body: 'Use select_related()', created_at: 0, sent_at: null,
      }]} />)
    expect(container.querySelectorAll('.diff-line.add')).toHaveLength(2)
    expect(container.querySelectorAll('.diff-line.del')).toHaveLength(1)
    expect(container.querySelector('.inline-comment')?.textContent)
      .toContain('Use select_related()')
    expect(container.querySelectorAll('.diff-line.commented')).toHaveLength(1)
  })

  it('says so plainly when a file has no textual diff', () => {
    const { container } = render(
      <DiffView diff="" mode="unified" comments={[]} onComment={() => {}} />)
    expect(container.textContent).toContain('No textual diff')
  })
})

describe('status presentation', () => {
  it('prefers the concrete action over the bare status', () => {
    expect(statusText(session({ currentAction: 'Running pytest' }))).toBe('Running pytest')
    expect(statusText(session({ currentAction: null, status: 'waiting_for_permission' })))
      .toBe('waiting for permission')
  })

  it('formats ages compactly enough for a dense list', () => {
    const now = Date.now() / 1000
    expect(relativeTime(now - 30)).toBe('30s')
    expect(relativeTime(now - 300)).toBe('5m')
    expect(relativeTime(now - 7200)).toBe('2h')
    expect(relativeTime(now - 172800)).toBe('2d')
  })
})

describe('the composer', () => {
  it('is replaced by the provider\'s own reason when the session cannot be written to', async () => {
    const { AgentTab } = await import('../components/AgentTab')
    render(<AgentTab session={session({
      capabilities: caps({
        send_message: false,
        write_blocked_reason:
          'This thread has a turn in progress in the Codex desktop app.',
      }),
    })} />)
    expect(await screen.findByText(/turn in progress in the Codex desktop app/)).toBeTruthy()
    expect(screen.getByText('Continue conversation')).toBeTruthy()
    // No message box at all -- not a disabled one that looks usable.
    expect(document.querySelector('.composer textarea')).toBeNull()
  })

  it('offers a message box when the session can be continued, naming the source', async () => {
    const { AgentTab } = await import('../components/AgentTab')
    render(<AgentTab session={session({ capabilities: caps({ send_message: true }) })} />)
    const box = await screen.findByPlaceholderText('Ask Codex…')
    expect(box).toBeTruthy()
    expect(screen.getByText(/Continues the same Codex App session/)).toBeTruthy()
  })
})
