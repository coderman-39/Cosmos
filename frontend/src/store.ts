import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { PersistStorage, StorageValue } from 'zustand/middleware'
import type { Todo } from './types/protocol'

// ─── Types ────────────────────────────────────────────────────────────────────

export type CosmosState =
  | 'sleeping' | 'waking' | 'idle' | 'listening'
  | 'thinking' | 'speaking' | 'executing'

// WS link health: 'online' = socket open, 'reconnecting' = closed/backing off
export type ConnectionStatus = 'online' | 'reconnecting'
export type PermissionMode = 'ask' | 'full'
// Top-level page. Not persisted — Cosmos always lands on the home page.
export type Page = 'home' | 'agent' | 'nexus' | 'dossier' | 'vision' | 'kinesis' | 'skills' | 'mcps' | 'slack' | 'panel' | 'mutate'

export interface Message {
  id: string; role: 'user' | 'cosmos'; text: string; timestamp: string
  /** 'briefing': scheduled digest card — title banner, never spoken */
  kind?: 'briefing'
  title?: string
}

export interface WeatherData {
  temp: number; feelsLike: number; humidity: number
  description: string; location: string; code: number
}

export type ToolCallStatus = 'running' | 'done' | 'error'

export interface ToolCall {
  id: string; tool: string; label: string; detail?: string
  status: ToolCallStatus; ts: string; durationMs?: number
}

export interface AgentThought {
  id: string; text: string; ts: string
}

export interface PlanStep       { summary: string; danger: string }
export interface ConfirmRequest { id: string; summary: string; danger: string; detail?: string; steps?: PlanStep[] }
export interface AskRequest     { id: string; question: string }

export interface ActionRun {
  id: string; command: string
  toolCalls: ToolCall[]; todos: Todo[]
  startedAt: string; ok: boolean
}

export interface Conversation {
  id: string; title: string; messages: Message[]
  startedAt: string; actionRuns: ActionRun[]
  pinned?: boolean
}

// ─── Settings (persisted; edited in the Settings tab) ────────────────────────

export interface Settings {
  voiceEnabled: boolean          // mic wake-word listening
  ttsEnabled: boolean            // spoken replies
  reducedMotion: boolean         // disable scanlines/sweeps/shimmers
  uiScale: 'normal' | 'large'    // type scale
  voiceRate: number              // TTS playback rate (0.8–1.4)
  weatherCity: string            // city key from WEATHER_CITIES
}

export const WEATHER_CITIES: Record<string, { lat: number; lon: number }> = {
  Mumbai:    { lat: 19.076,  lon: 72.877 },
  Bangalore: { lat: 12.972,  lon: 77.594 },
  Delhi:     { lat: 28.644,  lon: 77.216 },
  Hyderabad: { lat: 17.385,  lon: 78.487 },
  Chennai:   { lat: 13.083,  lon: 80.270 },
  Pune:      { lat: 18.520,  lon: 73.857 },
}

export const DEFAULT_SETTINGS: Settings = {
  voiceEnabled: true, ttsEnabled: true, reducedMotion: false,
  uiScale: 'normal', voiceRate: 1.0, weatherCity: 'Mumbai',
}

export type LeftTab = 'missions' | 'memory' | 'settings'
export type RightTab = 'activity' | 'recorder' | 'audit'

// ─── Store ────────────────────────────────────────────────────────────────────

interface CosmosStore {
  // HUD
  state: CosmosState
  weather: WeatherData | null
  page: Page                       // current top-level page (home / agent / skills / mcps)
  audioLevel: number
  isBackendConnected: boolean
  connectionStatus: ConnectionStatus
  partialTranscript: string
  permissionMode: PermissionMode   // 'ask' = guarded, 'full' = only deletions confirm
  earconsEnabled: boolean          // audible chirps on wake/accept/error/sleep

