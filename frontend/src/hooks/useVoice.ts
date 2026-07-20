import { useEffect, useRef, useCallback } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { useCosmosStore } from '../store'
import { playEarcon } from '../lib/earcons'
import { truncateForSpeech, remainderChunks } from '../lib/speech'

const SpeechRecognition =
  (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition

// ─── Noise filter ──────────────────────────────────────────────────────────────

const NOISE_WORDS = new Set([
  'uh', 'um', 'hm', 'hmm', 'ah', 'oh', 'er', 'mm', 'eh',
  'huh', 'mhm', 'ugh', 'uhh', 'umm', 'shh',
])

function isNoise(text: string): boolean {
  const t = text.trim().toLowerCase().replace(/[.,!?]/g, '')
  if (t.length < 3) return true
  const words = t.split(/\s+/)
  if (words.length === 1 && NOISE_WORDS.has(words[0])) return true
  if (words.every(w => NOISE_WORDS.has(w))) return true
  return false
}

// ─── Command extraction ────────────────────────────────────────────────────────

// Words that can appear between "cosmos" and the actual command
const FILLER = /^(wake\s*up|please|can\s*you|could\s*you|would\s*you|go\s*ahead\s*and|,)[\s,]*/i

// Wake-word tolerance: Web Speech regularly mishears "cosmos" as "freddy",
// "fry day", or "cosmos's", and users often lead with a junk token ("so
// cosmos…"). Lenient mode accepts these; STRICT mode (used while TTS is
// playing) accepts only an exact leading cosmos/hey-cosmos so Cosmos's own
// voice can never wake her.
const WAKE_RE = /^(?:(?:hey|ok|okay|so|um|uh|yo)[\s,]+)?(?:cosmos'?s?|freddie|freddy|fry\s?day)\b[\s,.!?]*/i

export function extractCosmosCommand(text: string, strict = false): string | null {
  const trimmed = text.trim()

  if (strict) {
    const lower = trimmed.toLowerCase()
    let rest: string
    if (lower.startsWith('hey cosmos'))  rest = trimmed.slice('hey cosmos'.length).trim()
    else if (lower.startsWith('cosmos')) rest = trimmed.slice('cosmos'.length).trim()
    else return null
    return rest.replace(FILLER, '').trim() || null
  }

  const m = trimmed.match(WAKE_RE)
  if (!m) return null
  return trimmed.slice(m[0].length).replace(FILLER, '').trim() || null
}

// Bare interrupt words that cut TTS dead WITHOUT the wake prefix — but only
// while Cosmos is actually speaking, so ambient "stop" can't cancel anything.
const INTERRUPT_RE = /^(stop|shut\s*up|be\s*quiet|quiet|never\s*mind|nevermind|cancel|enough)$/i

function isInterrupt(text: string): boolean {
  return INTERRUPT_RE.test(text.trim().replace(/[.,!?]+$/, ''))
}

// Echo guard: fraction of `heard`'s meaningful tokens that also appear in what
// Cosmos just spoke. Above ~0.6 the mic almost certainly picked up her own TTS.
export function tokenOverlap(heard: string, spoken: string): number {
  const tok = (s: string) =>
    s.toLowerCase().replace(/[^a-z0-9\s]/g, ' ').split(/\s+/).filter(w => w.length > 2)
  const H = tok(heard)
  if (!H.length) return 0
  const S = new Set(tok(spoken))
  return H.filter(w => S.has(w)).length / H.length
}

// How long after a wake-only hit / a question from Cosmos the mic accepts
// commands WITHOUT the wake prefix ("attentive window").
const ATTENTIVE_MS = 7_000
// Grace after releasing push-to-talk — the final transcript often lands late.
const PTT_GRACE_MS = 1_500

function isWakeOnly(cmd: string): boolean {
  return /^(wake\s*up|hello|hi|hey)$/i.test(cmd.trim())
}

function isSleepCmd(cmd: string): boolean {
  return /\b(sleep|goodbye|go\s*offline|shut\s*down|goodnight)\b/i.test(cmd)
}

// ─── Hook ──────────────────────────────────────────────────────────────────────

export function useVoice(onCommand: (text: string) => void,
                         onPrefetch?: () => void) {
  // Selector + shallow compare — a whole-store subscription would re-render the
  // caller (App) on every setAudioLevel tick
  const { state, setState, addMessage, setAudioLevel, setPartialTranscript,
          voiceEnabled } = useCosmosStore(
    useShallow(s => ({
      state: s.state, setState: s.setState, addMessage: s.addMessage,
      setAudioLevel: s.setAudioLevel, setPartialTranscript: s.setPartialTranscript,
      voiceEnabled: s.settings.voiceEnabled,
    })),
  )

  const recognitionRef  = useRef<any>(null)
  const restartTimerRef = useRef<ReturnType<typeof setTimeout>>()
  const isActiveRef     = useRef(false)   // true once initialized
  const isSpeakingRef   = useRef(false)   // true while TTS is playing
  const goSleepNextRef  = useRef(false)   // true = next speech.onend → wake loop
  const onCommandRef    = useRef(onCommand)
  const onPrefetchRef   = useRef(onPrefetch)
  const prefetchSentRef = useRef(false)   // one prefetch per utterance
  const voiceRef        = useRef<SpeechSynthesisVoice | null>(null)
  const currentUttRef   = useRef<SpeechSynthesisUtterance | null>(null)
  const currentAudioRef = useRef<HTMLAudioElement | null>(null)   // backend TTS <audio>
  const speakSeqRef     = useRef(0)                               // stale-response guard
  const attentiveUntilRef = useRef(0)     // epoch ms — no-wake-word window
  const lastSpokenRef   = useRef<{ text: string; ts: number } | null>(null)  // echo guard
  const pttRef          = useRef(false)   // push-to-talk held
  const pttGraceRef     = useRef(0)       // epoch ms — accept finals shortly after release
  const micEventRef     = useRef(Date.now())  // last recognizer event — mic-health watchdog
  const remainderRef    = useRef<string[]>([]) // unspoken sentence chunks of the last reply
  const leadQueueRef    = useRef<string[]>([]) // lead continuations spoken after the current utterance
  const leadSeqRef      = useRef(-1)           // speakSeq of the current LEAD utterance
  // Real mic pipeline (replaces the Math.random waveform when granted)
  const audioCtxRef     = useRef<AudioContext | null>(null)
  const micStreamRef    = useRef<MediaStream | null>(null)
  const analyserRef     = useRef<AnalyserNode | null>(null)
  const noiseFloorRef   = useRef(0.008)
  const voiceSinceRef   = useRef(0)   // ms timestamp speech energy started
  const quietSinceRef   = useRef(0)   // ms timestamp silence started

  useEffect(() => { onCommandRef.current = onCommand }, [onCommand])
  useEffect(() => { onPrefetchRef.current = onPrefetch }, [onPrefetch])

  // Pick and cache TTS voice once
  useEffect(() => {
    function pick() {
      const voices = window.speechSynthesis?.getVoices() ?? []
      return (
        voices.find(v => v.name === 'Samantha') ||
        voices.find(v => v.name.includes('Google UK English Female')) ||
        voices.find(v => v.lang === 'en-GB' && !v.name.includes('Male')) ||
        voices.find(v => v.lang.startsWith('en')) ||
        null
      )
    }
    voiceRef.current = pick()
    const handler = () => { voiceRef.current = pick() }
    window.speechSynthesis?.addEventListener('voiceschanged', handler)
    return () => window.speechSynthesis?.removeEventListener('voiceschanged', handler)
  }, [])

  // ─── TTS ────────────────────────────────────────────────────────────────────
  // opts.addToChat=false → TTS only (protocol `speak` events / ask_user questions
  // are interim voice, they must NOT become permanent chat messages).
  // opts.expectReply=true → after this utterance finishes, keep an "attentive"
  // window open so the user can answer WITHOUT re-saying the wake word.
  const speak = useCallback((text: string, opts?: {
    addToChat?: boolean; expectReply?: boolean; verbatim?: boolean
    queued?: boolean; lead?: boolean
  }) => {
    // Chat insertion happens BEFORE any TTS guard — in browsers without
    // speechSynthesis the transcript must still show Cosmos's replies.
    if (opts?.addToChat !== false) {
      addMessage({ role: 'cosmos', text })
      // Full replies retain their unspoken tail — "cosmos, read the rest".
      remainderRef.current = remainderChunks(text)
    }

    // queued: a lead CONTINUATION — it belongs to ONE specific lead
    // utterance. Enqueue only while that exact lead is still playing; in
    // every other case (barge-in bumped the seq, the lead already ended,
    // an interim speak took over) it is DROPPED, never spoken standalone —
    // a disembodied "second sentence" out of context is worse than silence.
    if (opts?.queued) {
      if (isSpeakingRef.current && speakSeqRef.current === leadSeqRef.current) {
        leadQueueRef.current.push(text)
      }
      return
    }

    // TTS disabled in Settings → text-only. Still arm the attentive window so
    // voice follow-ups to questions work.
    if (!useCosmosStore.getState().settings.ttsEnabled) {
      if (opts?.expectReply) attentiveUntilRef.current = Date.now() + ATTENTIVE_MS
      return
    }

    // Detach the PREVIOUS utterance/audio's handlers + cancel before starting
    // the new one (Chrome fires the cancelled utterance's onend/onerror async).
    cancelSpeech()
    isSpeakingRef.current = true
    // Bump the sequence — any in-flight /api/tts fetch from a prior speak() is
    // now stale and must NOT start playing when it resolves.
    const seq = ++speakSeqRef.current
    if (opts?.lead) leadSeqRef.current = seq
    const expectReply = opts?.expectReply === true
    // Keep the mic ALIVE during TTS so the user can barge in with
    // "cosmos <command>". Cosmos doesn't trigger itself: while speaking,
    // onresult discards every transcript that doesn't start with the wake word.
    startListening()

    const spoken = opts?.verbatim ? text : truncateForSpeech(text)
    // Echo guard reference: what the mic may pick back up.
    lastSpokenRef.current = { text: spoken, ts: Date.now() }

    // After speech, drop back to 'executing' if a run is still in flight —
    // interim updates (say/ask_user/confirm nudges) must not knock the orb
    // out of EXECUTING for the rest of the task.
    const settle = () => {
      isSpeakingRef.current = false
      currentUttRef.current = null
      currentAudioRef.current = null
      setState(useCosmosStore.getState().isExecuting ? 'executing' : 'idle')
    }

    // Shared post-speech lifecycle: settle state, then resume listening (or go
    // to sleep if that intent is pending). Identical for backend audio + Web
    // Speech so barge-in / wake-word behavior is preserved either way.
    const onDone = () => {
      // A queued lead continuation picks up seamlessly — state stays
      // 'speaking', the mic stays open, and the sleep intent (never set by
      // lead speech) is untouched. If TTS was toggled off mid-utterance,
      // drop the queue and settle normally (speak()'s disabled early-return
      // would otherwise leave isSpeakingRef/'speaking' stuck forever).
      const next = useCosmosStore.getState().settings.ttsEnabled
        ? leadQueueRef.current.shift()
        : undefined
      if (next) {
        speak(next, { addToChat: false, verbatim: true })
        return
      }
      leadQueueRef.current = []
      settle()
      // Extend the echo-guard window through the END of playback.
      if (lastSpokenRef.current) lastSpokenRef.current.ts = Date.now()
      // Cosmos asked something / just woke — accept the reply without the
      // wake prefix for a short attentive window.
      if (expectReply) attentiveUntilRef.current = Date.now() + ATTENTIVE_MS
      const goSleep = goSleepNextRef.current
      goSleepNextRef.current = false
      if (goSleep) {
        setState('sleeping')
        startListening()          // stay stopped — still listen for wake word
      } else {
        setTimeout(startListening, 300)
      }
    }

    // ── Web Speech fallback (unchanged behavior) ────────────────────────────
    const speakWebSpeech = () => {
      if (!window.speechSynthesis) {
        // No TTS available at all — restore HUD state and bail.
        settle()
        return
      }
      const utt  = new SpeechSynthesisUtterance(spoken)
      utt.rate   = 1.05 * useCosmosStore.getState().settings.voiceRate
      utt.pitch  = 0.78
      utt.volume = 1.0
      if (voiceRef.current) utt.voice = voiceRef.current

      utt.onstart = () => setState('speaking')
      utt.onend   = onDone
      utt.onerror = () => { settle(); setTimeout(startListening, 300) }

      currentUttRef.current = utt
      setState('speaking')
      window.speechSynthesis.speak(utt)
    }

    // ── Buffered backend audio (macOS say / ElevenLabs full render) ─────────
    const playBuffered = () => {
      fetch('/api/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: spoken }),
      })
        .then(async res => {
          if (seq !== speakSeqRef.current) return          // superseded by a newer speak()
          if (!res.ok) { speakWebSpeech(); return }        // 503/4xx → Web Speech
          const blob = await res.blob()
          if (seq !== speakSeqRef.current) return           // superseded while downloading
          if (!blob.size) { speakWebSpeech(); return }

          const url   = URL.createObjectURL(blob)
          const audio = new Audio(url)
          audio.volume = 1.0
          audio.playbackRate = useCosmosStore.getState().settings.voiceRate
          // Wire the SAME lifecycle the utterance had.
          audio.onplay  = () => setState('speaking')
          audio.onended = () => { URL.revokeObjectURL(url); onDone() }
          audio.onerror = () => {
            URL.revokeObjectURL(url)
            // Playback broke — fall back to Web Speech rather than going silent.
            if (seq === speakSeqRef.current) speakWebSpeech()
            else settle()
          }
          currentAudioRef.current = audio
          audio.play().catch(() => {
            URL.revokeObjectURL(url)
            if (seq === speakSeqRef.current) speakWebSpeech()
            else settle()
          })
        })
        .catch(() => {
          // Network/proxy failure → Web Speech fallback.
          if (seq === speakSeqRef.current) speakWebSpeech()
        })
    }

    // ── Streaming backend audio FIRST — ElevenLabs /stream through
    // MediaSource: first sound in ~300ms instead of after the full render.
    // Resolves false when unavailable so the buffered path takes over.
    const playStreaming = async (): Promise<boolean> => {
      const MS = (window as any).MediaSource
      if (!MS || !MS.isTypeSupported?.('audio/mpeg')) return false
      let res: Response
      try {
        res = await fetch('/api/tts/stream', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: spoken }),
        })
      } catch { return false }
      if (seq !== speakSeqRef.current) { try { res.body?.cancel() } catch {} ; return true }
      if (!res.ok || !res.body) return false

      const ms = new MS() as MediaSource
      const url = URL.createObjectURL(ms)
      const audio = new Audio(url)
      audio.volume = 1.0
      audio.playbackRate = useCosmosStore.getState().settings.voiceRate
      audio.onplay  = () => setState('speaking')
      audio.onended = () => { URL.revokeObjectURL(url); onDone() }
      audio.onerror = () => {
        URL.revokeObjectURL(url)
        if (seq === speakSeqRef.current) speakWebSpeech()
        else settle()
      }
      currentAudioRef.current = audio

      ms.addEventListener('sourceopen', () => {
        let sb: SourceBuffer
        try { sb = ms.addSourceBuffer('audio/mpeg') } catch { audio.onerror?.(new Event('error') as any); return }
        const reader = res.body!.getReader()
        const pump = () => {
          reader.read().then(({ done, value }) => {
            if (seq !== speakSeqRef.current) { try { reader.cancel() } catch {} ; return }
            if (done) { try { ms.endOfStream() } catch {} ; return }
            const append = () => {
              try { sb.appendBuffer(value!) } catch { return }
              sb.addEventListener('updateend', pump, { once: true })
            }
            if (sb.updating) sb.addEventListener('updateend', append, { once: true })
            else append()
          }).catch(() => { try { ms.endOfStream() } catch {} })
        }
        pump()
        audio.play().catch(() => {
          if (seq === speakSeqRef.current) speakWebSpeech()
          else settle()
        })
      }, { once: true })
      return true
    }

    setState('speaking')
    playStreaming().then(streamed => {
      if (!streamed && seq === speakSeqRef.current) playBuffered()
    })
  }, [setState, addMessage])

  // Kill in-flight TTS without side effects: detach handlers BEFORE cancel()
  // (Chrome fires the cancelled utterance's onend/onerror asynchronously —
  // they must not run settle()/startListening and stomp the barge-in command's
  // state), then reset the speaking flag.
  function cancelSpeech() {
    // Invalidate any in-flight /api/tts fetch so it doesn't start playing after
    // a barge-in / new utterance.
    speakSeqRef.current++
    // Pending lead continuations die with the utterance they were queued behind.
    leadQueueRef.current = []
    if (currentUttRef.current) {
      currentUttRef.current.onend = null
      currentUttRef.current.onerror = null
      currentUttRef.current = null
    }
    window.speechSynthesis?.cancel()
    // Stop backend-audio playback too (barge-in must silence it immediately).
    if (currentAudioRef.current) {
      currentAudioRef.current.onended = null
      currentAudioRef.current.onerror = null
      try { currentAudioRef.current.pause() } catch {}
      currentAudioRef.current = null
    }
    isSpeakingRef.current = false
  }

  // ─── Single continuous listen loop ────────────────────────────────────────────
  function stopListening() {
    if (recognitionRef.current) {
      try { recognitionRef.current.abort() } catch {}
      recognitionRef.current = null
    }
    clearTimeout(restartTimerRef.current)
  }

  function startListening() {
    if (!SpeechRecognition || !isActiveRef.current) return
    if (!useCosmosStore.getState().settings.voiceEnabled) return  // mic off in Settings
    if (recognitionRef.current) return  // already running
    // NOTE: deliberately NO isSpeakingRef guard — the mic stays open during
    // TTS for barge-in; onresult wake-word-gates everything while speaking.

    const rec = new SpeechRecognition()
    rec.lang = 'en-US'
    rec.continuous = false
    rec.interimResults = true
    // 3 alternatives: when the top hypothesis mangles the wake word
    // ("fried a"), a runner-up often has it.
    rec.maxAlternatives = 3

    rec.onresult = (e: any) => {
      micEventRef.current = Date.now()
      let interim = '', final = ''
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript
        if (e.results[i].isFinal) final += t
        else interim += t
      }

      // INSTANT interrupt: a bare "stop"/"quiet"/"never mind" while Cosmos is
      // talking cuts TTS from the INTERIM transcript (~200ms) — no wake word,
      // no waiting for the final. If a task is executing, stop that too.
      if (isSpeakingRef.current && isInterrupt(final || interim)) {
        cancelSpeech()
        goSleepNextRef.current = false
        setPartialTranscript('')
        if (useCosmosStore.getState().isExecuting) onCommandRef.current('stop')
        return
      }

      const attentive = pttRef.current ||
        Date.now() < pttGraceRef.current ||
        Date.now() < attentiveUntilRef.current

      // Show partial (wake-prefixed, or anything during attentive/PTT windows)
      if (interim && !isSpeakingRef.current) {
        const partialCmd = extractCosmosCommand(interim) ?? (attentive ? interim.trim() : null)
        setPartialTranscript(partialCmd ? `cosmos ${partialCmd}` : '')
        // A command is clearly coming — let the backend prefetch (focus-probe
        // warm-up) inside the ~0.5-1s Chrome endpointing tail. Once per
        // utterance; the frame is side-effect-free by protocol contract.
        if (partialCmd && !prefetchSentRef.current) {
          prefetchSentRef.current = true
          onPrefetchRef.current?.()
        }
      }

      if (!final.trim()) return
      setPartialTranscript('')
      prefetchSentRef.current = false

      // Ignore noise
      if (isNoise(final)) return

      // Echo guard: if most of the final's words are words Cosmos just spoke,
      // the mic heard her own TTS — drop it before any wake processing.
      const spoken = lastSpokenRef.current
      if (spoken && (isSpeakingRef.current || Date.now() - spoken.ts < 15_000) &&
          tokenOverlap(final, spoken.text) > 0.6) {
        return
      }

      // Wake extraction: STRICT while Cosmos is speaking (echo defense);
      // lenient variants ("freddy", "fry day", "so cosmos…") otherwise.
      // During the attentive/PTT windows no wake word is needed at all.
      let cmd = extractCosmosCommand(final, isSpeakingRef.current)
      // Wake-word recovery from runner-up hypotheses (never while speaking —
      // strict echo defense applies to the primary only).
      if (!cmd && !isSpeakingRef.current) {
        for (let i = e.resultIndex; i < e.results.length && !cmd; i++) {
          if (!e.results[i].isFinal) continue
          for (let a = 1; a < e.results[i].length && !cmd; a++) {
            cmd = extractCosmosCommand(e.results[i][a].transcript || '')
          }
        }
      }
      if (!cmd && !isSpeakingRef.current && attentive) cmd = final.trim()
      if (!cmd) {
        // Honest feedback: real speech was heard but lacked the wake word —
        // surface it briefly instead of silently swallowing it.
        if (!isSpeakingRef.current && final.trim().split(/\s+/).length >= 2) {
          useCosmosStore.getState().setLastDiscarded(final.trim().slice(0, 60))
        }
        return
      }

      // Barge-in: "cosmos <command>" landed while TTS is playing — cut the
      // current utterance dead and fall through to the normal command path.
      if (isSpeakingRef.current) {
        cancelSpeech()
        goSleepNextRef.current = false   // interrupted utterance's sleep intent dies with it
      }

      // Consumed — close the no-wake-word windows.
      attentiveUntilRef.current = 0
      pttGraceRef.current = 0

      // Handle sleep
      if (isSleepCmd(cmd)) {
        playEarcon('sleep')
        goSleepNextRef.current = true
        speak('Going offline, sir.')
        return
      }

      // Handle wake-only (e.g. "cosmos wake up", "cosmos hello")
      if (isWakeOnly(cmd)) {
        handleWake()
        return
      }

      // Real command — process it
      playEarcon('accept')
      addMessage({ role: 'user', text: cmd })
      setState('thinking')
      recognitionRef.current = null   // stop current session
      onCommandRef.current(cmd)
    }

    rec.onerror = (e: any) => {
      micEventRef.current = Date.now()
      recognitionRef.current = null
      if (e.error !== 'aborted') {
        restartTimerRef.current = setTimeout(startListening, 600)
      }
    }

    rec.onend = () => {
      micEventRef.current = Date.now()
      recognitionRef.current = null
      // Interim-only utterances (wake heard, no final transcript) must not
      // suppress the NEXT command's prefetch warm-up.
      prefetchSentRef.current = false
      // Restart even while speaking (barge-in needs the mic open during TTS);
      // the 120ms debounce keeps Chrome's frequent onend from storming.
      if (isActiveRef.current) {
        restartTimerRef.current = setTimeout(startListening, 120)
      }
    }

    recognitionRef.current = rec
    try { rec.start() } catch { recognitionRef.current = null }
  }

  async function handleWake() {
    stopListening()
    playEarcon('wake')
    setState('waking')
    await new Promise(r => setTimeout(r, 150))
    setState('idle')
    const hour = new Date().getHours()
    const greeting =
      hour < 12 ? 'Good morning, sir.' :
      hour < 17 ? 'Good afternoon, sir.' :
                  'Good evening, sir.'
    // expectReply: the follow-up command needs no second "cosmos".
    speak(`${greeting} Ready.`, { expectReply: true })
  }

  // Real mic analyser — honest waveform + VAD ducking. Requested once at
  // initialize(); denial silently keeps the synthetic waveform.
  async function initMicAnalyser() {
    if (analyserRef.current || !navigator.mediaDevices?.getUserMedia) return
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true } })
      const AC = window.AudioContext || (window as any).webkitAudioContext
      const ctx = new AC()
      const src = ctx.createMediaStreamSource(stream)
      const an = ctx.createAnalyser()
      an.fftSize = 512
      src.connect(an)
      audioCtxRef.current = ctx
      micStreamRef.current = stream
      analyserRef.current = an
    } catch { /* mic denied — synthetic waveform remains */ }
  }

  function micRms(): number | null {
    const an = analyserRef.current
    if (!an) return null
    const buf = new Uint8Array(an.fftSize)
    an.getByteTimeDomainData(buf)
    let sum = 0
    for (let i = 0; i < buf.length; i++) {
      const v = (buf[i] - 128) / 128
      sum += v * v
    }
    return Math.sqrt(sum / buf.length)
  }

  // ─── Audio level animation ────────────────────────────────────────────────────
  useEffect(() => {
    let raf: number
    let smoothed = useCosmosStore.getState().audioLevel
    const active = state === 'listening' || state === 'speaking'
    const tick = () => {
      if (active) {
        const rms = micRms()
        let target: number
        if (rms !== null) {
          // REAL signal: adaptive noise floor + normalized level.
          noiseFloorRef.current = Math.min(
            noiseFloorRef.current * 0.995 + rms * 0.005, 0.05)
          target = Math.max(0, Math.min(1, (rms - noiseFloorRef.current) * 9))
          // VAD ducking: the user speaking over Cosmos's TTS drops its volume
          // so the recognizer hears THEM — restored after 500ms of silence.
          const now = Date.now()
          const speaking = rms > noiseFloorRef.current * 3 + 0.01
          if (speaking) { voiceSinceRef.current ||= now; quietSinceRef.current = 0 }
          else { quietSinceRef.current ||= now; voiceSinceRef.current = 0 }
          const audioEl = currentAudioRef.current
          if (audioEl) {
            if (voiceSinceRef.current && now - voiceSinceRef.current > 250) {
              audioEl.volume = 0.25
            } else if (quietSinceRef.current && now - quietSinceRef.current > 500) {
              audioEl.volume = 1.0
            }
          }
        } else {
          target = 0.05 + Math.random() * 0.55   // synthetic fallback
        }
        smoothed = smoothed + (target - smoothed) * 0.08   // low-pass: ~8% each frame
        setAudioLevel(smoothed)
      } else {
        smoothed = smoothed * 0.85   // decay to zero
        if (smoothed < 0.01) {
          // Settled at zero — stop the loop entirely (no setAudioLevel(0) spam
          // notifying the store 60×/s forever while idle). The effect re-runs
          // on the next state change and restarts the loop.
          if (useCosmosStore.getState().audioLevel !== 0) setAudioLevel(0)
          return
        }
        setAudioLevel(smoothed)
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [state, setAudioLevel])

  // Show listening state while the mic is open
  useEffect(() => {
    if (!isActiveRef.current) return
    const interval = setInterval(() => {
      if (recognitionRef.current && state === 'idle') {
        setState('listening')
      } else if (!recognitionRef.current && state === 'listening') {
        setState('idle')
      }
    }, 500)
    return () => clearInterval(interval)
  }, [state, setState])

  // ─── Public API ──────────────────────────────────────────────────────────────
  const initialize = useCallback(() => {
    if (!SpeechRecognition) { console.warn('[COSMOS] SpeechRecognition not supported'); return }
    isActiveRef.current = true
    setState('idle')
    setAudioLevel(0)
    startListening()
    void initMicAnalyser()   // real waveform + VAD (denial keeps synthetic)
  }, [setState, setAudioLevel])

  const manualWake = useCallback(() => {
    if (!isActiveRef.current) { initialize(); return }
    stopListening()
    handleWake()
  }, [initialize])

  const manualCommand = useCallback((text: string) => {
    stopListening()
    addMessage({ role: 'user', text })
    setState('thinking')
    onCommandRef.current(text)
  }, [addMessage, setState])

  const goSleep = useCallback(() => {
    goSleepNextRef.current = true
  }, [])

  // "cosmos, read the rest / continue" — speak the next unspoken chunk of the
  // last reply. expectReply keeps the attentive window open so a bare
  // "continue" works for the chunk after that too.
  const speakRemainder = useCallback(() => {
    const next = remainderRef.current.shift()
    if (!next) {
      speak('That was everything, sir.', { addToChat: false })
      return
    }
    const more = remainderRef.current.length
    speak(more ? `${next} … say continue for more.` : next,
          { addToChat: false, verbatim: true, expectReply: more > 0 })
  }, [speak])

  const hasRemainder = useCallback(() => remainderRef.current.length > 0, [])

  // Hold-Space push-to-talk: while held, the wake word is not required. On
  // press: silence any TTS and make sure the mic is open. On release: a short
  // grace window still accepts the (often late) final transcript.
  const setPushToTalk = useCallback((active: boolean) => {
    if (active && !pttRef.current) {
      pttRef.current = true
      if (!isActiveRef.current) { initialize(); return }
      cancelSpeech()
      playEarcon('wake')
      startListening()
      if (useCosmosStore.getState().state === 'idle') setState('listening')
    } else if (!active && pttRef.current) {
      pttRef.current = false
      pttGraceRef.current = Date.now() + PTT_GRACE_MS
    }
  }, [setState])

  // Mic-health watchdog: the VOICE chip must not stay green while Web Speech
  // has silently died. Recognizer events refresh micEventRef; >20s of silence
  // while we're supposed to be listening = stale mic.
  useEffect(() => {
    const iv = setInterval(() => {
      const st = useCosmosStore.getState()
      if (!isActiveRef.current || !st.settings.voiceEnabled ||
          st.state === 'thinking' || st.state === 'executing' || st.state === 'speaking') {
        st.setMicAlive(true)   // not supposed to be listening right now — no alarm
        return
      }
      st.setMicAlive(Date.now() - micEventRef.current < 20_000)
    }, 5_000)
    return () => clearInterval(iv)
  }, [])

  // React to the Settings mic toggle: off → stop the recognizer; on → resume.
  useEffect(() => {
    if (!isActiveRef.current) return
    if (voiceEnabled) {
      startListening()
    } else {
      stopListening()
      if (useCosmosStore.getState().state === 'listening') setState('idle')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voiceEnabled])

  useEffect(() => () => {
    isActiveRef.current = false; stopListening()
    micStreamRef.current?.getTracks().forEach(t => t.stop())
    audioCtxRef.current?.close()
  }, [])

  return { speak, initialize, manualWake, manualCommand, goSleep, setPushToTalk,
           speakRemainder, hasRemainder,
           hasSpeechAPI: !!SpeechRecognition }
}
