import { useEffect, useRef } from 'react'
import * as THREE from 'three'
import type { Page } from '../store'

// ── "Cosmos Command Center" landing — ported from the design export. ──
// The markup is injected verbatim (self-contained inline styles); the
// Three.js particle brain, boot sequence, state machine, telemetry, waveform
// and scroll-reveal are driven imperatively in the effect below, wired to the
// app router. Boot plays once per session.
let bootedOnce = false

const HTML = `
  <!-- AURORA / DEEP-SPACE BACKGROUND -->
  <div style="position:fixed;inset:-25%;z-index:0;pointer-events:none;filter:blur(60px);opacity:.75;animation:fri-aurora 26s ease-in-out infinite;background:radial-gradient(38% 44% at 28% 32%,rgba(0,120,180,.42),transparent 70%),radial-gradient(34% 40% at 74% 60%,rgba(96,64,220,.34),transparent 70%),radial-gradient(30% 34% at 58% 20%,rgba(0,190,230,.22),transparent 70%);"></div>
  <div style="position:fixed;inset:0;z-index:0;pointer-events:none;background:radial-gradient(120% 90% at 50% 0%,transparent 40%,rgba(2,9,22,.6) 100%);"></div>
  <div style="position:fixed;inset:0;z-index:60;pointer-events:none;mix-blend-mode:overlay;opacity:.5;background-image:repeating-linear-gradient(0deg,rgba(120,220,255,.05) 0px,rgba(120,220,255,.05) 1px,transparent 1px,transparent 3px);animation:fri-scan 22s linear infinite;"></div>
  <div style="position:fixed;inset:0;z-index:59;pointer-events:none;box-shadow:inset 0 0 320px 60px rgba(0,4,12,.9);"></div>

  <!-- NAV -->
  <nav style="position:fixed;top:0;left:0;right:0;z-index:70;display:flex;align-items:center;justify-content:space-between;padding:18px 34px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);background:linear-gradient(180deg,rgba(3,12,26,.72),rgba(3,12,26,.12));border-bottom:1px solid rgba(0,212,255,.1);">
    <div style="display:flex;align-items:center;gap:13px;">
      <div style="position:relative;width:26px;height:26px;">
        <div style="position:absolute;inset:0;border:1.5px solid #00d4ff;border-radius:7px;transform:rotate(45deg);box-shadow:0 0 14px rgba(0,212,255,.6);"></div>
        <div style="position:absolute;inset:8px;background:#00d4ff;border-radius:3px;transform:rotate(45deg);box-shadow:0 0 10px rgba(0,212,255,.9);"></div>
      </div>
      <span style="font-family:Orbitron,sans-serif;font-weight:800;letter-spacing:.42em;font-size:16px;color:#eafcff;text-shadow:0 0 18px rgba(0,212,255,.55);padding-left:.42em;">COSMOS</span>
    </div>
    <div style="display:flex;align-items:center;gap:24px;font-family:'Share Tech Mono',monospace;font-size:12px;letter-spacing:.18em;text-transform:uppercase;">
      <a href="#" data-nav="home" style="color:#9fd6ea;">Home</a>
      <a href="#" data-nav="agent" style="color:#6f93a8;">Agent</a>
      <a href="#" data-nav="nexus" style="color:#6f93a8;">Nexus</a>
      <a href="#" data-nav="dossier" style="color:#6f93a8;">Dossier</a>
      <a href="#" data-nav="vision" style="color:#6f93a8;">Vision</a>
      <a href="#" data-nav="kinesis" style="color:#6f93a8;">Kinesis</a>
      <a href="#" data-nav="slack" style="color:#6f93a8;">Slack</a>
      <a href="#" data-nav="panel" style="color:#6f93a8;">Panel</a>
      <a href="#" data-nav="skills" style="color:#6f93a8;">Skills</a>
      <a href="#" data-nav="mutate" style="color:#6f93a8;">Mutate</a>
      <a href="#" data-nav="mcps" style="color:#6f93a8;">Connectors</a>
    </div>
    <div style="display:flex;align-items:center;gap:16px;">
      <button data-el="burger" aria-label="menu" style="cursor:pointer;background:none;border:none;width:30px;height:22px;position:relative;padding:0;">
        <span style="position:absolute;left:0;top:2px;height:2px;width:100%;background:#00d4ff;border-radius:2px;transition:transform .35s cubic-bezier(.7,0,.2,1),opacity .25s;box-shadow:0 0 8px rgba(0,212,255,.6);"></span>
        <span style="position:absolute;left:0;top:10px;height:2px;width:100%;background:#00d4ff;border-radius:2px;transition:transform .35s cubic-bezier(.7,0,.2,1),opacity .25s;box-shadow:0 0 8px rgba(0,212,255,.6);"></span>
        <span style="position:absolute;left:0;top:18px;height:2px;width:100%;background:#00d4ff;border-radius:2px;transition:transform .35s cubic-bezier(.7,0,.2,1),opacity .25s;box-shadow:0 0 8px rgba(0,212,255,.6);"></span>
      </button>
    </div>
  </nav>

  <!-- HERO -->
  <section style="position:relative;z-index:10;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;overflow:hidden;">
    <div style="position:absolute;top:48%;left:50%;transform:translate(-50%,-50%);width:min(760px,92vw);height:min(760px,92vw);z-index:1;pointer-events:none;background:radial-gradient(circle,rgba(0,150,200,.16),rgba(0,90,150,.05) 42%,transparent 66%);"></div>
    <div style="position:absolute;top:48%;left:50%;z-index:2;pointer-events:none;">
      <div style="position:absolute;top:0;left:0;width:640px;height:640px;border:1px solid rgba(0,212,255,.5);border-radius:50%;animation:fri-radar 6s ease-out infinite;"></div>
      <div style="position:absolute;top:0;left:0;width:640px;height:640px;border:1px solid rgba(0,212,255,.4);border-radius:50%;animation:fri-radar 6s ease-out infinite 2s;"></div>
      <div style="position:absolute;top:0;left:0;width:640px;height:640px;border:1px solid rgba(124,92,255,.4);border-radius:50%;animation:fri-radar 6s ease-out infinite 4s;"></div>
    </div>
    <canvas data-el="canvas" style="position:absolute;inset:0;z-index:3;width:100%;height:100%;display:block;"></canvas>

    <div data-el="hud" style="position:absolute;inset:0;z-index:4;pointer-events:none;font-family:'Share Tech Mono',monospace;color:#6fd3ea;opacity:0;transition:opacity 1.1s ease .2s;">
      <div style="position:absolute;top:104px;left:34px;font-size:11px;letter-spacing:.18em;line-height:1.9;color:#5fa9c4;">
        <div style="color:#00d4ff;">◈ NEURAL CORE</div><div>MODEL&nbsp;&nbsp;·&nbsp;cosmos-ultra</div><div>PARAMS&nbsp;·&nbsp;1.8T</div><div>REGION&nbsp;·&nbsp;us-hud-01</div>
      </div>
      <div style="position:absolute;top:104px;right:34px;text-align:right;font-size:11px;letter-spacing:.18em;line-height:1.9;color:#5fa9c4;">
        <div style="color:#00d4ff;">◈ TELEMETRY</div><div>LATENCY&nbsp;·&nbsp;<span data-el="stat-latency" style="color:#bfeeff;">142</span>ms</div><div>TOKENS/S&nbsp;·&nbsp;<span data-el="stat-tokens" style="color:#bfeeff;">318</span></div><div>CONNECTORS&nbsp;·&nbsp;<span style="color:#3fe0a0;">9 ONLINE</span></div>
      </div>
      <div style="position:absolute;bottom:34px;left:34px;font-size:11px;letter-spacing:.18em;color:#5fa9c4;">
        <span style="display:inline-flex;align-items:center;gap:8px;"><span style="width:7px;height:7px;border-radius:50%;background:#3fe0a0;color:#3fe0a0;animation:fri-blink 1.8s infinite;"></span>GUARDED BY DEFAULT</span>
      </div>
      <div style="position:absolute;bottom:34px;right:34px;font-size:11px;letter-spacing:.18em;color:#5fa9c4;">SESSION · <span data-el="stat-clock">00:00:00</span></div>
    </div>

    <div data-el="hero-content" style="position:relative;z-index:5;text-align:center;pointer-events:none;opacity:0;transform:translateY(24px);transition:opacity 1.2s cubic-bezier(.2,.7,.2,1),transform 1.2s cubic-bezier(.2,.7,.2,1);">
      <div style="display:inline-flex;align-items:center;gap:10px;pointer-events:auto;font-family:'Share Tech Mono',monospace;font-size:11.5px;letter-spacing:.32em;text-transform:uppercase;color:#8fdcff;padding:8px 16px;border:1px solid rgba(0,212,255,.28);border-radius:999px;background:rgba(0,60,90,.22);backdrop-filter:blur(6px);margin-bottom:30px;">
        <span data-el="chip-dot" style="width:8px;height:8px;border-radius:50%;background:#00d4ff;color:#00d4ff;animation:fri-blink 1.6s infinite;"></span>
        <span data-el="state-name">SYSTEM · IDLE</span>
      </div>
      <h1 style="margin:0;font-family:Orbitron,sans-serif;font-weight:800;font-size:clamp(46px,8.2vw,116px);line-height:.94;letter-spacing:.02em;color:#f2fdff;text-shadow:0 0 40px rgba(0,180,230,.55),0 0 90px rgba(0,120,200,.3);">At your<br>service.</h1>
      <p data-el="state-desc" style="margin:26px auto 0;max-width:560px;font-size:clamp(15px,1.7vw,19px);line-height:1.6;color:#a9cede;font-weight:300;">Talk to it, it acts. COSMOS is a personal AI agent that reasons, remembers, and executes across your tools — with a human in the loop when it counts.</p>
      <div style="display:flex;gap:16px;justify-content:center;margin-top:38px;pointer-events:auto;">
        <button data-el="cta-primary" style="cursor:pointer;font-family:'Chakra Petch',sans-serif;font-weight:600;font-size:15px;letter-spacing:.06em;color:#02121c;background:linear-gradient(180deg,#26e0ff,#00b6e6);border:none;padding:16px 32px;border-radius:11px;box-shadow:0 0 30px rgba(0,212,255,.55),inset 0 1px 0 rgba(255,255,255,.55);transition:transform .3s,box-shadow .3s;">Launch Agent →</button>
        <button data-el="cta-secondary" style="cursor:pointer;font-family:'Chakra Petch',sans-serif;font-weight:500;font-size:15px;letter-spacing:.06em;color:#bfeeff;background:rgba(10,40,60,.4);border:1px solid rgba(0,212,255,.35);padding:16px 30px;border-radius:11px;backdrop-filter:blur(8px);transition:background .3s,border-color .3s;">Watch it think</button>
      </div>
    </div>

    <div style="position:absolute;bottom:26px;left:50%;transform:translateX(-50%);z-index:5;display:flex;flex-direction:column;align-items:center;gap:9px;font-family:'Share Tech Mono',monospace;font-size:10px;letter-spacing:.3em;color:#5c9ab0;">
      <span>SCROLL</span>
      <div style="width:22px;height:36px;border:1px solid rgba(0,212,255,.4);border-radius:12px;position:relative;"><span style="position:absolute;top:6px;left:50%;transform:translateX(-50%);width:3px;height:7px;border-radius:2px;background:#00d4ff;animation:fri-scrollpulse 1.8s ease-in-out infinite;"></span></div>
    </div>
  </section>

  <!-- BOOT OVERLAY -->
  <div data-el="boot" style="position:fixed;inset:0;z-index:120;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#010610;transition:opacity 1s ease;">
    <div style="width:min(560px,86vw);">
      <div style="display:flex;align-items:center;justify-content:space-between;font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:.3em;color:#4f8298;margin-bottom:18px;"><span>COSMOS OS</span><span>v3.0 · SECURE BOOT</span></div>
      <div style="font-family:Orbitron,sans-serif;font-weight:900;font-size:clamp(40px,9vw,88px);letter-spacing:.08em;color:#0a3346;text-shadow:0 0 30px rgba(0,140,190,.2);line-height:1;margin-bottom:26px;"><span style="background:linear-gradient(90deg,#0a3346 0%,#0a3346 40%,#7fe9ff 50%,#0a3346 60%,#0a3346 100%);background-size:260% 100%;-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;animation:fri-shimmer 2.4s linear infinite;">COSMOS</span></div>
      <div data-el="boot-log" style="font-family:'Share Tech Mono',monospace;font-size:12.5px;line-height:2;color:#5fb4cf;min-height:170px;letter-spacing:.04em;"></div>
      <div style="margin-top:18px;height:3px;width:100%;background:rgba(0,120,160,.16);border-radius:3px;overflow:hidden;"><div data-el="boot-bar" style="height:100%;width:0%;background:linear-gradient(90deg,#0088c0,#26e0ff);box-shadow:0 0 14px rgba(0,212,255,.7);transition:width .25s ease;"></div></div>
    </div>
  </div>

  <!-- CAPABILITIES -->
  <section id="capabilities" style="position:relative;z-index:10;padding:130px 30px 60px;max-width:1220px;margin:0 auto;">
    <div data-reveal style="opacity:0;transform:translateY(30px);transition:opacity .9s cubic-bezier(.2,.7,.2,1),transform .9s cubic-bezier(.2,.7,.2,1);text-align:center;margin-bottom:64px;">
      <div style="font-family:'Share Tech Mono',monospace;font-size:12px;letter-spacing:.4em;text-transform:uppercase;color:#00d4ff;margin-bottom:16px;">◈ Capabilities</div>
      <h2 style="margin:0;font-family:Orbitron,sans-serif;font-weight:700;font-size:clamp(30px,4.6vw,56px);letter-spacing:.01em;color:#eafcff;text-shadow:0 0 30px rgba(0,150,210,.35);">One agent. Every surface.</h2>
      <p style="margin:20px auto 0;max-width:600px;color:#9fc2d4;font-weight:300;font-size:17px;line-height:1.6;">COSMOS plans, previews, and executes real work across your stack — narrating every step and asking before it does anything irreversible.</p>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:22px;">
      <div data-reveal data-card style="opacity:0;transform:translateY(30px);transition:opacity .9s cubic-bezier(.2,.7,.2,1),transform .9s cubic-bezier(.2,.7,.2,1);grid-column:span 2;position:relative;overflow:hidden;padding:34px;border-radius:18px;border:1px solid rgba(0,212,255,.16);background:linear-gradient(160deg,rgba(9,30,50,.72),rgba(5,16,30,.5));backdrop-filter:blur(10px);box-shadow:0 20px 60px rgba(0,10,25,.5);">
        <div style="position:absolute;top:-40px;right:-30px;width:220px;height:220px;background:radial-gradient(circle,rgba(0,180,230,.28),transparent 70%);pointer-events:none;"></div>
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:20px;"><svg width="30" height="30" viewBox="0 0 30 30" fill="none"><rect x="12" y="4" width="6" height="15" rx="3" stroke="#00d4ff" stroke-width="1.6"/><path d="M7 14a8 8 0 0 0 16 0M15 22v4M10 26h10" stroke="#00d4ff" stroke-width="1.6" stroke-linecap="round"/></svg><span style="font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:.28em;text-transform:uppercase;color:#7fbfd6;">01 · Realtime</span></div>
        <h3 style="margin:0 0 12px;font-family:'Chakra Petch',sans-serif;font-weight:600;font-size:26px;color:#eafcff;">Voice agent</h3>
        <p style="margin:0;max-width:440px;color:#9fc2d4;font-weight:300;font-size:15.5px;line-height:1.65;">Speak naturally. COSMOS listens with a live waveform, thinks out loud, and acts — sub-second turn-taking with barge-in and full transcript recall.</p>
        <div style="display:flex;align-items:flex-end;gap:4px;height:40px;margin-top:24px;" data-el="wave"></div>
      </div>
      <div data-reveal data-card style="opacity:0;transform:translateY(30px);transition:opacity .9s cubic-bezier(.2,.7,.2,1),transform .9s cubic-bezier(.2,.7,.2,1);position:relative;overflow:hidden;padding:34px;border-radius:18px;border:1px solid rgba(124,92,255,.18);background:linear-gradient(160deg,rgba(24,18,54,.7),rgba(8,10,28,.5));backdrop-filter:blur(10px);box-shadow:0 20px 60px rgba(0,10,25,.5);">
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:20px;"><svg width="30" height="30" viewBox="0 0 30 30" fill="none"><rect x="5" y="7" width="20" height="16" rx="2.5" stroke="#a58cff" stroke-width="1.6"/><path d="M5 10l10 7 10-7" stroke="#a58cff" stroke-width="1.6" stroke-linecap="round"/></svg><span style="font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:.28em;text-transform:uppercase;color:#b0a2e6;">02 · Workspace</span></div>
        <h3 style="margin:0 0 12px;font-family:'Chakra Petch',sans-serif;font-weight:600;font-size:22px;color:#eafcff;">Google Workspace</h3>
        <p style="margin:0;color:#b6b0d4;font-weight:300;font-size:15px;line-height:1.6;">Triage Gmail, schedule across Calendar, draft in Docs, crunch Sheets, join Meet — all with one grant.</p>
      </div>
      <div data-reveal data-card style="opacity:0;transform:translateY(30px);transition:opacity .9s cubic-bezier(.2,.7,.2,1),transform .9s cubic-bezier(.2,.7,.2,1);position:relative;overflow:hidden;padding:34px;border-radius:18px;border:1px solid rgba(0,212,255,.16);background:linear-gradient(160deg,rgba(9,30,50,.7),rgba(5,16,30,.5));backdrop-filter:blur(10px);box-shadow:0 20px 60px rgba(0,10,25,.5);">
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:20px;"><svg width="30" height="30" viewBox="0 0 30 30" fill="none"><circle cx="15" cy="15" r="10" stroke="#00d4ff" stroke-width="1.6"/><path d="M15 15V8M15 15l5 3" stroke="#00d4ff" stroke-width="1.6" stroke-linecap="round"/></svg><span style="font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:.28em;text-transform:uppercase;color:#7fbfd6;">03 · Proactive</span></div>
        <h3 style="margin:0 0 12px;font-family:'Chakra Petch',sans-serif;font-weight:600;font-size:22px;color:#eafcff;">Proactive briefings</h3>
        <p style="margin:0;color:#9fc2d4;font-weight:300;font-size:15px;line-height:1.6;">Wake up to a synthesized briefing — overnight threads, conflicts, and the three things that need you today.</p>
      </div>
      <div data-reveal data-card style="opacity:0;transform:translateY(30px);transition:opacity .9s cubic-bezier(.2,.7,.2,1),transform .9s cubic-bezier(.2,.7,.2,1);grid-column:span 2;position:relative;overflow:hidden;padding:34px;border-radius:18px;border:1px solid rgba(0,212,255,.16);background:linear-gradient(160deg,rgba(9,30,50,.7),rgba(5,16,30,.5));backdrop-filter:blur(10px);box-shadow:0 20px 60px rgba(0,10,25,.5);">
        <div style="position:absolute;top:-40px;left:-30px;width:200px;height:200px;background:radial-gradient(circle,rgba(124,92,255,.24),transparent 70%);pointer-events:none;"></div>
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:20px;"><svg width="30" height="30" viewBox="0 0 30 30" fill="none"><circle cx="6" cy="15" r="3" stroke="#00d4ff" stroke-width="1.6"/><circle cx="24" cy="7" r="3" stroke="#a58cff" stroke-width="1.6"/><circle cx="24" cy="23" r="3" stroke="#a58cff" stroke-width="1.6"/><path d="M9 15c5 0 7-8 12-8M9 15c5 0 7 8 12 8" stroke="#00d4ff" stroke-width="1.4"/></svg><span style="font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:.28em;text-transform:uppercase;color:#7fbfd6;">04 · Orchestration</span></div>
        <h3 style="margin:0 0 12px;font-family:'Chakra Petch',sans-serif;font-weight:600;font-size:26px;color:#eafcff;">Parallel sub-agents</h3>
        <p style="margin:0;max-width:460px;color:#9fc2d4;font-weight:300;font-size:15.5px;line-height:1.65;">Hard tasks fan out into specialist sub-agents that work in parallel, then merge their findings back into one answer — visible as branching light-streams.</p>
      </div>
      <div data-reveal data-card style="opacity:0;transform:translateY(30px);transition:opacity .9s cubic-bezier(.2,.7,.2,1),transform .9s cubic-bezier(.2,.7,.2,1);position:relative;overflow:hidden;padding:34px;border-radius:18px;border:1px solid rgba(255,176,32,.22);background:linear-gradient(160deg,rgba(40,28,8,.6),rgba(14,12,20,.5));backdrop-filter:blur(10px);box-shadow:0 20px 60px rgba(0,10,25,.5);">
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:20px;"><svg width="30" height="30" viewBox="0 0 30 30" fill="none"><rect x="6" y="5" width="18" height="20" rx="2.5" stroke="#ffb020" stroke-width="1.6"/><path d="M10 11h10M10 15h10M10 19h6" stroke="#ffb020" stroke-width="1.6" stroke-linecap="round"/></svg><span style="font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:.28em;text-transform:uppercase;color:#e6b465;">05 · Guardrail</span></div>
        <h3 style="margin:0 0 12px;font-family:'Chakra Petch',sans-serif;font-weight:600;font-size:22px;color:#fff3df;">Plan-preview &amp; undo</h3>
        <p style="margin:0;color:#e2cba6;font-weight:300;font-size:15px;line-height:1.6;">See the full plan before it runs. Approve, edit, or reject any step — and undo anything with a single command.</p>
      </div>
      <div data-reveal data-card style="opacity:0;transform:translateY(30px);transition:opacity .9s cubic-bezier(.2,.7,.2,1),transform .9s cubic-bezier(.2,.7,.2,1);grid-column:span 3;position:relative;overflow:hidden;padding:38px;border-radius:18px;border:1px solid rgba(0,212,255,.16);background:linear-gradient(120deg,rgba(9,30,50,.7),rgba(16,12,42,.55));backdrop-filter:blur(10px);box-shadow:0 20px 60px rgba(0,10,25,.5);display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:30px;">
        <div style="max-width:560px;">
          <div style="display:flex;align-items:center;gap:14px;margin-bottom:18px;"><svg width="30" height="30" viewBox="0 0 30 30" fill="none"><circle cx="15" cy="15" r="4" stroke="#00d4ff" stroke-width="1.6"/><circle cx="15" cy="15" r="10" stroke="#00d4ff" stroke-width="1.2" stroke-dasharray="3 4"/><circle cx="25" cy="15" r="1.6" fill="#a58cff"/><circle cx="8" cy="8" r="1.6" fill="#00d4ff"/><circle cx="9" cy="23" r="1.6" fill="#a58cff"/></svg><span style="font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:.28em;text-transform:uppercase;color:#7fbfd6;">06 · Memory</span></div>
          <h3 style="margin:0 0 12px;font-family:'Chakra Petch',sans-serif;font-weight:600;font-size:26px;color:#eafcff;">Semantic memory</h3>
          <p style="margin:0;color:#9fc2d4;font-weight:300;font-size:15.5px;line-height:1.65;">Everything COSMOS learns coalesces into a living knowledge graph. Recalled context drifts back in as glowing particles — never re-explain yourself.</p>
        </div>
        <div style="display:flex;gap:26px;font-family:Orbitron,sans-serif;">
          <div style="text-align:center;"><div style="font-size:40px;font-weight:800;color:#00d4ff;text-shadow:0 0 22px rgba(0,212,255,.5);">9</div><div style="font-family:'Share Tech Mono',monospace;font-size:10px;letter-spacing:.24em;color:#6f9cb0;margin-top:6px;">CONNECTORS</div></div>
          <div style="text-align:center;"><div style="font-size:40px;font-weight:800;color:#a58cff;text-shadow:0 0 22px rgba(124,92,255,.5);">∞</div><div style="font-family:'Share Tech Mono',monospace;font-size:10px;letter-spacing:.24em;color:#6f9cb0;margin-top:6px;">RECALL</div></div>
          <div style="text-align:center;"><div style="font-size:40px;font-weight:800;color:#3fe0a0;text-shadow:0 0 22px rgba(63,224,160,.4);">100%</div><div style="font-family:'Share Tech Mono',monospace;font-size:10px;letter-spacing:.24em;color:#6f9cb0;margin-top:6px;">GUARDED</div></div>
        </div>
      </div>
    </div>
    <div data-reveal style="opacity:0;transform:translateY(30px);transition:opacity .9s cubic-bezier(.2,.7,.2,1),transform .9s cubic-bezier(.2,.7,.2,1);margin-top:70px;padding:38px;border-radius:18px;border:1px solid rgba(0,212,255,.14);background:linear-gradient(180deg,rgba(6,20,36,.6),rgba(4,12,24,.4));display:flex;align-items:center;gap:28px;flex-wrap:wrap;justify-content:center;text-align:center;">
      <svg width="46" height="52" viewBox="0 0 46 52" fill="none"><path d="M23 3L42 11v14c0 13-8 20-19 24C12 45 4 38 4 25V11L23 3z" stroke="#00d4ff" stroke-width="1.6" fill="rgba(0,120,160,.08)"/><path d="M16 26l5 5 10-11" stroke="#00d4ff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
      <div style="max-width:640px;"><h3 style="margin:0 0 8px;font-family:'Chakra Petch',sans-serif;font-weight:600;font-size:22px;color:#eafcff;">Guarded by default</h3><p style="margin:0;color:#9fc2d4;font-weight:300;font-size:15px;line-height:1.6;">Least-privilege scopes, on-device redaction, and mandatory confirmation for anything irreversible. COSMOS is powerful because you can trust it to pause.</p></div>
    </div>
  </section>

  <footer style="position:relative;z-index:10;border-top:1px solid rgba(0,212,255,.1);padding:40px 34px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:20px;font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:.2em;color:#5c8598;">
    <span style="color:#7fbfd6;letter-spacing:.4em;font-family:Orbitron,sans-serif;font-weight:700;">COSMOS</span>
    <span>© 2026 · NEURAL CORE ONLINE · <span style="color:#3fe0a0;">ALL SYSTEMS NOMINAL</span></span>
  </footer>

  <div data-el="mobile-menu" style="position:fixed;inset:0;z-index:110;background:rgba(2,8,18,.94);backdrop-filter:blur(16px);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:30px;opacity:0;pointer-events:none;transition:opacity .4s ease;font-family:Orbitron,sans-serif;font-weight:700;letter-spacing:.2em;">
    <a href="#" data-nav="home" style="color:#eafcff;font-size:26px;">HOME</a>
    <a href="#" data-nav="agent" style="color:#7fbfd6;font-size:26px;">AGENT</a>
    <a href="#" data-nav="nexus" style="color:#7fbfd6;font-size:26px;">NEXUS</a>
    <a href="#" data-nav="dossier" style="color:#7fbfd6;font-size:26px;">DOSSIER</a>
    <a href="#" data-nav="vision" style="color:#7fbfd6;font-size:26px;">VISION</a>
    <a href="#" data-nav="kinesis" style="color:#7fbfd6;font-size:26px;">KINESIS</a>
    <a href="#" data-nav="slack" style="color:#7fbfd6;font-size:26px;">SLACK</a>
    <a href="#" data-nav="panel" style="color:#7fbfd6;font-size:26px;">PANEL</a>
    <a href="#" data-nav="skills" style="color:#7fbfd6;font-size:26px;">SKILLS</a>
    <a href="#" data-nav="mutate" style="color:#7fbfd6;font-size:26px;">MUTATE</a>
    <a href="#" data-nav="mcps" style="color:#7fbfd6;font-size:26px;">CONNECTORS</a>
  </div>
`

