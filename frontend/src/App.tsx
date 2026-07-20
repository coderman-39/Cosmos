import { useState, useCallback, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useShallow } from 'zustand/react/shallow'
import { useCosmosStore } from './store'
import { useVoice } from './hooks/useVoice'
import { useWebSocket } from './hooks/useWebSocket'
import type { ServerMessage } from './types/protocol'
import { playEarcon } from './lib/earcons'
import { isReadTheRest, cleanForSpeech, fixPronunciation } from './lib/speech'
import { WEATHER_CITIES } from './store'
import CosmosOrb from './components/CosmosOrb'
import HudTabs from './components/HudTabs'
import MemoryPanel from './components/MemoryPanel'
import AuditPanel from './components/AuditPanel'
import SettingsPanel from './components/SettingsPanel'
import FlightRecorder from './components/FlightRecorder'
import VoiceWaveform from './components/VoiceWaveform'
import BootSequence from './components/BootSequence'
import ChatLog from './components/ChatLog'
import TaskBoard from './components/TaskBoard'
import AgentActivity from './components/AgentActivity'
import ConfirmBar from './components/ConfirmBar'
import SpaceBackdrop from './components/SpaceBackdrop'
import CommandPalette from './components/CommandPalette'
import NavMenu from './components/NavMenu'
import CustomCursor from './components/CustomCursor'
import HomePage from './components/HomePage'
import SkillsPage from './components/SkillsPage'
import KinesisPage from './components/KinesisPage'
import NexusPage from './components/NexusPage'
import DossierPage from './components/DossierPage'
import VisionPage from './components/VisionPage'
import ConnectorsPage from './components/ConnectorsPage'
import SlackBridgePage from './components/SlackBridgePage'
import PanelPage from './components/PanelPage'
import MutatePage from './components/MutatePage'

const STATE_COLOR: Record<string, string> = {
  sleeping:'rgba(0,212,255,0.25)', waking:'#0066dd', idle:'rgba(0,212,255,0.65)',
  listening:'#00d4ff', thinking:'#ff9500', speaking:'#00ff88', executing:'#9944ff',
}
const STATE_LABEL: Record<string, string> = {
  sleeping:'○  STANDBY', waking:'◌  INITIALIZING', idle:'◉  ONLINE',
  listening:'●  LISTENING', thinking:'◈  PROCESSING', speaking:'◉  RESPONDING', executing:'◉  EXECUTING',
}

// Commands that trigger a new conversation — matched EXACTLY (after trimming
// trailing punctuation), never by substring: "open a new chat window in slack"
// must reach the agent, not wipe the conversation.
const NEW_CHAT_PHRASES = ['start a new chat', 'new chat', 'new conversation', 'start new convo',
  'fresh start', 'clear chat', 'new session']

// Safety-timer windows: how long the UI tolerates total backend silence before
// declaring the run stuck and recovering.
const SAFETY_MS_COMMAND = 25_000   // waiting for the backend's first reaction
const SAFETY_MS_RUN     = 60_000   // mid-run — a single slow LLM call can be quiet for a while

const SENTENCE_RE = /[^.!?]+[.!?]+(?:\s|$)/g

