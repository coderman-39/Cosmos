import { useEffect, useRef } from 'react'
import { CosmosState, useCosmosStore } from '../store'

const N = 20

const STATE_COLOR: Record<string, string> = {
  listening: '#00d4ff', speaking: '#00ff88', thinking: '#ff9500',
  executing: '#9944ff', idle: '#0055aa', sleeping: '#112240', waking: '#003488',
}

export default function VoiceWaveform({ state }: { state: CosmosState }) {
  const barsRef = useRef<(HTMLDivElement | null)[]>([])
  const color   = STATE_COLOR[state] ?? '#0055aa'
  const active  = state === 'listening' || state === 'speaking'

  useEffect(() => {
    // Reduced motion + not actively listening/speaking → static bars, no rAF.
    if (!active && document.documentElement.dataset.motion === 'reduced') {
      barsRef.current.forEach(bar => {
        if (bar) { bar.style.height = '4px'; bar.style.opacity = '0.2' }
      })
      return
    }
    let raf: number, tick = 0
    const animate = () => {
      tick++
      // Imperative read — subscribing to audioLevel would re-render at 60fps
      const audioLevel = useCosmosStore.getState().audioLevel
      barsRef.current.forEach((bar, i) => {
        if (!bar) return
        const base  = active ? 0.5 : 0.08
        const noise = Math.sin(tick * 0.09 + i * 0.55) * 0.3
                    + Math.sin(tick * 0.14 + i * 0.9)  * 0.2
                    + audioLevel * 0.7
        const h = Math.max(3, Math.min(44, (base + noise) * 48))
        bar.style.height  = `${h}px`
        bar.style.opacity = String(active ? 0.90 : 0.20)
      })
      raf = requestAnimationFrame(animate)
    }
    raf = requestAnimationFrame(animate)
    return () => cancelAnimationFrame(raf)
  }, [active])

  return (
    <div style={{ display:'flex', alignItems:'center', gap:3, height:48 }}>
      {Array.from({ length: N }).map((_, i) => (
        <div
          key={i}
          ref={el => { barsRef.current[i] = el }}
          style={{
            width: 3, height: 4, background: color,
            borderRadius: 2, transition: 'height 0.04s ease, opacity 0.1s ease',
            boxShadow: active ? `0 0 5px ${color}` : 'none',
          }}
        />
      ))}
    </div>
  )
}
