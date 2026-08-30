/**
 * Application state: the session list, the live WebSocket, and connection health.
 *
 * Sessions arrive two ways -- a full REST list on load, and deltas over the socket --
 * and both funnel through one reducer so the dashboard cannot drift from the server.
 */
import {
  createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, useState,
} from 'react'
import type { ReactNode } from 'react'
import { api, setCsrfToken } from './api'
import type { Session } from './types'

type ConnectionState = 'connecting' | 'online' | 'offline'

interface State {
  sessions: Record<string, Session>
  order: string[]
  lastReconcile: number | null
}

type Action =
  | { type: 'replace'; sessions: Session[]; lastReconcile: number | null }
  | { type: 'upsert'; session: Session }
  | { type: 'status'; id: string; status: Session['status']; action: string | null; isActive: boolean }
  | { type: 'diff'; id: string; diffStats: Session['diffStats'] }
  | { type: 'remove'; id: string }

function sortIds(sessions: Record<string, Session>): string[] {
  return Object.values(sessions)
    .sort((a, b) => {
      if (a.isActive !== b.isActive) return a.isActive ? -1 : 1
      return (b.lastActivity ?? 0) - (a.lastActivity ?? 0)
    })
    .map((s) => s.id)
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'replace': {
      const sessions = Object.fromEntries(action.sessions.map((s) => [s.id, s]))
      return { sessions, order: sortIds(sessions), lastReconcile: action.lastReconcile }
    }
    case 'upsert': {
      const sessions = { ...state.sessions, [action.session.id]: action.session }
      return { ...state, sessions, order: sortIds(sessions) }
    }
    case 'status': {
      const existing = state.sessions[action.id]
      if (!existing) return state
      const sessions = {
        ...state.sessions,
        [action.id]: {
          ...existing, status: action.status,
          currentAction: action.action, isActive: action.isActive,
          lastActivity: Date.now() / 1000,
        },
      }
      return { ...state, sessions, order: sortIds(sessions) }
    }
    case 'diff': {
      const existing = state.sessions[action.id]
      if (!existing) return state
      return {
        ...state,
        sessions: { ...state.sessions, [action.id]: { ...existing, diffStats: action.diffStats } },
      }
    }
    case 'remove': {
      const sessions = { ...state.sessions }
      delete sessions[action.id]
      return { ...state, sessions, order: sortIds(sessions) }
    }
  }
}