// Everything before the first blank line / list / heading in a PARTIAL stream,
// plus whether that structural boundary has actually appeared yet.
function leadOf(buf: string): { text: string; boundary: boolean } {
  const lines = buf.replace(/\r\n/g, '\n').split('\n')
  const lead: string[] = []
  let boundary = false
  for (const raw of lines) {
    const l = raw.trim()
    if (!l) { if (lead.length) { boundary = true }; if (boundary) break; else continue }
    if (/^([-*•]\s|#{1,4}\s|\d+[.)]\s|\|)/.test(l)) { if (lead.length) boundary = true; break }
    lead.push(l)
  }
  return { text: lead.join(' ').trim(), boundary }
}

// Returns the spoken LEAD as soon as it's worth starting the voice on — a
// structural boundary appeared, two full sentences accumulated, or ONE
// substantial sentence is complete (its follow-up is queued as it completes).
// A short fragment ("Done.") may still be growing — the quiescence timer in
// the delta handler catches it at stream-tail instead. Returns null while the
// lead may still be growing.
function completeLead(buf: string): string | null {
  const { text, boundary } = leadOf(buf)
  if (!text) return null
  if (boundary) return text
  const sentences = text.match(SENTENCE_RE)
  if (!sentences) return null
  return sentences.length >= 2 || text.length >= 40 ? text : null
}

export default function App() {
  const [booted,  setBooted]  = useState(false)
  const [started, setStarted] = useState(false)
  const [clock,   setClock]   = useState(new Date())
  const [paletteOpen, setPaletteOpen] = useState(false)

  const speakRef        = useRef<((t:string, opts?:{addToChat?:boolean; expectReply?:boolean; verbatim?:boolean; queued?:boolean; lead?:boolean})=>void)|null>(null)
  const sendRef         = useRef<((d:object)=>void)|null>(null)
  const isConnRef       = useRef(false)
  const weatherRef      = useRef<any>(null)
  const voiceGoSleepRef = useRef<(()=>void)|null>(null)
  const speakRemainderRef = useRef<(()=>void)|null>(null)
  const safetyTimerRef  = useRef<ReturnType<typeof setTimeout>>()
  // Streaming-TTS: accumulate the streamed answer and speak the LEAD sentence
  // the moment it's complete, so the voice starts with the text (not after it).
  const streamBufRef    = useRef('')
  const spokeLeadRef    = useRef(false)
  // How many lead sentences are already spoken/queued (max 2 — the TTS budget).
  // 1 means sentence 2 gets queued behind the current utterance as it completes.
  const leadSpokenCountRef = useRef(0)
  // Delta-quiescence fallback: a stalled/finished stream whose lead never hit
  // the completeLead bar ("Done, sir.") speaks at stream-tail instead of
  // waiting behind the verify critic for the `response` frame.
  const quiescenceRef   = useRef<ReturnType<typeof setTimeout>>()
  // Set on tool_start: the NEXT model turn's first delta replaces (rather
  // than concatenates onto) the previous turn's streamed preamble.
  const turnBrokeRef    = useRef(false)

  // ── rAF-batched store writes for response_delta ──────────────────────────
  // Every delta used to hit appendStreaming() directly — one store set() (and
  // one persist serialization) per token. Deltas now accumulate here and land
  // in the store at most once per animation frame. The lead-speech logic is
  // NOT affected: streamBufRef is still updated synchronously per delta.
  const pendingDeltaRef = useRef('')
  const deltaRafRef     = useRef<number | null>(null)

  // Batch a delta for the next frame's single appendStreaming().
  const queueStreamingDelta = useCallback((text: string) => {
    pendingDeltaRef.current += text
    if (deltaRafRef.current === null) {
      deltaRafRef.current = requestAnimationFrame(() => {
        deltaRafRef.current = null
        const t = pendingDeltaRef.current
        pendingDeltaRef.current = ''
        if (t) useCosmosStore.getState().appendStreaming(t)
      })
    }
  }, [])

  // Drop any un-flushed tail — used when the buffer is being cleared anyway
  // (response landed / reset / new run), so a stale flush can't resurrect it.
  const discardStreamingDelta = useCallback(() => {
    if (deltaRafRef.current !== null) {
      cancelAnimationFrame(deltaRafRef.current)
      deltaRafRef.current = null
    }
    pendingDeltaRef.current = ''
  }, [])

  // Apply any pending tail NOW — used before finishRun() salvages
  // streamingText (stop / abort / safety timeout), so the archived text
  // isn't missing up to a frame's worth of tokens.
  const flushStreamingDelta = useCallback(() => {
    if (deltaRafRef.current !== null) {
      cancelAnimationFrame(deltaRafRef.current)
      deltaRafRef.current = null
    }
    const t = pendingDeltaRef.current
    pendingDeltaRef.current = ''
    if (t) useCosmosStore.getState().appendStreaming(t)
  }, [])

  // Selector + shallow compare — deliberately EXCLUDES audioLevel (60fps audio
  // ticks; orb/waveform read it imperatively inside their own animation loops)
  // AND streamingText (per-token updates; only the StreamingBubble leaf inside
  // ChatLog subscribes to it), so neither re-renders the whole app tree.
  const {
    state, weather, partialTranscript,
    isBackendConnected, connectionStatus, isExecuting, currentActionCommand,
    todos, toolCalls, agentThoughts, confirmRequest, askRequest,
    conversations, activeConversationId, permissionMode, suggestion,
    micAlive, lastDiscarded, lastRunMeta,
    settings, activeLeftTab, activeRightTab, setLeftTab, setRightTab,
    setWeather, addMessage, setPermissionMode, setSuggestion,
    startNewConversation, switchConversation,
    page, setPage,
  } = useCosmosStore(useShallow(s => ({
    state: s.state, weather: s.weather, partialTranscript: s.partialTranscript,
    isBackendConnected: s.isBackendConnected, connectionStatus: s.connectionStatus,
    isExecuting: s.isExecuting,
    currentActionCommand: s.currentActionCommand,
    todos: s.todos, toolCalls: s.toolCalls, agentThoughts: s.agentThoughts,
    confirmRequest: s.confirmRequest, askRequest: s.askRequest,
    conversations: s.conversations, activeConversationId: s.activeConversationId,
    permissionMode: s.permissionMode, suggestion: s.suggestion,
    micAlive: s.micAlive, lastDiscarded: s.lastDiscarded, lastRunMeta: s.lastRunMeta,
    settings: s.settings, activeLeftTab: s.activeLeftTab, activeRightTab: s.activeRightTab,
    setLeftTab: s.setLeftTab, setRightTab: s.setRightTab,
    setWeather: s.setWeather, addMessage: s.addMessage, setPermissionMode: s.setPermissionMode,
    setSuggestion: s.setSuggestion,
    startNewConversation: s.startNewConversation, switchConversation: s.switchConversation,
    page: s.page, setPage: s.setPage,
  })))

  // Derive messages and actionRuns from the active conversation
  const activeConv = conversations.find(c => c.id === activeConversationId) ?? conversations[0]
  const messages   = activeConv?.messages   ?? []
  const actionRuns = activeConv?.actionRuns ?? []

  useEffect(() => { weatherRef.current = weather }, [weather])
  useEffect(() => {
    const t = setInterval(() => setClock(new Date()), 1000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    if (!booted) return
    const city = settings.weatherCity in WEATHER_CITIES ? settings.weatherCity : 'Mumbai'
    const { lat, lon } = WEATHER_CITIES[city]
    fetch(`https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&current_weather=true&hourly=relativehumidity_2m,apparent_temperature&timezone=auto`)
      .then(r=>r.json()).then(data=>{
        const codes:Record<number,string>={0:'Clear',1:'Mainly clear',2:'Partly cloudy',3:'Overcast',
          45:'Foggy',51:'Light drizzle',61:'Light rain',63:'Moderate rain',65:'Heavy rain',80:'Showers',95:'Thunderstorm'}
        const cw=data.current_weather
        setWeather({ temp:Math.round(cw.temperature), feelsLike:Math.round(data.hourly.apparent_temperature?.[0]??cw.temperature),
          humidity:data.hourly.relativehumidity_2m?.[0]??65, description:codes[cw.weathercode]??'Variable',
          location:city, code:cw.weathercode })
      }).catch(()=>{})
  },[booted, settings.weatherCity])

  // Apply display settings to the document root (CSS reads the data attrs).
  useEffect(() => {
    document.documentElement.dataset.uiScale = settings.uiScale
    document.documentElement.dataset.motion = settings.reducedMotion ? 'reduced' : 'full'
  }, [settings.uiScale, settings.reducedMotion])

  // Discarded-transcript hint fades after 8s.
  useEffect(() => {
    if (!lastDiscarded) return
    const t = setTimeout(() => useCosmosStore.getState().setLastDiscarded(null), 8000)
    return () => clearTimeout(t)
  }, [lastDiscarded])

  // Stuck-state watchdog: armed on every outgoing command AND re-armed on every
  // backend message while a task is in flight, so it measures backend SILENCE.
  // On fire it also tells the backend to stop — otherwise the UI resets while
  // the backend keeps running and rejects every next command as busy.
  const armSafetyTimer = useCallback((ms: number) => {
    clearTimeout(safetyTimerRef.current)
    safetyTimerRef.current = setTimeout(() => {
      const st = useCosmosStore.getState()
      if (st.confirmRequest || st.askRequest) return   // waiting on the USER, not the backend
      if (st.state === 'thinking' || st.state === 'executing' || st.isExecuting) {
        sendRef.current?.({ type: 'stop' })
        flushStreamingDelta()    // land any batched tail before the salvage
        st.finishRun(false)      // archive whatever partial run exists
        st.setState('idle')
        st.setExecuting(false)
        playEarcon('error')
        speakRef.current?.('That took too long, sir. Please try again.')
      }
    }, ms)
  }, [flushStreamingDelta])

  // Auto tab-switching: show the panel the action is happening in, but never
  // fight a recent MANUAL selection (15s grace).
  const lastManualTabRef = useRef(0)
  const manualLeftTab = useCallback((t: Parameters<typeof setLeftTab>[0]) => {
    lastManualTabRef.current = Date.now(); setLeftTab(t)
  }, [setLeftTab])
  const manualRightTab = useCallback((t: Parameters<typeof setRightTab>[0]) => {
    lastManualTabRef.current = Date.now(); setRightTab(t)
  }, [setRightTab])
  const autoSwitchOk = () => Date.now() - lastManualTabRef.current > 15_000

  // Quiescence fire: the stream has been quiet ~300ms with the lead unspoken —
  // speak whatever complete sentences exist now (truncateForSpeech bounds it).
  const fireQuiescentLead = useCallback(() => {
    if (spokeLeadRef.current) return
    const { text } = leadOf(streamBufRef.current)
    const sents = text.match(SENTENCE_RE)
    if (!sents) return
    spokeLeadRef.current = true
    leadSpokenCountRef.current = Math.min(sents.length, 2)
    speakRef.current?.(text, { addToChat: false, lead: true })
  }, [])

  // handleWsMessage reads ALL store state via getState() — no closure deps, no timing issues
  const handleWsMessage = useCallback((data: ServerMessage) => {
    const store = useCosmosStore.getState()
    // Backend is alive — clear the silence watchdog (re-armed below if still busy)
    clearTimeout(safetyTimerRef.current)
    switch (data.type) {
      case 'state':
        store.setState(data.state)
        break
      case 'response':
        clearTimeout(quiescenceRef.current)
        discardStreamingDelta()  // batched tail is superseded too
        store.clearStreaming()   // final message supersedes the live buffer
        store.setState('idle')
        store.setExecuting(false)
        // ALWAYS land the message in chat directly — routing it through the
        // voice hook (`speak()` adds to chat) silently lost the answer when
        // the ref wasn't mounted yet: text vanished until a refresh replay.
        store.addMessage({ role: 'cosmos', text: data.text })
        if (!spokeLeadRef.current) {
          speakRef.current?.(data.text, { addToChat: false })
        }
        streamBufRef.current = ''
        spokeLeadRef.current = false
        leadSpokenCountRef.current = 0
        turnBrokeRef.current = false
        break
      case 'response_delta':
        // Restore frame: the backend re-sends already-streamed text after a
        // stream fallback — display it, but never re-speak its lead.
        if ((data as { restore?: boolean }).restore) {
          clearTimeout(quiescenceRef.current)
          queueStreamingDelta(data.text)
          streamBufRef.current += data.text
          spokeLeadRef.current = true
          break
        }
        // First text of a NEW model turn after a tool ran: the previous
        // turn's preamble is obsolete now — replace, don't concatenate.
        if (turnBrokeRef.current) {
          discardStreamingDelta()   // pending tail belongs to the dead turn
          store.clearStreaming()
          streamBufRef.current = ''
          turnBrokeRef.current = false
          // The NEW turn's lead is unspoken — without this reset, a spoken
          // pre-tool preamble left spokeLead=true and the final answer was
          // never voiced (or worse, only its second sentence was, via the
          // leadSpokenCount===1 continuation matching the wrong turn).
          spokeLeadRef.current = false
          leadSpokenCountRef.current = 0
        }
        // Store write is rAF-batched; the speech logic below reads
        // streamBufRef, which sees EVERY delta synchronously.
        queueStreamingDelta(data.text)
        streamBufRef.current += data.text
        // Speak the lead the moment it's worth starting on, in parallel with
        // the rest streaming to screen.
        if (!spokeLeadRef.current) {
          const lead = completeLead(streamBufRef.current)
          if (lead) {
            clearTimeout(quiescenceRef.current)
            spokeLeadRef.current = true
            leadSpokenCountRef.current = Math.min((lead.match(SENTENCE_RE) ?? [lead]).length, 2)
            speakRef.current?.(lead, { addToChat: false, lead: true })
          } else {
            // Lead not ready — if the stream goes quiet (short confirmations
            // like "Done, sir." at stream-tail), speak what's complete.
            clearTimeout(quiescenceRef.current)
            quiescenceRef.current = setTimeout(fireQuiescentLead, 300)
          }
        } else if (leadSpokenCountRef.current === 1) {
          // Sentence 1 fired early; queue sentence 2 behind it as it completes
          // (bounded by the same 2-sentence/180-char budget TTS always had).
          const { text } = leadOf(streamBufRef.current)
          const sents = text.match(SENTENCE_RE)
          if (sents && sents.length >= 2) {
            leadSpokenCountRef.current = 2
            if ((sents[0] + sents[1]).trim().length <= 180) {
              speakRef.current?.(fixPronunciation(cleanForSpeech(sents[1])),
                                 { addToChat: false, verbatim: true, queued: true })
            }
          }
        }
        break
      case 'response_delta_reset':
        clearTimeout(quiescenceRef.current)
        discardStreamingDelta()
        store.clearStreaming()
        streamBufRef.current = ''
        spokeLeadRef.current = false
        leadSpokenCountRef.current = 0
        break
      case 'speak':
        // Interim spoken update — TTS only per protocol, never a chat message
        speakRef.current?.(data.text, { addToChat: false })
        break
      case 'weather':
        store.setWeather(data.payload)
        break
      case 'todos':
        store.setTodos(data.todos)   // FULL replacement, never a diff
        if (autoSwitchOk()) store.setLeftTab('missions')
        break
      case 'tool_start':
        // Keep the streamed preamble ON SCREEN while tools run — clearing it
        // here made the answer visibly vanish mid-run. The next turn's first
        // delta replaces it (turnBrokeRef), and `response` supersedes it.
        // The quiescence timer dies too: a preamble is not the answer's lead.
        clearTimeout(quiescenceRef.current)
        turnBrokeRef.current = true
        store.startToolCall(data.tool_id, data.tool, data.label)
        if (autoSwitchOk()) store.setRightTab('activity')
        break
      case 'tool_done':
        store.finishToolCall(data.tool_id, data.ok, data.detail)  // duration computed FE-side
        break
      case 'agent_thought':
        store.addAgentThought(data.text)
        break
      case 'briefing_card':
        // Scheduled digest: silent card in the chat — never TTS'd (a full
        // briefing read aloud on every open tab is exactly what we removed).
        store.addMessage({ role: 'cosmos', text: data.markdown,
                           kind: 'briefing', title: data.title })
        break
      case 'confirm_request':
        store.setConfirmRequest({ id: data.id, summary: data.summary,
                                  danger: data.danger, detail: data.detail,
                                  steps: data.steps })
        break
      case 'confirm_timeout':
        // Backend auto-declined an unanswered confirm/ask. Match by id — a
        // stale or replayed timeout for banner N must not dismiss banner N+1.
        if (!data.id || store.confirmRequest?.id === data.id) {
          store.setConfirmRequest(null)
        }
        if (data.id && store.askRequest?.id === data.id) {
          store.setAskRequest(null)
        }
        break
      case 'ask_user':
        store.setAskRequest({ id: data.id, question: data.question })
        // Question is displayed in the ConfirmBar — speak it without logging.
        // expectReply: the user can answer by voice without the wake word.
        speakRef.current?.(data.question, { addToChat: false, expectReply: true })
        break
      case 'action_start':
        clearTimeout(quiescenceRef.current)
        discardStreamingDelta()
        store.clearRun()
        streamBufRef.current = ''
        spokeLeadRef.current = false
        leadSpokenCountRef.current = 0
        store.setExecuting(true, data.command ?? '')
        store.setState('executing')
        break
      case 'suggestion':
        store.setSuggestion(data.text)
        break
      case 'run_meta':
        store.setLastRunMeta({ model: data.model, elapsedMs: data.elapsed_ms })
        break
      case 'action_complete': {
        // finishRun salvages any leftover streamingText (aborted runs) —
        // make sure the batched tail is in it first. Happy path: `response`
        // already cleared everything, so this is a no-op.
        flushStreamingDelta()
        const calls = useCosmosStore.getState().toolCalls
        store.finishRun(!calls.some(t => t.status === 'error'))
        store.setState('idle')
        // `response` carries the spoken summary — speak nothing here
        break
      }
    }
    // Re-arm the silence watchdog while a task is still in flight (skip while
    // a confirm/ask banner is up — then we're waiting on the user, not the backend)
    const after = useCosmosStore.getState()
    if (!after.confirmRequest && !after.askRequest &&
        (after.state === 'thinking' || after.state === 'executing' || after.isExecuting)) {
      armSafetyTimer(SAFETY_MS_RUN)
    }
  }, [armSafetyTimer, fireQuiescentLead, queueStreamingDelta,
      discardStreamingDelta, flushStreamingDelta])  // stable — reads live store state at call-time via getState()

  const { send } = useWebSocket(handleWsMessage)
  useEffect(()=>{ sendRef.current=send },[send])
  useEffect(()=>{ isConnRef.current=isBackendConnected },[isBackendConnected])

  // Stop whatever is running. The backend's stop handler is deliberately
  // silent — the single "Stopped, sir." spoken/logged here is the only one.
  const stopExecution = useCallback(() => {
    clearTimeout(safetyTimerRef.current)
    // A late quiescence fire would speak a fragment of the aborted run and
    // cut off the "Stopped, sir." below.
    clearTimeout(quiescenceRef.current)
    spokeLeadRef.current = true
    const store = useCosmosStore.getState()
    // Land any batched delta tail first — finishRun salvages streamingText.
    flushStreamingDelta()
    // Archive the aborted run (todos + tool cards) instead of leaving a stale
    // "current run" with a perpetually-running shimmer card on screen.
    store.finishRun(false)
    store.setState('idle')
    store.setExecuting(false)
    sendRef.current?.({ type: 'stop' })
    speakRef.current?.('Stopped, sir.')
  }, [flushStreamingDelta])

  const handleCommand = useCallback((text: string, fromVoice = false) => {
    const lower = text.toLowerCase().trim()
    const normalized = lower.replace(/[.,!?]+$/, '').trim()

    // Stop command
    if (normalized === 'stop' || normalized === 'cancel' || normalized === 'abort') {
      stopExecution(); return
    }

    // "read the rest / continue" — speak the unspoken tail of the last reply
    // locally; never a backend round-trip.
    if (isReadTheRest(normalized) && speakRemainderRef.current) {
      speakRemainderRef.current()
      return
    }

    // New chat — exact phrase only; substrings ("open a new chat window in
    // slack") must go to the agent. Cancel any in-flight operation first.
    if (NEW_CHAT_PHRASES.includes(normalized)) {
      clearTimeout(safetyTimerRef.current)
      sendRef.current?.({ type: 'stop' })
      discardStreamingDelta()         // a scheduled flush must not leak into the new chat
      startNewConversation()          // resets state to 'idle' in store
      sendRef.current?.({ type: 'new_chat',
                          id: useCosmosStore.getState().activeConversationId })
      speakRef.current?.('Starting a fresh conversation, sir.')
      return
    }

    // Add user message for typed input (voice hook already adds it)
    if (!fromVoice) addMessage({ role: 'user', text })

    // A command while confirm/ask is pending IS the answer — backend resolves it
    // (or, for a non-yes/no reply to a confirm, re-emits the confirm_request)
    const store = useCosmosStore.getState()
    const wasPending = !!(store.confirmRequest || store.askRequest)
    store.setConfirmRequest(null)
    store.setAskRequest(null)

    if (!wasPending) store.setState('thinking')

    // Silence watchdog — longer window when a run is already in flight
    armSafetyTimer(wasPending ? SAFETY_MS_RUN : SAFETY_MS_COMMAND)

    // Check live store state too — isConnRef can be stale during renders
    const connected = isConnRef.current || useCosmosStore.getState().isBackendConnected
    if(connected && sendRef.current){
      const st = useCosmosStore.getState()
      sendRef.current({ type:'command', text, mode: st.permissionMode,
                        conversation_id: st.activeConversationId })
      return
    }
    // Local fallback
    setTimeout(() => {
      if(lower.includes('weather')){
        const w = weatherRef.current
        useCosmosStore.getState().setState('idle')
        speakRef.current?.(w ? `${w.temp}°C, ${w.description}. Humidity ${w.humidity}%, sir.` : "Backend's offline, sir.")
        return
      }
      if(lower.includes('time')){
        useCosmosStore.getState().setState('idle')
        speakRef.current?.(`${new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'})}, sir.`)
        return
      }
      if(lower.includes('sleep')||lower.includes('goodbye')){
        voiceGoSleepRef.current?.()
        speakRef.current?.('Going offline, sir.')
        setTimeout(()=>useCosmosStore.getState().setState('sleeping'),1800)
        return
      }
      useCosmosStore.getState().setState('idle')
      speakRef.current?.("Backend offline, sir. Run start.sh.")
    }, 1200)
  },[addMessage, startNewConversation, stopExecution, armSafetyTimer, discardStreamingDelta])

  // Confirm banner buttons → protocol `confirm` message. Carries the banner's
  // id so a duplicate/stale click can never approve a DIFFERENT pending action.
  const handleConfirm = useCallback((response: 'yes' | 'no') => {
    const cur = useCosmosStore.getState().confirmRequest
    sendRef.current?.({ type: 'confirm', response, id: cur?.id })
    const store = useCosmosStore.getState()
    store.setConfirmRequest(null)
    store.setAskRequest(null)
    // The run resumes — restart the backend-silence watchdog
    armSafetyTimer(SAFETY_MS_RUN)
  }, [armSafetyTimer])

  // Typed answer to ask_user — any command text IS the answer
  const handleAskAnswer = useCallback((text: string) => {
    handleCommand(text)
  }, [handleCommand])

  // Dispatch a queued command once the current run fully settles.
  const queuedCommand = useCosmosStore(s => s.queuedCommand)
  useEffect(() => {
    if (!queuedCommand) return
    const st = useCosmosStore.getState()
    if (st.state === 'idle' && !st.isExecuting && !st.confirmRequest && !st.askRequest) {
      st.setQueuedCommand(null)
      handleCommand(queuedCommand)
    }
  }, [queuedCommand, state, isExecuting, confirmRequest, askRequest])  // eslint-disable-line react-hooks/exhaustive-deps

  // Permission-mode toggle → update store + tell the backend immediately
  const toggleMode = useCallback(() => {
    const next = useCosmosStore.getState().permissionMode === 'full' ? 'ask' : 'full'
    setPermissionMode(next)
    sendRef.current?.({ type: 'set_mode', mode: next })
  }, [setPermissionMode])

  const voice = useVoice((text) => handleCommand(text, true),
                         useCallback(() => sendRef.current?.({ type: 'prefetch' }), []))
  useEffect(()=>{ speakRef.current=voice.speak },[voice.speak])
  useEffect(()=>{ voiceGoSleepRef.current=voice.goSleep },[voice.goSleep])
  useEffect(()=>{ speakRemainderRef.current=voice.speakRemainder },[voice.speakRemainder])

  // Hold-Space push-to-talk (ignored while typing in any input/textarea).
  useEffect(() => {
    const isTyping = (t: EventTarget | null) => {
      const el = t as HTMLElement | null
      return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)
    }
    const down = (e: KeyboardEvent) => {
      if (e.code !== 'Space' || e.repeat || isTyping(e.target)) return
      e.preventDefault()
      voice.setPushToTalk(true)
    }
    const up = (e: KeyboardEvent) => {
      if (e.code !== 'Space' || isTyping(e.target)) return
      voice.setPushToTalk(false)
    }
    window.addEventListener('keydown', down)
    window.addEventListener('keyup', up)
    return () => { window.removeEventListener('keydown', down); window.removeEventListener('keyup', up) }
  }, [voice.setPushToTalk])

  // Global keyboard layer: ⌘K palette, ⌘N new chat, Esc stops a running task.
  // (ConfirmBar owns Y/N/Esc while an authorization is pending.)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const st = useCosmosStore.getState()
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        if (!st.confirmRequest) setPaletteOpen(o => !o)
        return
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'n') {
        e.preventDefault()
        clearTimeout(safetyTimerRef.current)
        sendRef.current?.({ type: 'stop' })
        discardStreamingDelta()
        startNewConversation()
        sendRef.current?.({ type: 'new_chat',
                            id: useCosmosStore.getState().activeConversationId })
        return
      }
      if (e.key === 'Escape' && !paletteOpen && !st.confirmRequest && !st.askRequest &&
          (st.isExecuting || st.state === 'thinking' || st.state === 'executing')) {
        e.preventDefault()
        stopExecution()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [paletteOpen, startNewConversation, stopExecution, discardStreamingDelta])

  const stateColor = STATE_COLOR[state] ?? STATE_COLOR.idle
  const stateLabel = STATE_LABEL[state] ?? STATE_LABEL.idle

  // ── Navigation: home is the landing; 'agent' also fires the start gesture
  // (needed for mic/audio autoplay). WS + voice hooks stay mounted across
  // pages, so switching to Skills/MCPs and back never re-boots the agent.
  const navigate = (p: typeof page) => {
    if (p === 'agent') setStarted(true)
    setPage(p)
  }

  // ── Agent view (init gesture → boot sequence → live HUD) ──
  const agentView = (
    <>
      {started && !booted && (
        // Mic init runs immediately: the user gesture for getUserMedia already
        // happened at 'start' — the old 800ms delay guarded nothing.
        <BootSequence onComplete={()=>{ setBooted(true); voice.initialize() }}/>
      )}
      <AnimatePresence>
        {booted && (
          <motion.div initial={{opacity:0}} animate={{opacity:1}} transition={{duration:0.8}}
            style={{ position:'fixed',inset:0,background:'var(--bg)',
              // Chat row (5) gets the larger share so the transcript is readable;
              // the orb row (3) shrinks — its side panels scroll internally.
              display:'grid', gridTemplateRows:'56px 1px 1fr 1px 1.3fr',
              overflow:'hidden' }}>
            <SpaceBackdrop/>
            <div className="bg-layer" style={{zIndex:0}}/>
            <div className="hex-grid" style={{zIndex:0}}/>
            <div className="scanlines" style={{zIndex:2}}/>
            <div className="scan-sweep" style={{zIndex:1}}/>

            {/* TOP BAR (paddingLeft clears the fixed hamburger button) */}
            <div className="top-bar" style={{zIndex:10,gridRow:1,paddingLeft:64}}>
              <motion.div initial={{opacity:0,x:-20}} animate={{opacity:1,x:0}} transition={{delay:0.2}}>
                <div className="top-bar-logo">COSMOS
                  <span className="top-bar-sub">STARK INDUSTRIES AI INTERFACE</span>
                </div>
              </motion.div>
              <motion.div className="top-bar-center" initial={{opacity:0,y:-10}} animate={{opacity:1,y:0}} transition={{delay:0.3}}>
                <div className="top-bar-status-row">
                  {([
                    // Honest three-state VOICE chip: no API / settings-off /
                    // recognizer silently dead / healthy.
                    {label: !voice.hasSpeechAPI ? 'NO SPEECH API'
                          : !settings.voiceEnabled ? 'VOICE OFF'
                          : !micAlive ? 'MIC STALE' : 'VOICE',
                     cls:   !voice.hasSpeechAPI ? 'warning'
                          : !settings.voiceEnabled ? ''
                          : !micAlive ? 'reconnecting' : 'online',
                     key:   'VOICE'},
                    // BACKEND is driven by live WS link state: green ONLINE when the
                    // socket is open, pulsing amber RECONNECTING while backing off
                    {label: connectionStatus==='online' ? 'BACKEND' : 'RECONNECTING',
                     cls:   connectionStatus==='online' ? 'online'  : 'reconnecting',
                     key:   'BACKEND'},
                  ] as {label:string; cls:string; key?:string}[]).map(({label,cls,key})=>(
                    <div key={key ?? label} className={`status-chip ${cls}`}>
                      <span className="status-dot"/>{label}
                    </div>
                  ))}
                  {lastRunMeta && (
                    <div className="status-chip" title="Model that answered the last run"
                      style={{opacity:0.75}}>
                      {lastRunMeta.model.replace(/-\d{8}$/,'')} · {(lastRunMeta.elapsedMs/1000).toFixed(1)}s
                    </div>
                  )}
                </div>
              </motion.div>
              <motion.div className="top-bar-right" initial={{opacity:0,x:20}} animate={{opacity:1,x:0}} transition={{delay:0.2}}>
                <button
                  onClick={toggleMode}
                  title={permissionMode==='full'
                    ? 'FULL ACCESS — outward actions run without asking (deletes still confirm). Click to guard.'
                    : 'GUARDED — every outward action asks first. Click for full access.'}
                  style={{
                    fontFamily:'var(--font-d)', fontSize:8, fontWeight:700, letterSpacing:'0.14em',
                    cursor:'pointer', borderRadius:2, padding:'4px 10px', whiteSpace:'nowrap',
                    display:'flex', alignItems:'center', gap:5,
                    color: permissionMode==='full' ? 'var(--amber)' : 'var(--cyan)',
                    background: permissionMode==='full' ? 'rgba(255,149,0,0.1)' : 'rgba(0,212,255,0.08)',
                    border:`1px solid ${permissionMode==='full' ? 'rgba(255,149,0,0.45)' : 'rgba(0,212,255,0.3)'}`,
                    boxShadow: permissionMode==='full' ? '0 0 10px rgba(255,149,0,0.25)' : 'none',
                    transition:'all 0.2s',
                  }}>
                  <span style={{ fontSize:9 }}>{permissionMode==='full' ? '⚡' : '🛡'}</span>
                  {permissionMode==='full' ? 'FULL ACCESS' : 'GUARDED'}
                </button>
                <div className="top-bar-date">
                  {clock.toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric'}).toUpperCase()}
                </div>
              </motion.div>
            </div>

            <div className="hud-divider" style={{gridRow:2,zIndex:10}}/>

            {/* PROACTIVE SUGGESTION CHIP */}
            <AnimatePresence>
              {suggestion && (
                <motion.div initial={{opacity:0,y:-8}} animate={{opacity:1,y:0}} exit={{opacity:0,y:-8}}
                  style={{position:'absolute',top:64,left:'50%',transform:'translateX(-50%)',
                    zIndex:40,display:'flex',alignItems:'center',gap:8,
                    background:'rgba(0,20,30,0.92)',border:'1px solid rgba(0,212,255,0.35)',
                    borderRadius:4,padding:'7px 12px',boxShadow:'0 0 18px rgba(0,212,255,0.15)'}}>
                  <span style={{fontFamily:'var(--font-d)',fontSize:9,letterSpacing:'0.14em',
                    color:'rgba(0,212,255,0.6)'}}>SUGGESTION</span>
                  <button
                    onClick={()=>{ const s=suggestion; setSuggestion(null); handleCommand(s) }}
                    style={{fontFamily:'var(--font-m)',fontSize:12,color:'var(--cyan)',
                      background:'none',border:'none',cursor:'pointer',padding:0}}>
                    {suggestion}
                  </button>
                  <button aria-label="Dismiss suggestion" onClick={()=>setSuggestion(null)}
                    style={{fontFamily:'var(--font-m)',fontSize:12,color:'rgba(0,212,255,0.5)',
                      background:'none',border:'none',cursor:'pointer',padding:'0 2px'}}>
                    ✕
                  </button>
                </motion.div>
              )}
            </AnimatePresence>

            {/* MAIN ROW */}
            <div className="main-row" style={{ gridRow:3, minHeight:0, zIndex:5 }}>

              {/* LEFT — Missions / Memory / Settings tabs */}
              <motion.div initial={{opacity:0,x:-24}} animate={{opacity:1,x:0}} transition={{delay:0.3}}
                style={{display:'flex',flexDirection:'column',minHeight:0,overflow:'hidden'}}>
                <div style={{flex:1,minHeight:0}}>
                  <HudTabs ariaLabel="left-panel" active={activeLeftTab}
                    onChange={(id)=>manualLeftTab(id as any)}
                    tabs={[
                      { id:'missions', label:'Missions',
                        badge: todos.filter(t=>t.status!=='completed').length || undefined,
                        content: <TaskBoard todos={todos}/> },
                      { id:'memory',   label:'Memory',   content: <MemoryPanel/> },
                      { id:'settings', label:'Settings', content: <SettingsPanel/> },
                    ]}/>
                </div>
              </motion.div>

              {/* CENTER — orb + waveform + partial transcript */}
              <motion.div initial={{opacity:0,scale:0.88}} animate={{opacity:1,scale:1}} transition={{delay:0.15,duration:0.8}}
                style={{display:'flex',flexDirection:'column',alignItems:'center',justifyContent:'center',gap:8,position:'relative',minHeight:0,overflow:'hidden'}}>
                <button
                  aria-label={state==='sleeping'||state==='idle' ? 'Wake Cosmos' : `Cosmos is ${state}`}
                  onClick={state==='sleeping'||state==='idle'?voice.manualWake:undefined}
                  style={{width:'min(320px,100%)',height:'min(320px,60vmin)',position:'relative',
                    cursor:state==='sleeping'||state==='idle'?'pointer':'default',
                    background:'none',border:'none',padding:0,
                    filter:`drop-shadow(0 0 40px ${stateColor}22)`}}>
                  <CosmosOrb state={state}/>
                  <div style={{position:'absolute',bottom:24,left:'50%',transform:'translateX(-50%)',
                    display:'flex',alignItems:'center',gap:6,
                    fontFamily:'var(--font-d)',fontSize:'var(--fs-cap)',letterSpacing:'0.28em',
                    color:stateColor, whiteSpace:'nowrap',
                    textShadow:state==='listening'?`0 0 14px ${stateColor}`:undefined}}>
                    {stateLabel}
                  </div>
                </button>
                {/* Screen-reader announcement of state transitions */}
                <div className="visually-hidden" role="status" aria-live="polite">
                  Cosmos is {state}
                </div>
                <VoiceWaveform state={state}/>

                {partialTranscript && state==='listening' && (
                  <motion.div initial={{opacity:0}} animate={{opacity:1}}
                    style={{fontFamily:'var(--font-m)',fontSize:10,color:'rgba(0,212,255,0.55)',
                      fontStyle:'italic',maxWidth:280,textAlign:'center',
                      overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                    "{partialTranscript}"
                  </motion.div>
                )}

                {/* Discarded-transcript hint — speech heard but no wake word */}
                {lastDiscarded && !partialTranscript && (
                  <motion.div initial={{opacity:0}} animate={{opacity:1}} aria-live="polite"
                    style={{fontFamily:'var(--font-m)',fontSize:'var(--fs-cap)',
                      color:'rgba(255,149,0,0.7)',maxWidth:300,textAlign:'center',
                      overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                    heard: "{lastDiscarded}" — start with "cosmos" (or hold Space)
                  </motion.div>
                )}
              </motion.div>

              {/* RIGHT — Activity / Flight Recorder / Audit tabs */}
              <motion.div initial={{opacity:0,x:24}} animate={{opacity:1,x:0}} transition={{delay:0.3}}
                style={{minHeight:0, height:'100%', overflow:'hidden'}}>
                <HudTabs ariaLabel="right-panel" active={activeRightTab}
                  onChange={(id)=>manualRightTab(id as any)}
                  tabs={[
                    { id:'activity', label:'Activity',
                      badge: isExecuting ? '●' : undefined,
                      content: <AgentActivity toolCalls={toolCalls} thoughts={agentThoughts}
                        isExecuting={isExecuting} currentCommand={currentActionCommand}/> },
                    { id:'recorder', label:'Recorder',
                      badge: actionRuns.length || undefined,
                      content: <FlightRecorder history={actionRuns}/> },
                    { id:'audit',    label:'Audit', content: <AuditPanel/> },
                  ]}/>
              </motion.div>
            </div>

            <div className="hud-divider" style={{gridRow:4,zIndex:10,margin:'0 16px'}}/>

            {/* BOTTOM ROW */}
            <motion.div initial={{opacity:0,y:16}} animate={{opacity:1,y:0}} transition={{delay:0.5}}
              style={{gridRow:5,padding:'12px 16px',minHeight:0,zIndex:5}}>
              <ChatLog
                messages={messages}
                partialTranscript={partialTranscript}
                currentToolLabel={[...toolCalls].reverse().find(t => t.status === 'running')?.label}
                state={state}
                onCommand={handleCommand}
                onStop={stopExecution}
                conversations={conversations}
                activeConversationId={activeConversationId}
                onNewChat={() => {
                  clearTimeout(safetyTimerRef.current)
                  sendRef.current?.({ type: 'stop' })
                  discardStreamingDelta()
                  startNewConversation()
                  sendRef.current?.({ type: 'new_chat',
                                      id: useCosmosStore.getState().activeConversationId })
                }}
                onSwitchConversation={(id) => {
                  clearTimeout(safetyTimerRef.current)
                  sendRef.current?.({ type: 'stop' })
                  discardStreamingDelta()
                  switchConversation(id)
                  sendRef.current?.({ type: 'switch_conversation', id })
                }}
                onDeleteConversation={(id) => {
                  useCosmosStore.getState().deleteConversation(id)
                  sendRef.current?.({ type: 'delete_conversation', id })
                }}
              />
            </motion.div>

            {/* CONFIRM / ASK OVERLAY */}
            <ConfirmBar
              confirmRequest={confirmRequest}
              askRequest={askRequest}
              onConfirm={handleConfirm}
              onAnswer={handleAskAnswer}
            />

            {/* ⌘K COMMAND PALETTE */}
            <CommandPalette
              open={paletteOpen}
              onClose={() => setPaletteOpen(false)}
              onCommand={handleCommand}
              onStop={stopExecution}
              onToggleMode={toggleMode}
              onNewChat={() => {
                clearTimeout(safetyTimerRef.current)
                sendRef.current?.({ type: 'stop' })
                discardStreamingDelta()
                startNewConversation()
                sendRef.current?.({ type: 'new_chat',
                                    id: useCosmosStore.getState().activeConversationId })
              }}
              onSwitchConversation={(id) => {
                clearTimeout(safetyTimerRef.current)
                sendRef.current?.({ type: 'stop' })
                discardStreamingDelta()
                switchConversation(id)
                sendRef.current?.({ type: 'switch_conversation', id })
              }}
            />
          </motion.div>
        )}
      </AnimatePresence>
    </>
  )

  const fade = { initial: { opacity: 0 }, animate: { opacity: 1 }, transition: { duration: 0.35 } }
  return (
    <>
      {/* Neon cursor on the calmer pages only. The agent HUD is dense WebGL
          (orb + Bloom) where a blend-mode overlay caused compositing/layout
          glitches — it keeps the native cursor. */}
      {page !== 'agent' && <CustomCursor />}
      {/* Home & the immersive pages carry their own top nav; only the agent HUD
          uses the hamburger drawer. */}
      {page === 'agent'  && <NavMenu page={page} onNavigate={navigate} />}
      {page === 'home'   && <motion.div key="home" {...fade}><HomePage onNavigate={navigate} /></motion.div>}
      {page === 'nexus'  && <motion.div key="nexus" {...fade}><NexusPage page={page} onNavigate={navigate} /></motion.div>}
      {page === 'dossier'&& <motion.div key="dossier" {...fade}><DossierPage page={page} onNavigate={navigate} /></motion.div>}
      {page === 'vision' && <motion.div key="vision" {...fade}><VisionPage page={page} onNavigate={navigate} /></motion.div>}
      {page === 'kinesis'&& <motion.div key="kinesis" {...fade}><KinesisPage page={page} onNavigate={navigate} /></motion.div>}
      {page === 'skills' && <motion.div key="skills" {...fade}><SkillsPage page={page} onNavigate={navigate} /></motion.div>}
      {page === 'mcps'   && <motion.div key="mcps" {...fade}><ConnectorsPage page={page} onNavigate={navigate} /></motion.div>}
      {page === 'slack'  && <motion.div key="slack" {...fade}><SlackBridgePage page={page} onNavigate={navigate} /></motion.div>}
      {page === 'panel'  && <motion.div key="panel" {...fade}><PanelPage page={page} onNavigate={navigate} /></motion.div>}
      {page === 'mutate' && <motion.div key="mutate" {...fade}><MutatePage page={page} onNavigate={navigate} /></motion.div>}
      {page === 'agent'  && agentView}
    </>
  )
}