  // Agent run (current, cleared on new run)
  todos: Todo[]
  toolCalls: ToolCall[]
  agentThoughts: AgentThought[]
  streamingText: string        // live text of the turn being generated (transient)
  confirmRequest: ConfirmRequest | null
  askRequest: AskRequest | null
  isExecuting: boolean
  currentActionCommand: string
  suggestion: string | null    // proactive "you usually do this now" chip
  queuedCommand: string | null // typed while a run was in flight — sent when it ends
  micAlive: boolean            // Web Speech recognizer produced events recently
  lastDiscarded: string | null // final transcript dropped for lacking the wake word
  lastRunMeta: { model: string; elapsedMs: number } | null

  // Conversations (persisted)
  conversations: Conversation[]
  activeConversationId: string

  // Settings + tab selection (persisted)
  settings: Settings
  activeLeftTab: LeftTab
  activeRightTab: RightTab

  // Setters
  setPage: (p: Page) => void
  setState: (s: CosmosState) => void
  setWeather: (w: WeatherData) => void
  setAudioLevel: (l: number) => void
  setBackendConnected: (c: boolean) => void
  setConnectionStatus: (c: ConnectionStatus) => void
  setPartialTranscript: (t: string) => void
  setPermissionMode: (m: PermissionMode) => void
  setEarconsEnabled: (v: boolean) => void

  addMessage: (m: Omit<Message, 'id' | 'timestamp'>) => void

  setTodos:        (todos: Todo[]) => void
  startToolCall:   (id: string, tool: string, label: string) => void
  finishToolCall:  (id: string, ok: boolean, detail?: string) => void
  addAgentThought: (text: string) => void
  appendStreaming: (text: string) => void
  clearStreaming:  () => void
  setConfirmRequest: (r: ConfirmRequest | null) => void
  setAskRequest:     (r: AskRequest | null) => void
  setSuggestion:     (s: string | null) => void
  setQueuedCommand:  (c: string | null) => void
  setMicAlive:       (v: boolean) => void
  setLastDiscarded:  (t: string | null) => void
  setLastRunMeta:    (m: { model: string; elapsedMs: number } | null) => void
  clearRun:    () => void
  setExecuting: (v: boolean, command?: string) => void
  finishRun:   (ok: boolean) => void

  startNewConversation: () => void
  switchConversation:   (id: string) => void

  updateSettings: (patch: Partial<Settings>) => void
  setLeftTab:  (t: LeftTab) => void
  setRightTab: (t: RightTab) => void

  renameConversation: (id: string, title: string) => void
  deleteConversation: (id: string) => void
  togglePinConversation: (id: string) => void
}

const uid = () => `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`

function newConv(): Conversation {
  return { id: uid(), title: 'New conversation', messages: [], startedAt: new Date().toISOString(), actionRuns: [] }
}

const INITIAL_CONV = newConv()

// ─── One-time migration from the pre-v3 persist key ──────────────────────────
// The persist key changed 'friday-v2' → 'friday-v3'; without this, all prior
// conversation history silently vanishes and the v2 blob rots in localStorage.
function migrateLegacyStorage() {
  try {
    if (typeof localStorage === 'undefined') return
    const old = localStorage.getItem('friday-v2')
    if (!old) return
    if (!localStorage.getItem('friday-v3')) {
      const parsed = JSON.parse(old)
      const s = parsed?.state ?? parsed
      const convs: Conversation[] = (Array.isArray(s?.conversations) ? s.conversations : [])
        .map((c: any): Conversation => ({
          id: String(c?.id ?? uid()),
          title: String(c?.title ?? 'Conversation'),
          startedAt: String(c?.startedAt ?? new Date().toISOString()),
          messages: Array.isArray(c?.messages) ? c.messages : [],
          // v2 ActionRun had a different shape — normalize defensively
          actionRuns: (Array.isArray(c?.actionRuns) ? c.actionRuns : []).map((r: any): ActionRun => ({
            id: String(r?.id ?? uid()),
            command: String(r?.command ?? ''),
            toolCalls: Array.isArray(r?.toolCalls) ? r.toolCalls : [],
            todos: Array.isArray(r?.todos) ? r.todos : [],
            startedAt: String(r?.startedAt ?? ''),
            ok: !!r?.ok,
          })),
        }))
      if (convs.length) {
        const activeId = convs.some((c) => c.id === s?.activeConversationId)
          ? s.activeConversationId : convs[0].id
        localStorage.setItem('friday-v3', JSON.stringify({
          state: { conversations: convs, activeConversationId: activeId },
          version: 1,
        }))
      }
    }
    localStorage.removeItem('friday-v2')
  } catch { /* corrupt legacy blob — start fresh */ }
}
migrateLegacyStorage()

