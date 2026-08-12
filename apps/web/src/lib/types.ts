export type SourceId = 'codex_desktop' | 'codex_cli' | 'codex_unknown' | 'claude_code'

export type StatusId =
  | 'running' | 'thinking' | 'executing' | 'editing' | 'waiting'
  | 'waiting_for_permission' | 'idle' | 'completed' | 'interrupted'
  | 'failed' | 'disconnected' | 'unknown'

export interface Capabilities {
  read_sessions: boolean
  read_conversation: boolean
  stream_events: boolean
  send_message: boolean
  interrupt: boolean
  steer: boolean
  resume: boolean
  terminate: boolean
  terminal: boolean
  diff: boolean
  approvals: boolean
  fork: boolean
  archive: boolean
  write_blocked_reason: string | null
}

export interface DiffStats { files_changed: number; insertions: number; deletions: number }

export interface GitState {
  branch: string | null; sha: string | null; origin_url: string | null
  modified: number; added: number; deleted: number; untracked: number
  ahead: number; behind: number
}

export interface Session {
  id: string
  source: SourceId
  sourceLabel: string
  provider: string
  externalSessionId: string
  title: string | null
  repository: string | null
  workingDirectory: string | null
  branch: string | null
  worktree: string | null
  forgejoIssue: number | null
  status: StatusId
  isActive: boolean
  currentAction: string | null
  createdAt: number | null
  lastActivity: number | null
  model: string | null
  archived: boolean
  capabilities: Capabilities
  gitStatus: GitState | null
  diffStats: DiffStats | null
  metadata: Record<string, unknown>
}

export type EventKind =
  | 'user_message' | 'agent_message' | 'command' | 'file_edit' | 'tool'
  | 'permission_request' | 'error' | 'turn_start' | 'turn_end' | 'turn_aborted' | 'system'

export interface SessionEvent {
  kind: EventKind
  timestamp: number
  text: string | null
  turnId: string | null
  detail: Record<string, unknown>
}

export interface FileChange {
  path: string; status: string; insertions: number; deletions: number; binary?: boolean
}

export interface CodexProject {
  id: string; name: string; rootPaths: string[]; kind: string
  sessions: Session[]; activeSessions: number
}

export interface Repository {
  name: string; path: string; forgejo: string | null; exists: boolean
  git?: GitState | null; sessions?: number; activeSessions?: number
}

export interface Issue {
  number: number; title: string; body: string; state: string
  labels: { name: string; color?: string }[]
  user?: { login: string }
  created_at?: string
  agents?: Session[]
}

export interface PullRequest {
  number: number; title: string; state: string; body?: string
  head?: { ref: string }; base?: { ref: string }
  user?: { login: string }
  comments?: number
}

export interface ReviewComment {
  id: number; session_id: string; file_path: string; line: number | null
  body: string; created_at: number; sent_at: number | null
}

export interface ActivityEntry {
  id: number; timestamp: number; session_id: string | null
  kind: string; text: string | null
}

export interface Diagnostics {
  server: { ok: boolean; wsSubscribers: number; lastReconcile: number | null; reconcileError: string | null }
  codexBinaries: { path: string; version: string; desktopBundled: boolean }[]
  codexDesktop: Record<string, any>
  codexCli: Record<string, any>
  claudeCode: Record<string, any>
  sharedDaemon: Record<string, any>
  forgejo: Record<string, any>
  tailscale: Record<string, any>
  git: { detected: boolean }
  sessionCounts: Record<string, number>
  activeSessions: number
  repositories: Repository[]
  terminals: number
}
