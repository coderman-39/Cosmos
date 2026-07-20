// WebAudio earcons — tiny synthesized chirps so the user HEARS that Cosmos
// woke / accepted a command / hit an error / went to sleep, without waiting
// for TTS. No audio assets; a lazy AudioContext is created on first use
// (must be after a user gesture, which the INITIALIZE click guarantees).

import { useCosmosStore } from '../store'

let ctx: AudioContext | null = null

function ac(): AudioContext | null {
  try {
    if (!ctx) {
      const AC = window.AudioContext || (window as any).webkitAudioContext
      if (!AC) return null
      ctx = new AC()
    }
    if (ctx.state === 'suspended') void ctx.resume()
    return ctx
  } catch {
    return null
  }
}

function tone(freq: number, dur: number, delay = 0,
              type: OscillatorType = 'sine', gain = 0.05) {
  const c = ac()
  if (!c) return
  const osc = c.createOscillator()
  const g = c.createGain()
  osc.type = type
  osc.frequency.value = freq
  const t0 = c.currentTime + delay
  g.gain.setValueAtTime(0, t0)
  g.gain.linearRampToValueAtTime(gain, t0 + 0.012)
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur)
  osc.connect(g)
  g.connect(c.destination)
  osc.start(t0)
  osc.stop(t0 + dur + 0.05)
}

export type Earcon = 'wake' | 'accept' | 'error' | 'sleep'

export function playEarcon(kind: Earcon) {
  if (!useCosmosStore.getState().earconsEnabled) return
  switch (kind) {
    case 'wake':   tone(880, 0.09); tone(1318, 0.14, 0.07); break            // rising
    case 'accept': tone(1174, 0.07); break                                    // single blip
    case 'error':  tone(330, 0.14, 0, 'square', 0.04); tone(247, 0.2, 0.11, 'square', 0.04); break
    case 'sleep':  tone(1318, 0.09); tone(880, 0.14, 0.07); break            // falling
  }
}
