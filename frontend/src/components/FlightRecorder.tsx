import { ActionRun } from '../store'
import { HistoryRun } from './AgentActivity'

// Flight Recorder tab — the archive of past runs (todos + tool calls),
// newest first, expandable.

export default function FlightRecorder({ history }: { history: ActionRun[] }) {
  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: '8px 10px', minHeight: 0 }}>
      {history.length === 0 ? (
        <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label, 9px)',
          color: 'var(--text-lo)', textAlign: 'center', marginTop: 24 }}>
          No recorded runs in this conversation yet.
        </div>
      ) : (
        <>
          <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap, 8px)',
            color: 'var(--text-lo)', marginBottom: 6, letterSpacing: '0.1em' }}>
            PAST RUNS — CLICK TO EXPAND
          </div>
          {history.map((run, i) => <HistoryRun key={run.id} run={run} defaultOpen={i === 0} />)}
        </>
      )}
    </div>
  )
}