interface StoreValue {
  sessions: Session[]
  sessionsById: Record<string, Session>
  connection: ConnectionState
  latencyMs: number | null
  lastReconcile: number | null
  authenticated: boolean
  config: any
  login: (token: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
  notifications: Notice[]
  notify: (notice: Omit<Notice, 'id'>) => void
  dismiss: (id: number) => void
}

export interface Notice {
  id: number
  kind: 'info' | 'error' | 'attention'
  text: string
  sessionId?: string
}

const StoreContext = createContext<StoreValue | null>(null)

export function StoreProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer,
    { sessions: {}, order: [], lastReconcile: null })
  const [connection, setConnection] = useState<ConnectionState>('connecting')
  const [latencyMs, setLatencyMs] = useState<number | null>(null)
  const [authenticated, setAuthenticated] = useState(false)
  const [config, setConfig] = useState<any>(null)
  const [notifications, setNotifications] = useState<Notice[]>([])
  const noticeId = useRef(0)
  const socketRef = useRef<WebSocket | null>(null)
  const retryRef = useRef(0)

  const notify = useCallback((notice: Omit<Notice, 'id'>) => {
    const id = ++noticeId.current
    setNotifications((current) => [...current.slice(-4), { ...notice, id }])
    if (notice.kind !== 'error') {
      setTimeout(() => setNotifications((c) => c.filter((n) => n.id !== id)), 6000)
    }
    if ('Notification' in window && Notification.permission === 'granted'
        && notice.kind === 'attention') {
      new Notification('AI Control', { body: notice.text, tag: notice.sessionId })
    }
  }, [])

  const dismiss = useCallback((id: number) => {
    setNotifications((current) => current.filter((n) => n.id !== id))
  }, [])

  const refresh = useCallback(async () => {
    const data = await api.sessions({ include_archived: 'false' })
    dispatch({ type: 'replace', sessions: data.sessions, lastReconcile: data.lastReconcile })
  }, [])

  const login = useCallback(async (token: string) => {
    const result = await api.login(token)
    setCsrfToken(result.csrfToken)
    setAuthenticated(true)
    const me = await api.me()
    setConfig(me.config)
    await refresh()
  }, [refresh])

  const logout = useCallback(async () => {
    await api.logout()
    setCsrfToken(null)
    setAuthenticated(false)
    socketRef.current?.close()
  }, [])

  useEffect(() => {
    api.me().then(async (me) => {
      setAuthenticated(me.authenticated)
      setCsrfToken(me.csrfToken)
      setConfig(me.config)
      if (me.authenticated) await refresh()
    }).catch(() => setAuthenticated(false))
  }, [refresh])

  // WebSocket with exponential backoff. The iPad suspends sockets when the app goes
  // to the background, so reconnecting silently is the normal case, not an error path.
  const measureLatency = useCallback(async () => {
    const started = performance.now()
    try {
      await fetch('/api/health', { cache: 'no-store', credentials: 'same-origin' })
      setLatencyMs(Math.round(performance.now() - started))
    } catch {
      setLatencyMs(null)
    }
  }, [])

  useEffect(() => {
    if (!authenticated) return
    let cancelled = false
    let timer: number | undefined

    const connect = () => {
      if (cancelled) return
      setConnection((c) => (c === 'online' ? c : 'connecting'))
      const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
      const socket = new WebSocket(`${protocol}//${location.host}/api/stream`)
      socketRef.current = socket

      socket.onopen = () => {
        retryRef.current = 0
        setConnection('online')
        void measureLatency()
      }
      socket.onmessage = (raw) => {
        const message = JSON.parse(raw.data)
        switch (message.type) {
          case 'hello':
            dispatch({ type: 'replace', sessions: message.sessions,
                       lastReconcile: message.lastReconcile })
            break
          case 'ping':
            // The heartbeat proves the socket is alive; latency is measured
            // separately, because the gap between heartbeats is the interval, not
            // the round trip to the Mac.
            void measureLatency()
            break
          case 'session.discovered':
          case 'session.created':
            dispatch({ type: 'upsert', session: message.session })
            notify({ kind: 'info', sessionId: message.sessionId,
                     text: `${message.session.sourceLabel}: ${message.session.title ?? 'new session'}` })
            break
          case 'session.status':
            dispatch({ type: 'status', id: message.sessionId, status: message.status,
                       action: message.action, isActive: message.isActive })
            break
          case 'session.git_changed':
            dispatch({ type: 'diff', id: message.sessionId, diffStats: message.diffStats })
            break
          case 'session.permission':
            notify({ kind: 'attention', sessionId: message.sessionId,
                     text: 'An agent is waiting for permission' })
            break
          case 'session.completed':
            notify({ kind: 'info', sessionId: message.sessionId, text: 'Agent finished' })
            break
          case 'session.failed':
            notify({ kind: 'error', sessionId: message.sessionId, text: 'Agent failed' })
            break
          case 'session.removed':
            dispatch({ type: 'remove', id: message.sessionId })
            break
        }
      }
      const scheduleRetry = () => {
        if (cancelled) return
        setConnection('offline')
        const delay = Math.min(1000 * 2 ** retryRef.current++, 15000)
        timer = window.setTimeout(connect, delay)
      }
      socket.onclose = scheduleRetry
      socket.onerror = () => socket.close()
    }

    connect()
    // Coming back from the app switcher should reconnect at once, not after backoff.
    const onVisible = () => {
      if (document.visibilityState === 'visible'
          && socketRef.current?.readyState !== WebSocket.OPEN) {
        retryRef.current = 0
        window.clearTimeout(timer)
        connect()
      }
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisible)
      socketRef.current?.close()
    }
  }, [authenticated, notify, measureLatency])

  const sessions = useMemo(
    () => state.order.map((id) => state.sessions[id]).filter(Boolean),
    [state.order, state.sessions])

  const value: StoreValue = {
    sessions, sessionsById: state.sessions, connection, latencyMs,
    lastReconcile: state.lastReconcile, authenticated, config,
    login, logout, refresh, notifications, notify, dismiss,
  }
  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>
}

export function useStore(): StoreValue {
  const value = useContext(StoreContext)
  if (!value) throw new Error('useStore must be used inside StoreProvider')
  return value
}
