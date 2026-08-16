import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { useStore } from '../lib/store'
import type { Session, SessionEvent } from '../lib/types'
import { Empty, clockTime } from './common'

/** Transcript plus composer. The composer only exists when the session can actually
 *  receive a message; otherwise the provider's reason is shown in its place. */
export function AgentTab({ session }: { session: Session }) {
  const { notify } = useStore()
  const [events, setEvents] = useState<SessionEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const pinnedRef = useRef(true)
  const scrollerRef = useRef<HTMLDivElement>(null)

  const load = async () => {
    try {
      const data = await api.sessionEvents(session.id)
      setEvents(data.events)
      setError(null)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setLoading(true)
    setEvents([])
    load()
    // Poll only while the agent is working: the socket carries status, not transcript
    // text, and this effect re-runs when isActive flips, so going active starts the
    // polling and going idle stops it.
    if (!session.isActive) return
    const timer = window.setInterval(load, 4000)
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.id, session.isActive])

  useEffect(() => {
    if (pinnedRef.current) bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [events])

  const onScroll = () => {
    const el = scrollerRef.current
    if (!el) return
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  const send = async () => {
    const message = draft.trim()
    if (!message || sending) return
    setSending(true)
    try {
      await api.sendMessage(session.id, message)
      setDraft('')
      setTimeout(load, 700)
    } catch (e) {
      const text = e instanceof ApiError ? e.message : String(e)
      setError(text)
      notify({ kind: 'error', text })
    } finally {
      setSending(false)
    }
  }

  const caps = session.capabilities
  const placeholder = session.provider === 'anthropic_claude' ? 'Message Claude…' : 'Ask Codex…'

  return (
    <div className="pane">
      <div className="pane-body" ref={scrollerRef} onScroll={onScroll}>
        {loading && <div className="notice info">Loading conversation…</div>}
        {error && !loading && <div className="notice error">{error}</div>}
        {!loading && events.length === 0 && !error && (
          <Empty title="No conversation recorded yet"
                 hint="This session has not produced any user-visible messages." />
        )}
        <div className="events">
          {events.map((event, index) => <EventRow key={index} event={event} />)}
        </div>
        <div ref={bottomRef} />
      </div>

      <div className="composer">
        {caps.send_message ? (
          <>
            <div className="field">
              <textarea
                className="textarea" rows={2} value={draft} placeholder={placeholder}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send() }
                }}
              />
              <button className="btn primary" disabled={!draft.trim() || sending} onClick={send}>
                {sending ? 'Sending…' : 'Send'}
              </button>
            </div>
            <div className="hint">
              ⌘↵ to send. Continues the same {session.sourceLabel} session
              {caps.steer ? ' — steers the turn in progress.' : '.'}
            </div>
          </>
        ) : (
          <div className="blocked">
            <strong>Continue conversation</strong>
            {caps.write_blocked_reason
              ?? `Not available through the current ${session.sourceLabel} integration.`}
          </div>
        )}
      </div>
    </div>
  )
}

function EventRow({ event }: { event: SessionEvent }) {
  if (event.kind === 'turn_start' || event.kind === 'turn_end' || event.kind === 'turn_aborted') {
    const label = event.kind === 'turn_start' ? 'Turn started'
      : event.kind === 'turn_end' ? 'Turn complete' : 'Turn interrupted'
    return (
      <div className={`event ${event.kind}`}>
        <div className="gutter" />
        <div className="body">{label}</div>
      </div>
    )
  }

  const prefix = event.kind === 'command' ? '$ '
    : event.kind === 'file_edit' ? '± '
    : event.kind === 'tool' ? '⚙ ' : ''

  return (
    <div className={`event ${event.kind}`}>
      <div className="gutter">{clockTime(event.timestamp)}</div>
      <div className="body">
        {event.kind === 'permission_request' && (
          <div style={{ marginBottom: 4, color: 'var(--perm)', fontWeight: 600 }}>
            Permission requested
          </div>
        )}
        {prefix}{event.text}
        {event.kind === 'command' && typeof event.detail?.exitCode === 'number'
          && event.detail.exitCode !== 0 && (
          <span style={{ color: 'var(--fail)' }}>  → exit {String(event.detail.exitCode)}</span>
        )}
      </div>
    </div>
  )
}