// ─── Debounced persist storage ───────────────────────────────────────────────
// zustand's persist middleware calls storage.setItem on EVERY set() — even when
// the changed key (streamingText per token, audioLevel at ~60fps,
// partialTranscript per interim) is excluded by partialize. With the default
// JSON storage that meant a full JSON.stringify of the conversations blob plus
// a synchronous localStorage write per set — 60–480ms/s of main-thread work
// during streaming, on the thread that schedules TTS and handles WS frames.
// This adapter defers BOTH the stringify and the write: writes coalesce into
// one trailing flush ≤500ms later (bounded staleness — the timer is NOT reset
// per call, so a 60fps set storm can't starve persistence forever), and a
// synchronous flush on pagehide/beforeunload/visibility-hidden guarantees
// conversation history survives reloads and tab closes.

type PersistedSlice = Pick<CosmosStore,
  'conversations' | 'activeConversationId' | 'permissionMode' | 'earconsEnabled' |
  'settings' | 'activeLeftTab' | 'activeRightTab'>

const PERSIST_FLUSH_MS = 500
let pendingPersist: { name: string; value: StorageValue<PersistedSlice> } | null = null
let persistTimer: ReturnType<typeof setTimeout> | null = null

function flushPersist() {
  if (persistTimer !== null) { clearTimeout(persistTimer); persistTimer = null }
  if (!pendingPersist) return
  const { name, value } = pendingPersist
  pendingPersist = null
  try {
    localStorage.setItem(name, JSON.stringify(value))
  } catch { /* quota exceeded / private mode — drop this snapshot */ }
}

const debouncedStorage: PersistStorage<PersistedSlice> = {
  getItem: (name) => {
    // Serve the pending snapshot first — it's newer than what's on disk.
    if (pendingPersist?.name === name) return pendingPersist.value
    try {
      const str = localStorage.getItem(name)
      return str ? (JSON.parse(str) as StorageValue<PersistedSlice>) : null
    } catch { return null }
  },
  setItem: (name, value) => {
    // partialize only builds a shallow object of references — cheap. The
    // expensive stringify happens once per flush, on the LATEST snapshot.
    pendingPersist = { name, value }
    if (persistTimer === null) persistTimer = setTimeout(flushPersist, PERSIST_FLUSH_MS)
  },
  removeItem: (name) => {
    if (pendingPersist?.name === name) pendingPersist = null
    try { localStorage.removeItem(name) } catch { /* ignore */ }
  },
}

if (typeof window !== 'undefined') {
  // pagehide fires reliably on tab close/navigation (incl. Safari where
  // beforeunload is flaky); beforeunload kept as a belt-and-braces fallback.
  window.addEventListener('pagehide', flushPersist)
  window.addEventListener('beforeunload', flushPersist)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushPersist()
  })
}

