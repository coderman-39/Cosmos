import { useEffect, useRef } from 'react'
import { useCosmosStore } from '../store'

// ── State-colored neon cursor: a crisp dot that tracks the pointer 1:1 and a
// larger ring that lags behind with easing, growing over interactive targets.
// The ring/dot recolor to Cosmos's live state so the cursor feels alive on the
// agent HUD; on the calmer pages it rests at cyan. Disabled entirely on coarse
// pointers (touch) and when reduced motion is requested — the native cursor
// stays in those cases (we only hide it while ours is active). ──
const STATE_COLOR: Record<string, string> = {
  sleeping: '#2a7a99', waking: '#0066dd', idle: '#00d4ff',
  listening: '#26e0ff', thinking: '#a58cff', speaking: '#3fe0a0', executing: '#9944ff',
}

const HOT_SELECTOR =
  'a,button,input,textarea,select,label,[role="button"],[role="tab"],' +
  '.hud-tab,.skill-card,.v2-card,[data-nav],[contenteditable="true"]'

export default function CustomCursor() {
  const dotRef = useRef<HTMLDivElement>(null)
  const ringRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const fine = window.matchMedia('(pointer: fine)').matches
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches ||
      document.documentElement.dataset.motion === 'reduced'
    if (!fine || reduced) return   // leave the native cursor alone

    const dot = dotRef.current, ring = ringRef.current
    if (!dot || !ring) return

    document.documentElement.classList.add('cosmos-cursor')
    dot.style.display = 'block'; ring.style.display = 'block'

    // Pointer target (dot snaps here; ring eases toward it)
    let px = window.innerWidth / 2, py = window.innerHeight / 2
    let rx = px, ry = py
    let visible = false
    let raf = 0

    const move = (e: PointerEvent) => {
      px = e.clientX; py = e.clientY
      if (!visible) { visible = true; rx = px; ry = py; dot.style.opacity = ring.style.opacity = '' }
      dot.style.transform = `translate3d(${px}px, ${py}px, 0) translate(-50%, -50%)`
      const hot = !!(e.target as Element)?.closest?.(HOT_SELECTOR)
      dot.classList.toggle('cur-hot', hot)
      ring.classList.toggle('cur-hot', hot)
    }
    const down = () => { dot.classList.add('cur-down'); ring.classList.add('cur-down') }
    const up = () => { dot.classList.remove('cur-down'); ring.classList.remove('cur-down') }
    const leave = () => { dot.style.opacity = '0'; ring.style.opacity = '0'; visible = false }

    const loop = () => {
      raf = requestAnimationFrame(loop)
      rx += (px - rx) * 0.18; ry += (py - ry) * 0.18
      ring.style.transform = `translate3d(${rx}px, ${ry}px, 0) translate(-50%, -50%)`
    }
    loop()

    window.addEventListener('pointermove', move, { passive: true })
    window.addEventListener('pointerdown', down, { passive: true })
    window.addEventListener('pointerup', up, { passive: true })
    document.addEventListener('pointerleave', leave)

    // Recolor to Cosmos's live state
    const applyColor = (s: string) => {
      const c = STATE_COLOR[s] || STATE_COLOR.idle
      dot.style.setProperty('--cursor-c', c)
      ring.style.setProperty('--cursor-c', c)
    }
    applyColor(useCosmosStore.getState().state)
    const unsub = useCosmosStore.subscribe(s => applyColor(s.state))

    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerdown', down)
      window.removeEventListener('pointerup', up)
      document.removeEventListener('pointerleave', leave)
      unsub()
      document.documentElement.classList.remove('cosmos-cursor')
    }
  }, [])

  return (
    <>
      <div ref={ringRef} className="cur-ring" style={{ display: 'none' }} />
      <div ref={dotRef} className="cur-dot" style={{ display: 'none' }} />
    </>
  )
}
