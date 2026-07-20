import { useShallow } from 'zustand/react/shallow'
import { useCosmosStore, WEATHER_CITIES } from '../store'

// Settings tab — the knobs that used to be hardcoded tribal constants:
// voice/TTS on-off, earcons, motion, type scale, voice rate, weather city.

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      gap: 10, padding: '8px 0', borderBottom: '1px solid rgba(0,212,255,0.06)' }}>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label, 10px)',
          color: 'var(--text-hi)' }}>{label}</div>
        {hint && <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap, 8.5px)',
          color: 'var(--text)', marginTop: 2, lineHeight: 1.4 }}>{hint}</div>}
      </div>
      <div style={{ flexShrink: 0 }}>{children}</div>
    </div>
  )
}

function Switch({ on, onToggle, label }: { on: boolean; onToggle: () => void; label: string }) {
  return (
    <button role="switch" aria-checked={on} aria-label={label} onClick={onToggle}
      style={{ width: 34, height: 18, borderRadius: 9, cursor: 'pointer', position: 'relative',
        background: on ? 'rgba(0,212,255,0.25)' : 'rgba(255,255,255,0.06)',
        border: `1px solid ${on ? 'var(--cyan-50)' : 'rgba(255,255,255,0.15)'}`,
        transition: 'background 0.15s, border-color 0.15s' }}>
      <span style={{ position: 'absolute', top: 2, left: on ? 17 : 2, width: 12, height: 12,
        borderRadius: '50%', background: on ? 'var(--cyan)' : 'var(--text-lo)',
        boxShadow: on ? '0 0 8px rgba(0,212,255,0.7)' : 'none', transition: 'left 0.15s' }} />
    </button>
  )
}

export default function SettingsPanel() {
  const { settings, updateSettings, earconsEnabled, setEarconsEnabled,
          permissionMode } = useCosmosStore(useShallow(s => ({
    settings: s.settings, updateSettings: s.updateSettings,
    earconsEnabled: s.earconsEnabled, setEarconsEnabled: s.setEarconsEnabled,
    permissionMode: s.permissionMode,
  })))

  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: '10px 14px', minHeight: 0 }}>
      <div style={{ fontFamily: 'var(--font-d)', fontSize: 'var(--fs-cap, 8px)',
        letterSpacing: '0.2em', color: 'rgba(0,212,255,0.55)', margin: '2px 0 4px' }}>
        VOICE
      </div>
      <Row label="Wake-word listening" hint='Mic listens for "Cosmos …" continuously'>
        <Switch on={settings.voiceEnabled} label="Wake-word listening"
          onToggle={() => updateSettings({ voiceEnabled: !settings.voiceEnabled })} />
      </Row>
      <Row label="Spoken replies (TTS)" hint="Off = silent, text only">
        <Switch on={settings.ttsEnabled} label="Spoken replies"
          onToggle={() => updateSettings({ ttsEnabled: !settings.ttsEnabled })} />
      </Row>
      <Row label="Earcons" hint="Chirps on wake / accept / error / sleep">
        <Switch on={earconsEnabled} label="Earcons"
          onToggle={() => setEarconsEnabled(!earconsEnabled)} />
      </Row>
      <Row label={`Voice speed — ${settings.voiceRate.toFixed(2)}×`}>
        <input type="range" min={0.8} max={1.4} step={0.05} value={settings.voiceRate}
          aria-label="Voice speed"
          onChange={e => updateSettings({ voiceRate: Number(e.target.value) })}
          style={{ width: 110, accentColor: '#00d4ff' }} />
      </Row>

      <div style={{ fontFamily: 'var(--font-d)', fontSize: 'var(--fs-cap, 8px)',
        letterSpacing: '0.2em', color: 'rgba(0,212,255,0.55)', margin: '14px 0 4px' }}>
        DISPLAY
      </div>
      <Row label="Large type" hint="Bigger, more readable HUD text">
        <Switch on={settings.uiScale === 'large'} label="Large type"
          onToggle={() => updateSettings({ uiScale: settings.uiScale === 'large' ? 'normal' : 'large' })} />
      </Row>
      <Row label="Reduced motion" hint="Disables scanlines, sweeps and shimmers">
        <Switch on={settings.reducedMotion} label="Reduced motion"
          onToggle={() => updateSettings({ reducedMotion: !settings.reducedMotion })} />
      </Row>

      <div style={{ fontFamily: 'var(--font-d)', fontSize: 'var(--fs-cap, 8px)',
        letterSpacing: '0.2em', color: 'rgba(0,212,255,0.55)', margin: '14px 0 4px' }}>
        ENVIRONMENT
      </div>
      <Row label="Weather city">
        <select value={settings.weatherCity} aria-label="Weather city"
          onChange={e => updateSettings({ weatherCity: e.target.value })}
          className="hud-input"
          style={{ padding: '4px 8px', borderRadius: 2, fontSize: 'var(--fs-label, 11px)',
            background: 'rgba(0,20,40,0.9)' }}>
          {Object.keys(WEATHER_CITIES).map(c => <option key={c} value={c}>{c}</option>)}
        </select>
      </Row>
      <Row label="Permission mode"
        hint={permissionMode === 'full'
          ? 'FULL ACCESS — outward actions run freely (toggle in the top bar)'
          : 'GUARDED — outward actions ask first (toggle in the top bar)'}>
        <span style={{ fontFamily: 'var(--font-d)', fontSize: 'var(--fs-cap, 8px)',
          letterSpacing: '0.12em',
          color: permissionMode === 'full' ? 'var(--amber)' : 'var(--cyan)' }}>
          {permissionMode === 'full' ? '⚡ FULL' : '🛡 GUARDED'}
        </span>
      </Row>
    </div>
  )
}
