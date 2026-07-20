import { useEffect, useRef, useCallback } from 'react'
import { useCosmosStore } from '../store'

const WS_URL = 'ws://localhost:8000/ws'
// Exponential backoff: 500ms → 1s → 2s → 4s → 8s (cap), +0–20% jitter,
// infinite retries. Counter resets on a successful open.
const BACKOFF_BASE_MS = 500
const BACKOFF_CAP_MS  = 8_000

// Stable per-browser session id — lets the backend keep a running task alive
// through a reconnect and replay the events we missed.
const SID_KEY = 'cosmos-session-id'
function sessionId(): string {
  try {
    let sid = localStorage.getItem(SID_KEY)
    if (!sid) {
      sid = `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`
      localStorage.setItem(SID_KEY, sid)
    }
    return sid
  } catch { return '' }
}

// last_seq survives a page refresh (sessionStorage: per-tab, cleared when the
// tab closes). Without it a refresh sends hello with last_seq=0 and the
// backend replays the ENTIRE run buffer — duplicating chat messages and
// re-speaking the whole answer.
const SEQ_KEY = 'cosmos-last-seq'
function loadSeq(): number {
  try { return parseInt(sessionStorage.getItem(SEQ_KEY) || '0', 10) || 0 }
  catch { return 0 }
}
function saveSeq(n: number): void {
  try { sessionStorage.setItem(SEQ_KEY, String(n)) } catch { /* private mode */ }
}

export function useWebSocket(onMessage: (data: any) => void) {
  const wsRef       = useRef<WebSocket | null>(null)
  const timerRef    = useRef<ReturnType<typeof setTimeout>>()
  const attemptRef  = useRef(0)      // consecutive failed connection attempts
  const disposedRef = useRef(false)  // set on unmount — kills any pending reconnect
  const onMsgRef    = useRef(onMessage)
  const lastSeqRef  = useRef(loadSeq())  // highest buffered-event seq applied
  const reattachTimerRef = useRef<ReturnType<typeof setTimeout>>()
  // Selector — a whole-store subscription would re-render the caller on every
  // store update (including 60fps audio-level ticks)
  const setBackendConnected = useCosmosStore(s => s.setBackendConnected)
  const setConnectionStatus = useCosmosStore(s => s.setConnectionStatus)

  // Keep onMessage ref current without triggering reconnects
  useEffect(() => { onMsgRef.current = onMessage }, [onMessage])

  const connect = useCallback(() => {
    if (disposedRef.current) return
    // Single-socket guard (also covers StrictMode's double effect invocation):
    // never open a second socket while one is CONNECTING or OPEN.
    if (wsRef.current &&
        (wsRef.current.readyState === WebSocket.CONNECTING ||
         wsRef.current.readyState === WebSocket.OPEN)) return

    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => {
      attemptRef.current = 0
      setBackendConnected(true)
      setConnectionStatus('online')
      // Reclaim our session: the backend keeps a running task alive through a
      // ≤45s gap and replays missed events past last_seq.
      try {
        ws.send(JSON.stringify({ type: 'hello', session_id: sessionId(),
                                 last_seq: lastSeqRef.current }))
      } catch { /* socket raced shut — onclose will retry */ }
      // Only declare a mid-run death if the backend does NOT restore state
      // shortly (a state/tool frame arrives on successful reattach).
      const st = useCosmosStore.getState()
      if (st.isExecuting || st.state === 'executing') {
        clearTimeout(reattachTimerRef.current)
        reattachTimerRef.current = setTimeout(() => {
          const now = useCosmosStore.getState()
          if (now.isExecuting || now.state === 'executing') {
            now.finishRun(false)
            now.setExecuting(false)
            now.setState('idle')
          }
        }, 3_000)
      }
      console.log('[COSMOS] Backend connected')
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        // Any backend frame proves the session is alive — cancel the
        // "declare the run dead" timer armed on reconnect. This must run
        // BEFORE the seq dedup return: a duplicate frame is still proof of
        // life, and skipping the cancel let the 3s timer wipe a live run.
        clearTimeout(reattachTimerRef.current)
        // Fresh backend session (restart / session expiry): its seq counter
        // restarted at 0, so our high-water mark would silently drop EVERY
        // new buffered event (seq 1, 2, …) — the "UI freezes until I
        // refresh" bug. Reset the epoch.
        if (data.type === 'hello_ack') {
          if (!data.attached) {
            lastSeqRef.current = 0
            saveSeq(0)
          }
          return                        // internal handshake — not for the app
        }
        // Replay dedup: buffered events carry monotonic sequence numbers.
        if (typeof data.seq === 'number') {
          if (data.seq <= lastSeqRef.current) return
          lastSeqRef.current = data.seq
          saveSeq(data.seq)
        }
        onMsgRef.current(data)
      } catch {
        console.warn('[COSMOS] Bad WS message', event.data)
      }
    }

    ws.onclose = () => {
      if (wsRef.current === ws) wsRef.current = null
      if (disposedRef.current) return   // unmounted — no reconnect leak
      setBackendConnected(false)
      setConnectionStatus('reconnecting')
      const delay  = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** attemptRef.current)
      const jitter = delay * 0.2 * Math.random()
      attemptRef.current += 1
      clearTimeout(timerRef.current)
      timerRef.current = setTimeout(connect, delay + jitter)
    }

    // onerror always precedes/accompanies onclose — reconnect scheduling
    // lives in onclose only, so a failed attempt never schedules twice.
    ws.onerror = () => { try { ws.close() } catch { /* already closed */ } }
  }, [setBackendConnected, setConnectionStatus]) // no onMessage dep — use ref instead

  useEffect(() => {
    disposedRef.current = false   // StrictMode remount reuses the same refs
    connect()
    return () => {
      disposedRef.current = true
      clearTimeout(timerRef.current)
      clearTimeout(reattachTimerRef.current)
      const ws = wsRef.current
      wsRef.current = null
      if (ws) {
        // Detach handlers BEFORE close: the close event fires async, after
        // unmount — it must not flip store state or schedule a reconnect.
        ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null
        try { ws.close() } catch { /* already closed */ }
      }
    }
  }, [connect])

  const send = useCallback((data: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data))
    } else {
      console.warn('[COSMOS] WS not open, cannot send:', data)
    }
  }, [])

  return { send }
}
