/** Thin fetch wrapper. Credentials are cookies; the CSRF token rides on writes. */

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

let csrfToken: string | null = null
export const setCsrfToken = (token: string | null) => { csrfToken = token }
export const getCsrfToken = () => csrfToken

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  if (init.body) headers.set('content-type', 'application/json')
  if (method !== 'GET' && csrfToken) headers.set('x-aicontrol-csrf', csrfToken)

  const response = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      // The backend puts the provider's own explanation in `detail`; showing it
      // verbatim is what keeps unsupported operations honest in the UI.
      detail = typeof body?.detail === 'string' ? body.detail : detail
    } catch { /* non-JSON error body */ }
    throw new ApiError(response.status, detail)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

const get = <T,>(path: string) => request<T>(path)
const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
const del = <T,>(path: string) => request<T>(path, { method: 'DELETE' })

const enc = encodeURIComponent

export const api = {
  me: () => get<{ authenticated: boolean; csrfToken: string | null; config: any }>('/api/auth/me'),
  login: (token: string) => post<{ ok: boolean; csrfToken: string }>('/api/auth/login', { token }),
  logout: () => post('/api/auth/logout'),

  sessions: (params: Record<string, string> = {}) =>
    get<{ sessions: import('./types').Session[]; lastReconcile: number | null }>(
      `/api/sessions?${new URLSearchParams(params)}`),
  sessionEvents: (id: string, limit = 400) =>
    get<{ events: import('./types').SessionEvent[] }>(`/api/sessions/${enc(id)}/events?limit=${limit}`),
  sessionChanges: (id: string) =>
    get<{ files: import('./types').FileChange[]; stats: import('./types').DiffStats | null; workingDirectory?: string; reason?: string }>(
      `/api/sessions/${enc(id)}/changes`),
  sessionDiff: (id: string, file: string, context = 3) =>
    get<{ file: string; diff: string }>(`/api/sessions/${enc(id)}/diff?file=${enc(file)}&context=${context}`),
  sendMessage: (id: string, message: string) =>
    post(`/api/sessions/${enc(id)}/messages`, { message }),
  interrupt: (id: string) => post(`/api/sessions/${enc(id)}/interrupt`),
  resume: (id: string) => post(`/api/sessions/${enc(id)}/resume`),
  terminate: (id: string) => post(`/api/sessions/${enc(id)}/terminate`),
  archive: (id: string, archived: boolean) => post(`/api/sessions/${enc(id)}/archive`, { archived }),
  linkIssue: (id: string, issue: number | null) => post(`/api/sessions/${enc(id)}/issue`, { issue }),
  createSession: (body: Record<string, unknown>) =>
    post<{ ok: boolean; session: import('./types').Session }>('/api/sessions', body),

  reviewComments: (id: string) =>
    get<{ comments: import('./types').ReviewComment[] }>(`/api/sessions/${enc(id)}/review`),
  addReviewComment: (id: string, file_path: string, line: number | null, body: string) =>
    post<{ ok: boolean; id: number }>(`/api/sessions/${enc(id)}/review`, { file_path, line, body }),
  deleteReviewComment: (id: string, commentId: number) =>
    del(`/api/sessions/${enc(id)}/review/${commentId}`),
  sendReview: (id: string) => post<{ ok: boolean; sent: number }>(`/api/sessions/${enc(id)}/review/send`),
  crossReview: (id: string, reviewer: string) =>
    post<{ ok: boolean; reviewSessionId: string }>(`/api/sessions/${enc(id)}/cross-review`, { reviewer }),
  reviewFindings: (id: string) =>
    get<{ findings: { severity: string; text: string; file: string | null; line: number | null }[] }>(
      `/api/sessions/${enc(id)}/review/findings`),
  forwardReview: (id: string, target: string) =>
    post(`/api/sessions/${enc(id)}/review/forward`, { target_session_id: target }),

  repos: () => get<{ repositories: import('./types').Repository[] }>('/api/repos'),
  repo: (name: string) => get<any>(`/api/repos/${enc(name)}`),
  repoChanges: (name: string) =>
    get<{ files: import('./types').FileChange[]; stats: import('./types').DiffStats }>(`/api/repos/${enc(name)}/changes`),
  repoDiff: (name: string, file: string) =>
    get<{ file: string; diff: string }>(`/api/repos/${enc(name)}/diff?file=${enc(file)}`),
  repoCommits: (name: string) => get<{ commits: any[] }>(`/api/repos/${enc(name)}/commits`),
  repoBranches: (name: string) => get<{ branches: string[] }>(`/api/repos/${enc(name)}/branches`),

  codexProjects: () =>
    get<{ projects: import('./types').CodexProject[]; selectedProjectId: string | null; available: boolean }>(
      '/api/codex/projects'),

  issues: (repository?: string) =>
    get<{ issues: import('./types').Issue[]; repository: string }>(
      `/api/issues${repository ? `?repository=${enc(repository)}` : ''}`),
  issue: (index: number, repository?: string) =>
    get<{ issue: import('./types').Issue; comments: any[]; agents: import('./types').Session[] }>(
      `/api/issues/${index}${repository ? `?repository=${enc(repository)}` : ''}`),
  commentIssue: (index: number, body: string, repository?: string) =>
    post(`/api/issues/${index}/comments${repository ? `?repository=${enc(repository)}` : ''}`, { body }),
  pulls: (repository?: string) =>
    get<{ pulls: import('./types').PullRequest[]; repository: string }>(
      `/api/pulls${repository ? `?repository=${enc(repository)}` : ''}`),
  pull: (index: number, repository?: string) =>
    get<any>(`/api/pulls/${index}${repository ? `?repository=${enc(repository)}` : ''}`),

  openTerminal: (body: Record<string, unknown>) =>
    post<{ ok: boolean; terminal: { id: string; cwd: string } }>('/api/terminals', body),
  closeTerminal: (id: string) => del(`/api/terminals/${enc(id)}`),

  diagnostics: () => get<import('./types').Diagnostics>('/api/diagnostics'),
  activity: (limit = 100, sessionId?: string) =>
    get<{ activity: import('./types').ActivityEntry[] }>(
      `/api/activity?limit=${limit}${sessionId ? `&session_id=${enc(sessionId)}` : ''}`),
  audit: () => get<{ entries: any[] }>('/api/audit'),
  search: (q: string) => get<any>(`/api/search?q=${enc(q)}`),
}
