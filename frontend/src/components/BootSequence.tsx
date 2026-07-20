import { useState, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'

// REAL probes — the boot log reports what is actually true instead of
// claiming "GitHub link established" unconditionally. Failures inform, never
// block: Cosmos still boots, degraded but honest.
type BootLine = { type: 'info' | 'ok' | 'err'; text: string }

async function runProbes(push: (l: BootLine) => void): Promise<void> {
  push({ type: 'info', text: 'COSMOS AI SYSTEM v3 — BOOT SEQUENCE INITIATED' })

  const timed = (p: Promise<Response>, ms = 3000) =>
    Promise.race([p, new Promise<never>((_, rej) => setTimeout(() => rej(new Error('timeout')), ms))])

  // Start ALL probes NOW and await them in order: the log reads identically,
  // but the 3s timeouts overlap instead of summing (a cold backend used to
  // cost up to 9s of serial waiting).
  const pHealth = timed(fetch('/health'))
  const pMemory = timed(fetch('/api/memory'))
  const pStatus = timed(fetch('/api/status'))

  // Backend reachability
  try {
    const r = await pHealth
    push(r.ok ? { type: 'ok', text: 'Backend core online (:8000)' }
              : { type: 'err', text: 'Backend responded abnormally' })
  } catch {
    push({ type: 'err', text: 'Backend unreachable — run start.sh' })
  }

  // Speech recognition + synthesis (browser capabilities)
  const hasSR = !!((window as any).SpeechRecognition || (window as any).webkitSpeechRecognition)
  push(hasSR ? { type: 'ok', text: 'Speech recognition available' }
             : { type: 'err', text: 'No Web Speech API in this browser' })
  push(window.speechSynthesis
    ? { type: 'ok', text: 'Voice synthesis module online' }
    : { type: 'err', text: 'No speech synthesis — text only' })

  // Long-term memory store
  try {
    const r = await pMemory
    const mem = await r.json()
    const facts = Object.keys(mem.preferences ?? {}).length +
                  Object.keys(mem.people ?? {}).length +
                  (mem.frequent_tasks ?? []).length
    push({ type: 'ok', text: `Long-term memory loaded (${facts} entries)` })
  } catch {
    push({ type: 'err', text: 'Memory store unavailable' })
  }

  // Model gateway health
  try {
    const r = await pStatus
    const s = await r.json()
    const cooling = Object.keys(s.llm?.cooldowns ?? {})
    push(cooling.length
      ? { type: 'err', text: `LLM gateway degraded (${cooling.join(', ')} cooling)` }
      : { type: 'ok', text: 'LLM gateway nominal' })
  } catch {
    push({ type: 'info', text: 'LLM gateway status unknown' })
  }

  push({ type: 'info', text: '════════════════════════════════' })
  push({ type: 'ok', text: 'COSMOS ONLINE — STANDING BY' })
}

interface Props {
  onComplete: () => void
}

export default function BootSequence({ onComplete }: Props) {
  const [lines, setLines] = useState<BootLine[]>([])
  const [progress, setProgress] = useState(0)
  const [done, setDone] = useState(false)
  const onCompleteRef = useRef(onComplete)
  useEffect(() => { onCompleteRef.current = onComplete }, [onComplete])

  const finish = () => {
    setDone(true)
    setTimeout(() => onCompleteRef.current(), 100)
  }

  useEffect(() => {
    // Repeat visits get a fast cadence — the theatre is for the first boot.
    const seen = sessionStorage.getItem('cosmos-booted') === '1'
    const paceMs = seen ? 60 : 170
    const TOTAL = 8
    let cancelled = false
    const queue: BootLine[] = []
    let shown = 0
    let draining = false

    const drain = () => {
      if (draining || cancelled) return
      draining = true
      const tick = () => {
        if (cancelled) return
        const next = queue.shift()
        if (next) {
          shown++
          setLines(prev => [...prev, next])
          setProgress(Math.min(100, Math.round((shown / TOTAL) * 100)))
          setTimeout(tick, paceMs)
        } else {
          draining = false
        }
      }
      tick()
    }

    runProbes(l => { queue.push(l); drain() }).then(() => {
      const wait = () => {
        if (cancelled) return
        if (queue.length === 0 && !draining) {
          setProgress(100)
          sessionStorage.setItem('cosmos-booted', '1')
          setTimeout(finish, 200)
        } else {
          setTimeout(wait, 120)
        }
      }
      wait()
    })

    // Press any key / click to skip the theatre.
    const skip = () => { if (!cancelled) { cancelled = true; finish() } }
    window.addEventListener('keydown', skip)
    window.addEventListener('pointerdown', skip)
    return () => {
      cancelled = true
      window.removeEventListener('keydown', skip)
      window.removeEventListener('pointerdown', skip)
    }
  }, []) // intentionally empty — runs once on mount

  const lineColor = (type: string) => {
    if (type === 'ok')  return '#00ff88'
    if (type === 'err') return '#ff0044'
    return '#00d4ff'
  }

  const linePrefix = (type: string) => {
    if (type === 'ok')  return '[  OK  ] '
    if (type === 'err') return '[ FAIL ] '
    return '[  --  ] '
  }

  return (
    <AnimatePresence>
      {!done && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0, scale: 0.95 }}
          transition={{ duration: 0.2 }}
          className="fixed inset-0 z-50 flex flex-col items-center justify-center"
          style={{ background: '#050510' }}
        >
          {/* Logo */}
          <motion.div
            initial={{ scale: 0.5, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            transition={{ duration: 0.6, ease: 'easeOut' }}
            className="mb-10 text-center"
          >
            <div
              style={{
                fontFamily: 'Orbitron, monospace',
                fontSize: 48,
                fontWeight: 900,
                color: '#00d4ff',
                letterSpacing: '0.2em',
                textShadow: '0 0 20px rgba(0,212,255,0.8), 0 0 60px rgba(0,212,255,0.4)',
              }}
            >
              COSMOS
            </div>
            <div
              style={{
                fontFamily: 'Share Tech Mono, monospace',
                fontSize: 11,
                color: 'rgba(0,212,255,0.5)',
                letterSpacing: '0.4em',
                marginTop: 4,
              }}
            >
              ADVANCED AI INTERFACE
            </div>
          </motion.div>

          {/* Boot log */}
          <div
            style={{
              width: 520,
              maxHeight: 300,
              overflowY: 'hidden',
              background: 'rgba(0,212,255,0.03)',
              border: '1px solid rgba(0,212,255,0.15)',
              padding: '16px 20px',
              borderRadius: 4,
            }}
          >
            {lines.map((line, idx) => (
              <motion.div
                key={idx}
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.15 }}
                style={{
                  fontFamily: 'Share Tech Mono, monospace',
                  fontSize: 11,
                  lineHeight: 1.9,
                  color: lineColor(line.type),
                }}
              >
                <span style={{ color: 'rgba(0,212,255,0.4)' }}>{linePrefix(line.type)}</span>
                {line.text}
              </motion.div>
            ))}
            <div style={{ fontFamily: 'Share Tech Mono, monospace', fontSize: 9,
              color: 'rgba(0,212,255,0.3)', marginTop: 6, letterSpacing: '0.15em' }}>
              PRESS ANY KEY TO SKIP
            </div>
            <span
              style={{
                display: 'inline-block',
                width: 8,
                height: 14,
                background: '#00d4ff',
                marginLeft: 2,
                animation: 'blink 1s step-end infinite',
                verticalAlign: 'middle',
              }}
            />
          </div>

          {/* Progress */}
          <div style={{ width: 520, marginTop: 16 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
              <span style={{ fontFamily: 'Share Tech Mono', fontSize: 10, color: 'rgba(0,212,255,0.5)' }}>
                SYSTEM LOAD
              </span>
              <span style={{ fontFamily: 'Orbitron', fontSize: 10, color: '#00d4ff' }}>
                {progress}%
              </span>
            </div>
            <div className="progress-bar">
              <div
                className="progress-bar-fill"
                style={{ width: `${progress}%` }}
              />
            </div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
