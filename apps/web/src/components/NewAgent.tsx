import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { useStore } from '../lib/store'
import type { Repository } from '../lib/types'

type Provider = 'codex_cli' | 'claude_code' | 'codex_desktop'
type BranchMode = 'current' | 'new_branch' | 'new_worktree'

interface Props {
  issue?: number | null
  onClose: () => void
  onCreated: (sessionId: string) => void
}

export function NewAgent({ issue, onClose, onCreated }: Props) {
  const { notify, refresh } = useStore()
  const [repos, setRepos] = useState<Repository[]>([])
  const [provider, setProvider] = useState<Provider>('codex_cli')
  const [repository, setRepository] = useState('')
  const [prompt, setPrompt] = useState('')
  const [branchMode, setBranchMode] = useState<BranchMode>('current')
  const [branch, setBranch] = useState('')
  const [model, setModel] = useState('')
  const [approval, setApproval] = useState('on-request')
  const [permissionMode, setPermissionMode] = useState('default')
  const [bypass, setBypass] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.repos().then((d) => {
      setRepos(d.repositories)
      if (d.repositories.length) setRepository(d.repositories[0].name)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (issue && !branch) setBranch(`ai/issue-${issue}`)
  }, [issue, branch])

  const submit = async () => {
    if (!repository || !prompt.trim()) return
    setBusy(true)
    setError(null)
    try {
      const result = await api.createSession({
        provider, repository, prompt: prompt.trim(),
        branch_mode: branchMode,
        branch: branchMode === 'current' ? null : (branch || null),
        model: model || null,
        issue: issue ?? null,
        approval_policy: provider === 'claude_code' ? null : approval,
        permission_mode: provider === 'claude_code' ? permissionMode : null,
        bypass_permissions: bypass,
      })
      await refresh()
      notify({ kind: 'info', text: 'Agent started' })
      onCreated(result.session.id)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <h2>New agent{issue ? ` for issue #${issue}` : ''}</h2>

        <div className="field-group">
          <label>Provider</label>
          <div className="choices">
            <button className="choice" aria-pressed={provider === 'codex_cli'}
                    onClick={() => setProvider('codex_cli')}>Codex CLI</button>
            <button className="choice" aria-pressed={provider === 'claude_code'}
                    onClick={() => setProvider('claude_code')}>Claude Code</button>
            <button className="choice" disabled title="Codex Desktop cannot be asked to open a new session from outside the app">
              Codex App
            </button>
          </div>
          <div className="faint" style={{ fontSize: 11.5, marginTop: 6, lineHeight: 1.5 }}>
            Codex Desktop has no supported way to start a session in the app from
            outside it. Start the task in the app on your Mac, and it will appear here
            automatically.
          </div>
        </div>

        <div className="field-group">
          <label>Repository</label>
          <select className="select" value={repository} onChange={(e) => setRepository(e.target.value)}>
            {repos.map((repo) => (
              <option key={repo.name} value={repo.name} disabled={!repo.exists}>{repo.name}</option>
            ))}
          </select>
        </div>

        <div className="field-group">
          <label>Branch</label>
          <div className="choices">
            <button className="choice" aria-pressed={branchMode === 'current'}
                    onClick={() => setBranchMode('current')}>Current branch</button>
            <button className="choice" aria-pressed={branchMode === 'new_branch'}
                    onClick={() => setBranchMode('new_branch')}>New branch</button>
            <button className="choice" aria-pressed={branchMode === 'new_worktree'}
                    onClick={() => setBranchMode('new_worktree')}>New worktree</button>
          </div>
          {branchMode !== 'current' && (
            <input className="input" style={{ marginTop: 8 }} value={branch}
                   placeholder="branch name" onChange={(e) => setBranch(e.target.value)} />
          )}
          {branchMode === 'new_worktree' && (
            <div className="faint" style={{ fontSize: 11.5, marginTop: 6 }}>
              An isolated worktree keeps this agent from colliding with anything else
              writing in the repository.
            </div>
          )}
        </div>

        <div className="field-group">
          <label>Prompt</label>
          <textarea className="textarea" rows={4} value={prompt} autoFocus
                    placeholder={issue ? 'Extra instructions (the issue is attached automatically)…'
                                       : 'What should the agent do?'}
                    onChange={(e) => setPrompt(e.target.value)} />
        </div>

        <div className="field-group">
          <label>Model (optional)</label>
          <input className="input" value={model} placeholder="provider default"
                 onChange={(e) => setModel(e.target.value)} />
        </div>

        <div className="field-group">
          <label>{provider === 'claude_code' ? 'Permission mode' : 'Approval policy'}</label>
          {provider === 'claude_code' ? (
            <div className="choices">
              {['default', 'plan', 'acceptEdits', 'dontAsk'].map((mode) => (
                <button key={mode} className="choice" disabled={bypass}
                        aria-pressed={!bypass && permissionMode === mode}
                        onClick={() => setPermissionMode(mode)}>{mode}</button>
              ))}
            </div>
          ) : (
            <div className="choices">
              {['untrusted', 'on-request', 'never'].map((policy) => (
                <button key={policy} className="choice" disabled={bypass}
                        aria-pressed={!bypass && approval === policy}
                        onClick={() => setApproval(policy)}>{policy}</button>
              ))}
            </div>
          )}
        </div>

        <div className="field-group">
          <label>Unattended</label>
          <div className="choices">
            <button className="choice danger" aria-pressed={bypass}
                    onClick={() => setBypass((v) => !v)}>
              {bypass ? '✓ ' : ''}Bypass permissions
            </button>
          </div>
          {bypass && (
            <div className="danger-note">
              The agent runs with no approval prompts and full disk and network access
              inside {repository || 'the repository'} —{' '}
              {provider === 'claude_code'
                ? <code className="mono">--permission-mode bypassPermissions</code>
                : <code className="mono">approvalPolicy: never</code>}
              {provider !== 'claude_code' && <> with a <code className="mono">danger-full-access</code> sandbox</>}.
              It will not stop to ask before running commands or editing files.
              Use a new worktree unless you mean it to touch your working tree.
              Every run started this way is recorded in the audit log.
            </div>
          )}
        </div>

        {error && <div className="notice error" style={{ margin: '0 0 12px' }}>{error}</div>}

        <div className="row" style={{ justifyContent: 'flex-end', gap: 8 }}>
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !prompt.trim() || !repository}
                  onClick={submit}>
            {busy ? 'Starting…' : 'Start agent'}
          </button>
        </div>
      </div>
    </div>
  )
}
