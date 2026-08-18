import { useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { api, ApiError } from '../lib/api'
import type { Session } from '../lib/types'

/** A real PTY over a WebSocket, with the key row an iPad soft keyboard cannot give you. */
export function TerminalTab({ session }: { session: Session }) {
  const hostRef = useRef<HTMLDivElement>(null)
  const termRef = useRef<Terminal | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ctrlArmed, setCtrlArmed] = useState(false)
  const ctrlRef = useRef(false)
  ctrlRef.current = ctrlArmed

  useEffect(() => {
    if (!session.capabilities.terminal) return
    let ptyId: string | null = null
    let disposed = false
    const fit = new FitAddon()

    const term = new Terminal({
      fontFamily: 'ui-monospace, "SF Mono", Menlo, monospace',
      fontSize: 13, lineHeight: 1.3, cursorBlink: true, scrollback: 5000,
      theme: {
        background: '#06080b', foreground: '#e5e9ef', cursor: '#5b8dff',
        selectionBackground: '#2d4a8a',
      },
      // The iPad's on-screen keyboard needs a real focusable element behind xterm.
      allowProposedApi: true,
    })
    term.loadAddon(fit)
    termRef.current = term
    if (hostRef.current) term.open(hostRef.current)
    try { fit.fit() } catch { /* not laid out yet */ }

    const start = async () => {
      try {
        const result = await api.openTerminal({
          session_id: session.id, cols: term.cols, rows: term.rows,
        })
        if (disposed) { api.closeTerminal(result.terminal.id).catch(() => {}); return }
        ptyId = result.terminal.id
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
        const socket = new WebSocket(`${protocol}//${location.host}/api/terminals/${ptyId}/stream`)
        socket.binaryType = 'arraybuffer'
        socketRef.current = socket
        socket.onmessage = (event) => term.write(new Uint8Array(event.data))
        socket.onclose = () => term.write('\r\n\x1b[2m[terminal closed]\x1b[0m\r\n')
        socket.onopen = () => socket.send(`\x00resize:${term.cols},${term.rows}`)
        term.onData((data) => {
          if (ctrlRef.current && data.length === 1) {
            const code = data.toUpperCase().charCodeAt(0)
            if (code >= 64 && code <= 95) {
              socket.send(String.fromCharCode(code - 64))
              setCtrlArmed(false)
              return
            }
            setCtrlArmed(false)
          }
          if (socket.readyState === WebSocket.OPEN) socket.send(data)
        })
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e))
      }
    }
    start()

    const onResize = () => {
      try {
        fit.fit()
        socketRef.current?.send(`\x00resize:${term.cols},${term.rows}`)
      } catch { /* ignore */ }
    }
    window.addEventListener('resize', onResize)
    const observer = new ResizeObserver(onResize)
    if (hostRef.current) observer.observe(hostRef.current)

    return () => {
      disposed = true
      window.removeEventListener('resize', onResize)
      observer.disconnect()
      socketRef.current?.close()
      if (ptyId) api.closeTerminal(ptyId).catch(() => {})
      term.dispose()
    }
  }, [session.id, session.capabilities.terminal])

  const sendKey = (data: string) => {
    socketRef.current?.send(data)
    termRef.current?.focus()
  }

  if (!session.capabilities.terminal) {
    return (
      <div className="pane-body">
        <div className="blocked" style={{ margin: 14 }}>
          <strong>Terminal</strong>
          This session runs in a process AI Control does not own, so it has no terminal.
          Open a terminal on the repository instead.
        </div>
      </div>
    )
  }

  return (
    <div className="pane">
      {error && <div className="notice error">{error}</div>}
      <div className="terminal-wrap" ref={hostRef} />
      <div className="term-keys">
        <button onClick={() => sendKey('\x1b')}>ESC</button>
        <button aria-pressed={ctrlArmed} onClick={() => { setCtrlArmed((v) => !v); termRef.current?.focus() }}>CTRL</button>
        <button onClick={() => sendKey('\t')}>TAB</button>
        <button onClick={() => sendKey('\x1b[A')}>↑</button>
        <button onClick={() => sendKey('\x1b[B')}>↓</button>
        <button onClick={() => sendKey('\x1b[D')}>←</button>
        <button onClick={() => sendKey('\x1b[C')}>→</button>
        <button onClick={() => sendKey('\x03')}>^C</button>
        <button onClick={() => sendKey('\x04')}>^D</button>
        <button onClick={() => sendKey('\x1a')}>^Z</button>
        <button onClick={() => sendKey('|')}>|</button>
        <button onClick={() => sendKey('~')}>~</button>
        <button onClick={() => sendKey('/')}>/</button>
        <button onClick={() => sendKey('-')}>-</button>
      </div>
    </div>
  )
}
