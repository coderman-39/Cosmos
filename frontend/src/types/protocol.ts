import type { CosmosState, WeatherData } from '../store'

// ══════════════════════════════════════════════════════════════
// COSMOS WebSocket protocol v3 — single source of truth
// Both sides MUST match these message shapes exactly.
// ══════════════════════════════════════════════════════════════

// ─── Shared payloads ───────────────────────────────────────────

export type TodoStatus = 'pending' | 'in_progress' | 'completed'

export interface Todo {
  id: string
  text: string
  status: TodoStatus
}

// ─── Client → Server ───────────────────────────────────────────

export type PermissionMode = 'ask' | 'full'

export interface CommandMsg {
  type: 'command'; text: string; mode?: PermissionMode
  /** Which frontend conversation this belongs to — backend keeps one history per id */
  conversation_id?: string
}
export interface ConfirmMsg { type: 'confirm'; response: 'yes' | 'no'; id?: string }
export interface StopMsg    { type: 'stop' }
export interface SetModeMsg { type: 'set_mode'; mode: PermissionMode }
export interface NewChatMsg            { type: 'new_chat'; id: string }
export interface SwitchConversationMsg { type: 'switch_conversation'; id: string }
export interface DeleteConversationMsg { type: 'delete_conversation'; id: string }
/** Speculative warm-up fired on the first wake-word interim — backend starts
    the focus-context probe during STT endpointing. Strictly side-effect-free. */
export interface PrefetchMsg           { type: 'prefetch' }

export type ClientMessage =
  | CommandMsg | ConfirmMsg | StopMsg | SetModeMsg
  | NewChatMsg | SwitchConversationMsg | DeleteConversationMsg | PrefetchMsg

// ─── Server → Client ───────────────────────────────────────────

/** Orb/HUD state change */
export interface StateMsg { type: 'state'; state: CosmosState }

/** Final reply — FE speaks it (TTS) + adds to chat, then idle */
export interface ResponseMsg { type: 'response'; text: string }

/** Interim spoken update (TTS only) */
export interface SpeakMsg { type: 'speak'; text: string }

/** Weather widget data */
export interface WeatherMsg { type: 'weather'; payload: WeatherData }

/** FULL todo list replacement (not a diff) */
export interface TodosMsg { type: 'todos'; todos: Todo[] }

/** Agent invoked a tool; label is a short human string */
export interface ToolStartMsg { type: 'tool_start'; tool_id: string; tool: string; label: string }

/** Tool finished; detail ≤200 chars */
export interface ToolDoneMsg { type: 'tool_done'; tool_id: string; ok: boolean; detail?: string }

/** Short narration of agent reasoning between tool calls (display only, never TTS) */
export interface AgentThoughtMsg { type: 'agent_thought'; text: string }

/** Streamed text delta of the turn being generated — shown live, never TTS'd */
export interface ResponseDeltaMsg { type: 'response_delta'; text: string }

/** Reset the live streaming buffer (a stream aborted and is being retried) */
export interface ResponseDeltaResetMsg { type: 'response_delta_reset' }

/** Agent wants to do something risky; answer via `confirm` msg or voice yes/no.
    `detail` is the exact tool call (JSON) for the SHOW EXACT COMMAND expander.
    `steps` (plan preview / batched approvals): one entry per gated call —
    approving the banner approves them ALL at once. */
export interface PlanStep { summary: string; danger: string }
export interface ConfirmRequestMsg { type: 'confirm_request'; id: string; summary: string; danger: string; detail?: string; steps?: PlanStep[] }

/** The confirm banner went unanswered too long — backend auto-declined; FE clears it */
export interface ConfirmTimeoutMsg { type: 'confirm_timeout'; id: string }

/** Agent needs info; FE speaks the question; user answers by voice or typing */
export interface AskUserMsg { type: 'ask_user'; id: string; question: string }

/** A task run began (FE clears current run panel) */
export interface ActionStartMsg { type: 'action_start'; command?: string }

/** Task run ended (FE archives run to history; `response` carries the spoken summary) */
export interface ActionCompleteMsg { type: 'action_complete'; summary: string }

/** Proactive suggestion chip ("you usually do this around now") — dismissible */
export interface SuggestionMsg { type: 'suggestion'; text: string }

/** Run telemetry — which model actually answered and how long it took */
export interface RunMetaMsg { type: 'run_meta'; model: string; elapsed_ms: number; turns?: number }

/** Scheduled digest (morning briefing etc.) — rendered as a silent card,
    NEVER spoken (a multi-section digest must not be TTS'd on every tab). */
export interface BriefingCardMsg { type: 'briefing_card'; title: string; markdown: string; ts?: string }

/** Context ledger — the exact evidence set an answer stood on (recall rows,
    RAPTOR themes, tool artifacts, compressed blocks). Emitted at run end for
    inspectable grounding; display-only, never spoken. */
export interface ContextLedgerEntry { kind: string; ref: string; summary?: string; version?: string }
export interface ContextLedgerMsg { type: 'context_ledger'; entries: ContextLedgerEntry[] }

export type ServerMessage =
  | StateMsg
  | ResponseMsg
  | SpeakMsg
  | WeatherMsg
  | TodosMsg
  | ToolStartMsg
  | ToolDoneMsg
  | AgentThoughtMsg
  | ResponseDeltaMsg
  | ResponseDeltaResetMsg
  | ConfirmRequestMsg
  | ConfirmTimeoutMsg
  | AskUserMsg
  | ActionStartMsg
  | ActionCompleteMsg
  | SuggestionMsg
  | RunMetaMsg
  | BriefingCardMsg
  | ContextLedgerMsg