export default function HomePage({ onNavigate }: { onNavigate: (p: Page) => void }) {
  const rootRef = useRef<HTMLDivElement>(null)
  const navRef = useRef(onNavigate)
  navRef.current = onNavigate

  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    const q = (sel: string) => root.querySelector(sel) as HTMLElement | null
    let disposed = false
    const timers: ReturnType<typeof setInterval>[] = []
    let raf = 0, cycleTimer: ReturnType<typeof setTimeout> | undefined
    let renderer: THREE.WebGLRenderer | undefined
    const disposables: { dispose: () => void }[] = []
    const onWin: [string, EventListener][] = []
    const addWin = (ev: string, fn: EventListener) => { window.addEventListener(ev, fn); onWin.push([ev, fn]) }

    // ── state machine ──
    const cur = { listen: 0, think: 0, exec: 0, mix: 0, r: 0, g: 0.83, b: 1 }
    const tgt = { ...cur }
    let uniforms: any = null
    const STATES: Record<string, any> = {
      idle:    { listen: 0, think: 0, exec: 0, mix: 0,    r: 0,    g: 0.83, b: 1, label: 'SYSTEM · IDLE',    color: '#00d4ff', desc: 'Talk to it, it acts. COSMOS is a personal AI agent that reasons, remembers, and executes across your tools — with a human in the loop when it counts.' },
      listen:  { listen: 1, think: 0, exec: 0, mix: 0.35, r: 0.2,  g: 0.9,  b: 1, label: 'STATE · LISTENING', color: '#26e0ff', desc: 'Listening. A ripple travels outward across the core as your voice is transcribed in realtime.' },
      think:   { listen: 0, think: 1, exec: 0, mix: 0.7,  r: 0.55, g: 0.4,  b: 1, label: 'STATE · THINKING',  color: '#a58cff', desc: 'Reasoning. Synapses spark and fan out into parallel sub-agents to plan the fastest safe path.' },
      execute: { listen: 0, think: 0, exec: 1, mix: 0.45, r: 0.4,  g: 0.9,  b: 1, label: 'STATE · EXECUTING', color: '#26e0ff', desc: 'Executing. Pulses of light travel the knowledge graph as COSMOS calls tools and returns results.' },
    }
    const setState = (name: string) => {
      const m = STATES[name]; if (!m) return
      Object.assign(tgt, { listen: m.listen, think: m.think, exec: m.exec, mix: m.mix, r: m.r, g: m.g, b: m.b })
      const nameEl = q('[data-el="state-name"]'); if (nameEl) nameEl.textContent = m.label
      const descEl = q('[data-el="state-desc"]'); if (descEl) descEl.textContent = m.desc
      const dot = q('[data-el="chip-dot"]'); if (dot) { dot.style.background = m.color; dot.style.color = m.color }
    }
    let cycleStarted = false
    const startCycle = () => {
      if (cycleStarted) return; cycleStarted = true
      const seq: [string, number][] = [['idle', 5200], ['listen', 3600], ['think', 4200], ['execute', 4400]]
      let i = 0
      const advance = () => { if (disposed) return; const [n, d] = seq[i % seq.length]; setState(n); i++; cycleTimer = setTimeout(advance, d) }
      advance()
    }
    const trigger = (seq: [string, number][]) => {
      if (cycleTimer) clearTimeout(cycleTimer); cycleStarted = true
      let i = 0
      const run = () => { if (disposed || i >= seq.length) { cycleStarted = false; startCycle(); return } setState(seq[i][0]); cycleTimer = setTimeout(run, seq[i][1]); i++ }
      run()
    }

    // ── Three.js particle brain (ported) ──
    let assembleStart = 0
    const initScene = () => {
      const canvas = q('[data-el="canvas"]') as HTMLCanvasElement | null
      if (!canvas) return
      const W = () => canvas.clientWidth || window.innerWidth
      const H = () => canvas.clientHeight || window.innerHeight
      renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true })
      renderer.setClearColor(0x000000, 0)
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      renderer.setPixelRatio(dpr); renderer.setSize(W(), H(), false)
      const scene = new THREE.Scene()
      const camera = new THREE.PerspectiveCamera(48, W() / H(), 0.1, 100)
      camera.position.set(0, 0, 5.2)
      const group = new THREE.Group(); scene.add(group)
      const hash = (x: number, y: number, z: number) => { const n = Math.sin(x * 127.1 + y * 311.7 + z * 74.7) * 43758.5453; return n - Math.floor(n) }
      const lerp = (a: number, b: number, t: number) => a + (b - a) * t
      const smooth = (t: number) => t * t * (3 - 2 * t)
      const vnoise = (x: number, y: number, z: number) => {
        const xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z)
        const xf = x - xi, yf = y - yi, zf = z - zi
        const u = smooth(xf), v = smooth(yf), w = smooth(zf)
        const c000 = hash(xi, yi, zi), c100 = hash(xi + 1, yi, zi), c010 = hash(xi, yi + 1, zi), c110 = hash(xi + 1, yi + 1, zi)
        const c001 = hash(xi, yi, zi + 1), c101 = hash(xi + 1, yi, zi + 1), c011 = hash(xi, yi + 1, zi + 1), c111 = hash(xi + 1, yi + 1, zi + 1)
        return lerp(lerp(lerp(c000, c100, u), lerp(c010, c110, u), v), lerp(lerp(c001, c101, u), lerp(c011, c111, u), v), w)
      }
      const fbm = (x: number, y: number, z: number) => { let s = 0, a = 0.5, f = 1; for (let o = 0; o < 4; o++) { s += a * vnoise(x * f, y * f, z * f); f *= 2; a *= 0.5 } return s }
      const N = 9000, SCALE = 1.55
      const positions = new Float32Array(N * 3), scatter = new Float32Array(N * 3), colors = new Float32Array(N * 3)
      const aScale = new Float32Array(N), aRand = new Float32Array(N), radii = new Float32Array(N)
      const pts: number[][] = []
      const GA = Math.PI * (3 - Math.sqrt(5))
      const cCyan = new THREE.Color(0x00d4ff), cViolet = new THREE.Color(0x7c5cff), cWhite = new THREE.Color(0xbfeeff)
      for (let i = 0; i < N; i++) {
        const y = 1 - (i / (N - 1)) * 2, rad = Math.sqrt(Math.max(0, 1 - y * y)), th = GA * i
        const bx = Math.cos(th) * rad, by = y, bz = Math.sin(th) * rad
        const n = fbm(bx * 1.9 + 5, by * 1.9 + 5, bz * 1.9 + 5)
        let r = 1 + (n - 0.4) * 0.42
        r *= 1 - 0.18 * Math.exp(-(bx * bx) / 0.03)
        let interior = false
        if (Math.random() < 0.32) { r *= 0.35 + Math.random() * 0.55; interior = true }
        const px = bx * r * SCALE, py = by * r * SCALE, pz = bz * r * SCALE
        positions[i * 3] = px; positions[i * 3 + 1] = py; positions[i * 3 + 2] = pz
        radii[i] = Math.sqrt(px * px + py * py + pz * pz); pts.push([px, py, pz])
        const sr = 3.2 + Math.random() * 2.5, sy = Math.random() * 2 - 1, srr = Math.sqrt(1 - sy * sy), sth = Math.random() * Math.PI * 2
        scatter[i * 3] = Math.cos(sth) * srr * sr; scatter[i * 3 + 1] = sy * sr; scatter[i * 3 + 2] = Math.sin(sth) * srr * sr
        const roll = Math.random(); let col = cCyan
        if (roll > 0.82) col = cViolet; else if (roll > 0.7) col = cWhite
        const dim = interior ? 0.55 : 1
        colors[i * 3] = col.r * dim; colors[i * 3 + 1] = col.g * dim; colors[i * 3 + 2] = col.b * dim
        aScale[i] = (interior ? 0.6 : 1) * (0.6 + Math.random() * 1.5); aRand[i] = Math.random()
      }
      const geo = new THREE.BufferGeometry(); disposables.push(geo)
      geo.setAttribute('position', new THREE.BufferAttribute(positions, 3))
      geo.setAttribute('aScatter', new THREE.BufferAttribute(scatter, 3))
      geo.setAttribute('aColor', new THREE.BufferAttribute(colors, 3))
      geo.setAttribute('aScale', new THREE.BufferAttribute(aScale, 1))
      geo.setAttribute('aRand', new THREE.BufferAttribute(aRand, 1))
      uniforms = {
        uTime: { value: 0 }, uSize: { value: 2.6 }, uDpr: { value: dpr },
        uListen: { value: 0 }, uThink: { value: 0 }, uExec: { value: 0 }, uAssemble: { value: 0 },
        uStateColor: { value: new THREE.Color(0x00d4ff) }, uStateMix: { value: 0 },
      }
      const vert = `uniform float uTime;uniform float uSize;uniform float uDpr;uniform float uListen;uniform float uThink;uniform float uExec;uniform float uAssemble;uniform vec3 uStateColor;uniform float uStateMix;attribute vec3 aScatter;attribute vec3 aColor;attribute float aScale;attribute float aRand;varying vec3 vColor;varying float vAlpha;float h(float n){return fract(sin(n)*43758.5453);}void main(){vec3 pos=mix(aScatter,position,uAssemble);float r=length(pos);vec3 dir=normalize(pos+0.0001);float breathe=sin(uTime*0.8+aRand*6.28)*0.018+sin(uTime*0.5)*0.03;pos+=dir*breathe*r;float wave=sin(r*5.5-uTime*3.2)*0.14*uListen;pos+=dir*wave;float j=(h(aRand*91.3+floor(uTime*10.0))-0.5);pos+=dir*j*0.22*uThink;pos.x+=sin(uTime*22.0+aRand*40.0)*0.02*uThink;pos.y+=cos(uTime*19.0+aRand*33.0)*0.02*uThink;vec4 mv=modelViewMatrix*vec4(pos,1.0);gl_Position=projectionMatrix*mv;float tw=0.5+0.5*sin(uTime*2.6+aRand*22.0);float size=uSize*aScale*(0.55+0.45*tw)*(1.0+uThink*0.6+uListen*0.2);gl_PointSize=size*uDpr*(7.0/-mv.z);vColor=mix(aColor,uStateColor,uStateMix)*0.85;vAlpha=(0.22+0.32*tw)*(0.4+0.6*uAssemble);float sweep=smoothstep(0.12,0.0,abs(r-fract(uTime*0.55)*2.2))*uExec;vColor+=vec3(0.35,0.55,0.7)*sweep;vAlpha+=sweep*0.5;}`
      const frag = `varying vec3 vColor;varying float vAlpha;void main(){vec2 uv=gl_PointCoord-0.5;float d=length(uv);float a=smoothstep(0.5,0.0,d);a=pow(a,2.2);gl_FragColor=vec4(vColor,a*vAlpha);}`
      const mat = new THREE.ShaderMaterial({ uniforms, vertexShader: vert, fragmentShader: frag, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, depthTest: false }); disposables.push(mat)
      group.add(new THREE.Points(geo, mat))
      // synapse lines
      const maxLines = 1400; const segPos: number[] = []; const segR: number[] = []; let made = 0
      for (let attempt = 0; attempt < N && made < maxLines; attempt += 3) {
        const a = pts[attempt]; let best = -1, bestD = 0.42 * 0.42
        for (let s = 0; s < 26; s++) {
          const bi = (attempt + 1 + ((Math.random() * N) | 0)) % N; const b = pts[bi]
          const dx = a[0] - b[0], dy = a[1] - b[1], dz = a[2] - b[2], dd = dx * dx + dy * dy + dz * dz
          if (dd < bestD && dd > 0.001) { bestD = dd; best = bi }
        }
        if (best >= 0) { const b = pts[best]; segPos.push(a[0], a[1], a[2], b[0], b[1], b[2]); segR.push(radii[attempt], Math.sqrt(b[0] * b[0] + b[1] * b[1] + b[2] * b[2])); made++ }
      }
      const lgeo = new THREE.BufferGeometry(); disposables.push(lgeo)
      lgeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(segPos), 3))
      lgeo.setAttribute('aR', new THREE.BufferAttribute(new Float32Array(segR), 1))
      const lvert = `uniform float uAssemble;attribute float aR;varying float vR;void main(){vR=aR;vec4 mv=modelViewMatrix*vec4(position,1.0);gl_Position=projectionMatrix*mv;}`
      const lfrag = `uniform float uTime;uniform float uThink;uniform float uExec;uniform float uAssemble;uniform vec3 uStateColor;varying float vR;void main(){float base=0.075;float flick=0.06*uThink*(0.5+0.5*sin(uTime*14.0+vR*30.0));float sweep=smoothstep(0.2,0.0,abs(vR-fract(uTime*0.55)*2.2))*uExec*0.55;float a=(base+flick+sweep)*uAssemble;gl_FragColor=vec4(uStateColor,a);}`
      const lmat = new THREE.ShaderMaterial({ uniforms, vertexShader: lvert, fragmentShader: lfrag, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, depthTest: false }); disposables.push(lmat)
      group.add(new THREE.LineSegments(lgeo, lmat))
      // halo
      const haloGeo = new THREE.SphereGeometry(2.35, 48, 48); disposables.push(haloGeo)
      const haloMat = new THREE.ShaderMaterial({
        uniforms,
        vertexShader: `varying vec3 vN;varying vec3 vV;void main(){vN=normalize(normalMatrix*normal);vec4 mv=modelViewMatrix*vec4(position,1.0);vV=normalize(-mv.xyz);gl_Position=projectionMatrix*mv;}`,
        fragmentShader: `uniform vec3 uStateColor;uniform float uAssemble;varying vec3 vN;varying vec3 vV;void main(){float f=pow(1.0-abs(dot(vN,vV)),3.6);gl_FragColor=vec4(uStateColor,f*0.24*uAssemble);}`,
        transparent: true, blending: THREE.AdditiveBlending, side: THREE.BackSide, depthWrite: false,
      }); disposables.push(haloMat)
      group.add(new THREE.Mesh(haloGeo, haloMat))
      const mouse = { x: 0, y: 0 }
      addWin('pointermove', ((e: PointerEvent) => { mouse.x = (e.clientX / window.innerWidth) * 2 - 1; mouse.y = (e.clientY / window.innerHeight) * 2 - 1 }) as EventListener)
      addWin('resize', (() => { if (!renderer) return; renderer.setSize(W(), H(), false); camera.aspect = W() / H(); camera.updateProjectionMatrix() }) as EventListener)
      const start = performance.now()
      const loop = () => {
        if (disposed || !renderer) return
        raf = requestAnimationFrame(loop)
        const t = (performance.now() - start) / 1000
        uniforms.uTime.value = t
        let asm = 0
        if (assembleStart) { const ap = Math.min(1, (performance.now() - assembleStart) / 2200); asm = ap < 1 ? (1 - Math.pow(1 - ap, 3)) : 1 }
        uniforms.uAssemble.value = asm
        const k = 0.045
        cur.listen += (tgt.listen - cur.listen) * k; cur.think += (tgt.think - cur.think) * k
        cur.exec += (tgt.exec - cur.exec) * k; cur.mix += (tgt.mix - cur.mix) * k
        cur.r += (tgt.r - cur.r) * k; cur.g += (tgt.g - cur.g) * k; cur.b += (tgt.b - cur.b) * k
        uniforms.uListen.value = cur.listen; uniforms.uThink.value = cur.think; uniforms.uExec.value = cur.exec
        uniforms.uStateMix.value = cur.mix; uniforms.uStateColor.value.setRGB(cur.r, cur.g, cur.b)
        group.rotation.y += 0.0016 + cur.think * 0.004
        group.rotation.x = Math.sin(t * 0.18) * 0.08
        camera.position.x += (mouse.x * 0.32 - camera.position.x) * 0.04
        camera.position.y += (-mouse.y * 0.24 - camera.position.y) * 0.04
        camera.lookAt(0, 0, 0)
        group.scale.setScalar(1 + Math.sin(t * 0.8) * 0.012 + cur.listen * 0.04)
        renderer.render(scene, camera)
      }
      loop()
    }

    // ── boot ──
    const revealHero = () => {
      const hero = q('[data-el="hero-content"]'); const hud = q('[data-el="hud"]')
      if (hero) { hero.style.opacity = '1'; hero.style.transform = 'translateY(0)' }
      if (hud) hud.style.opacity = '1'
      startCycle()
    }
    const boot = q('[data-el="boot"]')
    if (bootedOnce) {
      if (boot) boot.style.display = 'none'
      assembleStart = performance.now(); revealHero()
    } else {
      const log = q('[data-el="boot-log"]'); const bar = q('[data-el="boot-bar"]')
      const lines = ['POST · verifying secure enclave ........ OK', 'mounting neural core /dev/friday0 ...... OK', 'loading 1.8T parameters ................ OK', 'establishing 9 connectors .............. OK', 'calibrating voice pipeline ............. OK', 'arming guardrails · least-privilege .... OK', 'COSMOS online. awaiting operator.']
      let i = 0
      const step = () => {
        if (disposed) return
        if (log && i < lines.length) {
          const row = document.createElement('div')
          row.innerHTML = '<span style="color:#3a7386;">›</span> ' + lines[i].replace('OK', '<span style="color:#3fe0a0;">OK</span>').replace('online. awaiting operator.', '<span style="color:#00d4ff;">online.</span> awaiting operator.')
          row.style.opacity = '0'; row.style.transition = 'opacity .3s'; log.appendChild(row)
          requestAnimationFrame(() => { row.style.opacity = '1' })
          if (bar) bar.style.width = Math.round(((i + 1) / lines.length) * 100) + '%'
          i++; setTimeout(step, i === lines.length ? 550 : 300 + Math.random() * 150)
        } else {
          assembleStart = performance.now()
          setTimeout(() => { if (boot) { boot.style.opacity = '0'; setTimeout(() => { boot.style.display = 'none' }, 1000) } revealHero(); bootedOnce = true }, 500)
        }
      }
      setTimeout(step, 400)
    }

    // ── clock + telemetry ──
    const clockEl = q('[data-el="stat-clock"]'), latEl = q('[data-el="stat-latency"]'), tokEl = q('[data-el="stat-tokens"]')
    const t0 = Date.now()
    timers.push(setInterval(() => {
      if (clockEl) { const s = Math.floor((Date.now() - t0) / 1000); clockEl.textContent = [Math.floor(s / 3600), Math.floor((s % 3600) / 60), s % 60].map(x => String(x).padStart(2, '0')).join(':') }
    }, 1000))
    timers.push(setInterval(() => {
      if (latEl) latEl.textContent = String(Math.round(120 + Math.random() * 40 + cur.think * 60))
      if (tokEl) tokEl.textContent = String(Math.round(280 + Math.random() * 90 + cur.think * 120))
    }, 700))

    // ── waveform ──
    const wave = q('[data-el="wave"]')
    if (wave) {
      const bars: HTMLElement[] = []
      for (let i = 0; i < 40; i++) { const b = document.createElement('div'); b.style.cssText = 'flex:1;background:linear-gradient(180deg,#26e0ff,#0088c0);border-radius:2px;box-shadow:0 0 8px rgba(0,212,255,.4);height:10%;transition:height .12s ease;'; wave.appendChild(b); bars.push(b) }
      timers.push(setInterval(() => { bars.forEach((b, i) => { const base = Math.abs(Math.sin((Date.now() / 220) + i * 0.5)); b.style.height = (14 + base * (60 + cur.think * 30)) + '%' }) }, 90))
    }

    // ── scroll reveal (root is the scroll container) ──
    const revealEls = Array.from(root.querySelectorAll('[data-reveal]')) as HTMLElement[]
    let ci = 0
    revealEls.forEach(el => { if (el.hasAttribute('data-card')) { el.style.transitionDelay = (ci * 80) + 'ms'; ci++ } })
    const seen = new Set<Element>()
    const checkReveal = () => {
      if (disposed) return
      const vh = window.innerHeight
      revealEls.forEach(el => { if (seen.has(el)) return; const r = el.getBoundingClientRect(); if (r.top < vh * 0.9 && r.bottom > 0) { seen.add(el); el.style.opacity = '1'; el.style.transform = 'translateY(0)' } })
    }
    root.addEventListener('scroll', () => requestAnimationFrame(checkReveal), { passive: true })
    addWin('resize', (() => requestAnimationFrame(checkReveal)) as EventListener)
    checkReveal(); setTimeout(checkReveal, 400); setTimeout(checkReveal, 1000)

    // ── nav + CTA wiring ──
    root.querySelectorAll('[data-nav]').forEach(a => a.addEventListener('click', (e) => {
      e.preventDefault(); navRef.current((a as HTMLElement).dataset.nav as Page)
    }))
    const prim = q('[data-el="cta-primary"]')
    if (prim) {
      prim.addEventListener('mouseenter', () => { prim.style.transform = 'translateY(-2px)'; prim.style.boxShadow = '0 0 44px rgba(0,212,255,.8),inset 0 1px 0 rgba(255,255,255,.6)' })
      prim.addEventListener('mouseleave', () => { prim.style.transform = 'none'; prim.style.boxShadow = '0 0 30px rgba(0,212,255,.55),inset 0 1px 0 rgba(255,255,255,.55)' })
      prim.addEventListener('click', () => navRef.current('agent'))
    }
    const sec = q('[data-el="cta-secondary"]')
    if (sec) sec.addEventListener('click', () => trigger([['think', 3800]]))
    // mobile menu
    const burger = q('[data-el="burger"]'); const menu = q('[data-el="mobile-menu"]')
    if (burger && menu) {
      const spans = burger.querySelectorAll('span'); let open = false
      burger.addEventListener('click', () => {
        open = !open; menu.style.opacity = open ? '1' : '0'; menu.style.pointerEvents = open ? 'auto' : 'none'
        if (spans.length === 3) { (spans[0] as HTMLElement).style.transform = open ? 'translateY(8px) rotate(45deg)' : 'none'; (spans[1] as HTMLElement).style.opacity = open ? '0' : '1'; (spans[2] as HTMLElement).style.transform = open ? 'translateY(-8px) rotate(-45deg)' : 'none' }
      })
      menu.querySelectorAll('a').forEach(a => a.addEventListener('click', () => { open = false; menu.style.opacity = '0'; menu.style.pointerEvents = 'none' }))
    }

    initScene()

    return () => {
      disposed = true
      cancelAnimationFrame(raf)
      if (cycleTimer) clearTimeout(cycleTimer)
      timers.forEach(clearInterval)
      onWin.forEach(([ev, fn]) => window.removeEventListener(ev, fn))
      disposables.forEach(d => d.dispose())
      if (renderer) renderer.dispose()
    }
  }, [])

  return (
    <div ref={rootRef}
      style={{ position: 'fixed', inset: 0, overflowX: 'hidden', overflowY: 'auto',
        background: '#020916', color: '#dff2ff', fontFamily: 'Inter, system-ui, sans-serif' }}
      dangerouslySetInnerHTML={{ __html: HTML }} />
  )
}