export const useCosmosStore = create<CosmosStore>()(
  persist(
    (set, get) => ({
      // ── HUD ────────────────────────────────────────────────────────────────
      page: 'home',
      state: 'sleeping', weather: null,
      audioLevel: 0, isBackendConnected: false,
      connectionStatus: 'reconnecting', partialTranscript: '', permissionMode: 'ask',
      earconsEnabled: true,

      // ── Agent run ────────────────────────────────────────────────────────────
      todos: [], toolCalls: [], agentThoughts: [], streamingText: '',
      confirmRequest: null, askRequest: null,
      isExecuting: false, currentActionCommand: '',
      suggestion: null,
      queuedCommand: null,
      micAlive: true, lastDiscarded: null, lastRunMeta: null,

      // ── Conversations ────────────────────────────────────────────────────────
      conversations:         [INITIAL_CONV],
      activeConversationId:  INITIAL_CONV.id,

      // ── Settings + tabs ─────────────────────────────────────────────────────
      settings:       { ...DEFAULT_SETTINGS },
      activeLeftTab:  'missions',
      activeRightTab: 'activity',

      // ── Setters ──────────────────────────────────────────────────────────────
      setPage:            (page) => set({ page }),
      setState:           (state) => set({ state }),
      setWeather:         (weather) => set({ weather }),
      setAudioLevel:      (audioLevel) => set({ audioLevel }),
      setBackendConnected:(isBackendConnected) => set({ isBackendConnected }),
      setConnectionStatus:(connectionStatus) => set({ connectionStatus }),
      setPartialTranscript:(partialTranscript) => set({ partialTranscript }),
      setPermissionMode:(permissionMode) => set({ permissionMode }),
      setEarconsEnabled:(earconsEnabled) => set({ earconsEnabled }),

      addMessage: (m) => set((s) => {
        const msg: Message = { ...m, id: uid(), timestamp: new Date().toISOString() }
        const activeId = s.activeConversationId
        return {
          conversations: s.conversations.map(c => {
            if (c.id !== activeId) return c
            const updated: Conversation = { ...c, messages: [...c.messages, msg] }
            if (m.role === 'user' && c.title === 'New conversation') {
              updated.title = m.text.slice(0, 50)
            }
            return updated
          }),
        }
      }),

      // FULL replacement — protocol v3 `todos` is never a diff
      setTodos: (todos) => set({ todos }),

      startToolCall: (id, tool, label) => set(s => ({
        toolCalls: [...s.toolCalls, { id, tool, label, status: 'running' as ToolCallStatus, ts: new Date().toISOString() }],
      })),

      // Duration computed FE-side: now − tool_start timestamp
      finishToolCall: (id, ok, detail) => set(s => ({
        toolCalls: s.toolCalls.map(t => t.id === id
          ? { ...t, status: (ok ? 'done' : 'error') as ToolCallStatus, detail,
              durationMs: Math.max(0, Date.now() - Date.parse(t.ts)) }
          : t),
      })),

      appendStreaming: (text) => set(s => ({ streamingText: s.streamingText + text })),
      clearStreaming:  () => set(s => (s.streamingText ? { streamingText: '' } : s)),

      addAgentThought: (text) => set(s => ({
        agentThoughts: [...s.agentThoughts, { id: uid(), text, ts: new Date().toISOString() }].slice(-6),
      })),

      setConfirmRequest: (confirmRequest) => set({ confirmRequest }),
      setAskRequest:     (askRequest) => set({ askRequest }),
      setSuggestion:     (suggestion) => set({ suggestion }),
      setQueuedCommand:  (queuedCommand) => set({ queuedCommand }),
      setMicAlive:       (micAlive) => set(s => s.micAlive === micAlive ? s : { micAlive }),
      setLastDiscarded:  (lastDiscarded) => set({ lastDiscarded }),
      setLastRunMeta:    (lastRunMeta) => set({ lastRunMeta }),

      clearRun: () => set({
        todos: [], toolCalls: [], agentThoughts: [], streamingText: '',
        confirmRequest: null, askRequest: null,
      }),

      setExecuting: (isExecuting, command) =>
        set(s => ({ isExecuting, currentActionCommand: command ?? s.currentActionCommand })),

      finishRun: (ok) => set((s) => {
        // An aborted/stopped run can leave streamed answer text on screen
        // with no `response` ever coming — promote it to a chat message
        // instead of silently destroying what the user was reading. (In the
        // happy path `response` clears streamingText BEFORE finishRun runs,
        // so this never double-adds.)
        const salvaged = s.streamingText.trim()
        const convs = salvaged
          ? s.conversations.map(c => c.id !== s.activeConversationId ? c : {
              ...c,
              messages: [...c.messages, {
                id: uid(), role: 'cosmos' as const,
                text: salvaged + ' …(interrupted)',
                timestamp: new Date().toISOString(),
              }],
            })
          : s.conversations
        if (!s.toolCalls.length && !s.todos.length) {
          return { conversations: convs, isExecuting: false,
                   confirmRequest: null, askRequest: null, streamingText: '' }
        }
        // A stop/abort can leave tools mid-flight — settle them as errors so
        // no card is archived as a perpetually 'running' shimmer.
        const settled = s.toolCalls.map(t => t.status === 'running'
          ? { ...t, status: 'error' as ToolCallStatus, detail: t.detail ?? 'Aborted',
              durationMs: Math.max(0, Date.now() - Date.parse(t.ts)) }
          : t)
        const run: ActionRun = {
          id: uid(), command: s.currentActionCommand,
          toolCalls: settled, todos: [...s.todos],
          startedAt: new Date().toISOString(), ok,
        }
        const activeId = s.activeConversationId
        return {
          conversations: convs.map(c => {
            if (c.id !== activeId) return c
            return { ...c, actionRuns: [run, ...c.actionRuns].slice(0, 50) }
          }),
          todos: [], toolCalls: [], agentThoughts: [], streamingText: '',
          confirmRequest: null, askRequest: null, isExecuting: false,
        }
      }),

      startNewConversation: () => {
        const conv = newConv()
        set(s => {
          // Pinned conversations survive the 10-cap; unpinned age out.
          const all = [conv, ...s.conversations]
          const pinnedCount = all.filter(c => c.pinned).length
          let unpinnedBudget = Math.max(1, 10 - pinnedCount)
          const kept = all.filter(c => {
            if (c.pinned) return true
            if (unpinnedBudget > 0) { unpinnedBudget--; return true }
            return false
          })
          return {
          conversations:        kept,
          activeConversationId: conv.id,
          todos: [], toolCalls: [], agentThoughts: [], streamingText: '',
          confirmRequest: null, askRequest: null,
          isExecuting:          false,
          state:                'idle' as CosmosState,
          currentActionCommand: '',
          }
        })
      },

      updateSettings: (patch) => set(s => ({ settings: { ...s.settings, ...patch } })),
      setLeftTab:  (activeLeftTab) => set({ activeLeftTab }),
      setRightTab: (activeRightTab) => set({ activeRightTab }),

      renameConversation: (id, title) => set(s => ({
        conversations: s.conversations.map(c =>
          c.id === id ? { ...c, title: title.slice(0, 60) || c.title } : c),
      })),

      deleteConversation: (id) => set(s => {
        const remaining = s.conversations.filter(c => c.id !== id)
        if (!remaining.length) {
          const conv = newConv()
          return { conversations: [conv], activeConversationId: conv.id }
        }
        return {
          conversations: remaining,
          activeConversationId: s.activeConversationId === id
            ? remaining[0].id : s.activeConversationId,
        }
      }),

      togglePinConversation: (id) => set(s => ({
        conversations: s.conversations.map(c =>
          c.id === id ? { ...c, pinned: !c.pinned } : c),
      })),

      switchConversation: (id) => set({
        activeConversationId: id,
        todos: [], toolCalls: [], agentThoughts: [], streamingText: '',
        confirmRequest: null, askRequest: null,
        isExecuting:          false,
        state:                'idle' as CosmosState,
        currentActionCommand: '',
      }),
    }),
    {
      name: 'friday-v3',
      // Versioned so future shape changes get a migrate hook instead of
      // rehydrating old-shape objects straight into the store.
      version: 2,
      migrate: (persisted: any) => {
        // v1 → v2: settings/tabs added — merge defaults over whatever exists.
        if (persisted && typeof persisted === 'object') {
          persisted.settings = { ...DEFAULT_SETTINGS, ...(persisted.settings ?? {}) }
        }
        return persisted as any
      },
      // Trailing-debounced writes — persist calls setItem on every set(), so
      // without this every streaming delta / audio tick re-serialized the
      // whole conversations blob to localStorage.
      storage: debouncedStorage,
      // Only persist conversation history + preferences — not ephemeral HUD state
      partialize: (s): PersistedSlice => ({
        conversations:        s.conversations,
        activeConversationId: s.activeConversationId,
        permissionMode:       s.permissionMode,
        earconsEnabled:       s.earconsEnabled,
        settings:             s.settings,
        activeLeftTab:        s.activeLeftTab,
        activeRightTab:       s.activeRightTab,
      }),
    }
  )
)
