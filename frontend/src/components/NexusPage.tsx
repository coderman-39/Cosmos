import { useEffect, useRef } from 'react'
import type { Page } from '../store'

// ── NEXUS · CORTEX — the live mind-map of everything COSMOS is wired into. ──
// Ported verbatim from the "Cosmos Cortex" design export: the canvas renderer,
// camera, synapse fires, brainwave shells and detail panel are the original
// code, unchanged. The only deviation is the DATA source — instead of the
// hard-coded LOBES/MEMORY, this fetches `/api/nexus`, which the backend rebuilds
// live from the real feature stores (connectors, MCP, skills, Kinesis, schedule,
// meetings, memory). Add a skill or record a macro → a new node appears here.
//
// Everything is driven imperatively inside one effect (querying within `root`),
// mirroring HomePage's port pattern, with full teardown on unmount.

// The original DATA block — used as an offline fallback so the map still renders
// with the design's demo cortex if the backend is unreachable.
const FALLBACK = {
  core: { stats: { Tools: '52 wired', Lobes: '6 online', Uptime: '99.98%', Latency: '42 ms' } },
  lobes: [
    { id: 'connectors', cat: 'connectors', dir: [1.00, 0.16, 0.28], desc: 'External services COSMOS reaches into to act on your behalf.',
      kids: [
        ['Slack', 'online', 'Team messaging · posts, reads, DMs', '2 channels watched'],
        ['Google Workspace', 'online', 'Gmail, Calendar, Drive, Docs', 'user@corp'],
        ['GitHub', 'online', 'Repos, PRs, issues, actions', '14 repos'],
        ['Docker', 'idle', 'Containers, images, compose stacks', '7 running'],
        ['CI', 'online', 'Pipelines, checks, build artifacts', '12 workflows'],
        ['Staging', 'online', 'Deploy previews & staging env', 'sync 4m ago'],
      ] },
    { id: 'mcp', cat: 'mcp', dir: [0.46, 0.60, -0.62], desc: 'Model Context Protocol servers exposing tools to the agent.',
      kids: [
        ['GitHub MCP', 'online', 'PR + workflow tools', '18 tools'],
        ['Postgres', 'online', 'SQL over the app database', '9 schemas'],
        ['AWS MCP', 'online', 'Cloud infra + deploy control', 'prod + stg'],
        ['Grafana', 'idle', 'Dashboards & alert rules', 'scoped RO'],
        ['Terraform MCP', 'online', 'Infra plans & modules', '6 tools'],
        ['K8s', 'needs-setup', 'Cluster ops & rollouts', 'no context'],
      ] },
    { id: 'skills', cat: 'skills', dir: [-0.52, 0.42, 0.55], desc: 'Learned playbooks the agent can invoke and reason with.',
      kids: [
        ['engineering', 'online', 'Git, branching & scaffolding flows', '41 procedures'],
        ['tool-chaining', 'online', 'Maps multi-step asks to connectors', 'v2.3'],
        ['macos-control', 'online', 'Local macOS automation', 'AppleScript'],
        ['research', 'online', 'Deep multi-source research', 'synced'],
        ['self-repair', 'idle', 'Detects & fixes broken tools', '12 fixes'],
        ['orchestration', 'online', 'Multi-step agent planning', 'core skill'],
      ] },
    { id: 'kinesis', cat: 'kinesis', dir: [-1.00, -0.12, -0.24], desc: 'Kinesis macros — recorded UI action sequences the agent replays.',
      kids: [
        ['open-media-dashboard', 'online', 'Launches media browsing flow', 'last: 2d'],
        ['search-and-watch', 'online', 'Search + play a title end-to-end', '12 runs'],
        ['trigger-ci-run', 'idle', 'Kicks a CI pipeline', 'staging'],
        ['restart-staging-pod', 'online', 'Guided staging pod restart', 'audited'],
      ] },
    { id: 'schedule', cat: 'schedule', dir: [-0.46, -0.58, 0.58], desc: 'Recurring routines COSMOS runs on a cadence.',
      kids: [
        ['9am briefing', 'online', 'Daily digest of inbox + calendar', 'fires 09:00'],
        ['meeting-prep', 'online', 'Assembles context before each meeting', '−15 min'],
        ['promise-sweep', 'idle', 'Chases open commitments', 'hourly'],
      ] },
    { id: 'meetings', cat: 'meetings', dir: [0.58, -0.52, -0.50], desc: 'Upcoming meetings the agent is tracking and preparing for.',
      kids: [
        ['1:1 with Alex — 3:00pm', 'online', 'Prep brief + last action items ready', 'today'],
        ['Team standup — 10:30am', 'idle', 'Blockers + ticket rollup queued', 'today'],
      ] },
  ],
  memory: [
    ['people', 'online', 'Who matters, roles & relationships', '168 entities'],
    ['projects', 'online', 'Active initiatives & their state', '23 tracked'],
    ['preferences', 'online', 'How you like things done', '54 prefs'],
    ['corrections', 'idle', 'Things you told it to stop doing', '9 rules'],
    ['tasks', 'online', 'Open work items & follow-ups', '31 open'],
    ['facts', 'online', 'Durable knowledge about your world', '610 facts'],
  ],
}

const STYLE = `
.nexus-root{position:fixed;inset:0;overflow:hidden;background:#040a16;color:#cfefff;
  font-family:'Share Tech Mono',ui-monospace,monospace;-webkit-font-smoothing:antialiased;
  --cyan:#00e5ff;--teal:#22e0c8;--violet:#9a7bff;--amber:#ffbf47;--coral:#ff6a3d;
  --hud-bg:rgba(8,18,34,0.42);--hud-brd:rgba(0,229,255,0.22);
  --mono:'Share Tech Mono',ui-monospace,monospace;--code:'JetBrains Mono',ui-monospace,monospace;
  --display:'Orbitron',var(--mono);user-select:none;}
.nexus-root *{box-sizing:border-box;}
.nexus-root #nx-stage{position:absolute;inset:0;}
.nexus-root #nx-graph{position:absolute;inset:0;display:block;touch-action:none;cursor:grab;}
.nexus-root #nx-graph.dragging{cursor:grabbing;}
.nexus-root #nx-scanlines{position:absolute;inset:0;z-index:12;pointer-events:none;
  background:repeating-linear-gradient(180deg,rgba(140,220,255,0.028) 0 1px,transparent 1px 3px);mix-blend-mode:overlay;}
.nexus-root #nx-hexgrid{position:absolute;inset:0;z-index:11;pointer-events:none;opacity:.55;
  background-image:url("data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='56' height='100'%3E%3Cpath d='M28 66L0 50L0 16L28 0L56 16L56 50L28 66L28 100' fill='none' stroke='rgba(130,220,255,0.07)' stroke-width='1'/%3E%3Cpath d='M28 0L28 34L0 50L0 84L28 100L56 84L56 50L28 34' fill='none' stroke='rgba(130,220,255,0.07)' stroke-width='1'/%3E%3C/svg%3E");
  -webkit-mask-image:radial-gradient(circle at 50% 46%,transparent 32%,#000 78%);mask-image:radial-gradient(circle at 50% 46%,transparent 32%,#000 78%);}
.nexus-root #nx-vignette{position:absolute;inset:0;z-index:12;pointer-events:none;
  background:radial-gradient(ellipse at 50% 46%,transparent 46%,rgba(1,4,10,0.55) 100%);}
.nexus-root #nx-hud{position:absolute;top:0;left:0;right:0;height:56px;z-index:20;display:flex;align-items:center;
  justify-content:space-between;padding:0 22px;background:linear-gradient(180deg,rgba(6,14,28,0.72),rgba(6,14,28,0.10));
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--hud-brd);pointer-events:none;}
.nexus-root #nx-hud .brand{font-family:var(--display);font-weight:800;letter-spacing:.28em;font-size:15px;color:#eafaff;
  text-shadow:0 0 14px rgba(0,229,255,0.55);display:flex;align-items:center;gap:12px;}
.nexus-root #nx-hud .brand .glyph{color:var(--cyan);font-size:17px;filter:drop-shadow(0 0 8px var(--cyan));animation:nxspin 9s linear infinite;display:inline-block;}
@keyframes nxspin{to{transform:rotate(360deg)}}
.nexus-root #nx-hud .stats{display:flex;align-items:center;gap:20px;font-family:var(--code);font-size:12px;letter-spacing:.06em;color:#7fb8cc;}
.nexus-root #nx-hud .stats b{color:#eafaff;font-weight:500;}
.nexus-root #nx-hud .stats .fire b{color:var(--amber);text-shadow:0 0 10px rgba(255,191,71,.6);}
.nexus-root #nx-hud .stats .dot{width:6px;height:6px;border-radius:50%;background:var(--teal);box-shadow:0 0 8px var(--teal);display:inline-block;margin-right:6px;animation:nxblink 2.4s ease-in-out infinite;}
@keyframes nxblink{0%,100%{opacity:1}50%{opacity:.35}}
.nexus-root #nx-back{position:absolute;top:12px;left:18px;z-index:40;pointer-events:auto;cursor:pointer;
  font-family:var(--code);font-size:11px;letter-spacing:.16em;color:#9fd6ea;padding:8px 14px;border-radius:8px;
  border:1px solid var(--hud-brd);background:var(--hud-bg);backdrop-filter:blur(10px);text-transform:uppercase;}
.nexus-root #nx-back:hover{color:#eafaff;border-color:var(--cyan);}
.nexus-root #nx-legend{position:absolute;left:18px;bottom:18px;z-index:20;background:var(--hud-bg);backdrop-filter:blur(12px);
  -webkit-backdrop-filter:blur(12px);border:1px solid var(--hud-brd);border-radius:12px;padding:12px 14px;pointer-events:none;
  box-shadow:0 0 30px rgba(0,60,90,0.35), inset 0 0 22px rgba(0,120,160,0.05);}
.nexus-root #nx-legend .ttl{font-family:var(--code);font-size:9.5px;letter-spacing:.24em;color:#5f8ea0;margin-bottom:9px;}
.nexus-root #nx-legend .row{display:flex;align-items:center;gap:9px;font-size:11.5px;color:#bfe6f2;line-height:1.9;letter-spacing:.03em;}
.nexus-root #nx-legend .sw{width:9px;height:9px;border-radius:50%;flex:none;}
.nexus-root #nx-hint{position:absolute;right:18px;bottom:18px;z-index:20;font-family:var(--code);font-size:10px;letter-spacing:.08em;color:#4f7688;text-align:right;line-height:1.8;pointer-events:none;}
.nexus-root #nx-hint b{color:#84b6c8;font-weight:500;}
.nexus-root #nx-tip{position:fixed;z-index:25;pointer-events:none;transform:translate(-50%,-150%);background:rgba(6,16,30,0.82);
  backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border:1px solid rgba(0,229,255,0.3);border-radius:8px;padding:7px 11px;
  opacity:0;transition:opacity .14s ease;white-space:nowrap;box-shadow:0 6px 24px rgba(0,0,0,.5);}
.nexus-root #nx-tip.on{opacity:1;}
.nexus-root #nx-tip .n{font-size:13px;color:#eafaff;letter-spacing:.02em;}
.nexus-root #nx-tip .t{font-family:var(--code);font-size:9.5px;letter-spacing:.16em;margin-top:2px;text-transform:uppercase;}
.nexus-root #nx-panel{position:fixed;left:0;top:0;width:min(300px,86vw);max-height:min(72vh,560px);z-index:30;
  background:linear-gradient(180deg,rgba(9,20,38,0.86),rgba(6,14,28,0.93));backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border:1px solid var(--pcol,var(--cyan));border-radius:14px;box-shadow:0 18px 60px rgba(0,0,0,0.55), 0 0 34px rgba(0,180,220,0.16), inset 0 0 26px rgba(0,180,220,0.05);
  opacity:0;transform:scale(0.9);transform-origin:left center;pointer-events:none;transition:opacity .22s ease, transform .3s cubic-bezier(.22,1,.36,1);
  padding:20px;display:flex;flex-direction:column;overflow-y:auto;}
.nexus-root #nx-panel.on{opacity:1;transform:scale(1);pointer-events:auto;}
.nexus-root #nx-panel::before{content:'';position:absolute;top:0;left:0;right:34%;height:2px;background:linear-gradient(90deg,var(--pcol,var(--cyan)),transparent);box-shadow:0 0 14px var(--pcol,var(--cyan));}
.nexus-root #nx-panel .close{position:absolute;top:12px;right:12px;width:28px;height:28px;border-radius:8px;display:flex;align-items:center;justify-content:center;cursor:pointer;color:#9fd0e0;font-size:15px;border:1px solid rgba(255,255,255,0.08);background:rgba(255,255,255,0.03);transition:all .18s;}
.nexus-root #nx-panel .close:hover{color:#fff;border-color:var(--pcol,var(--cyan));background:rgba(0,180,220,0.12);}
.nexus-root #nx-panel .kicker{font-family:var(--code);font-size:10px;letter-spacing:.26em;color:var(--pcol,var(--cyan));text-transform:uppercase;margin-bottom:10px;}
.nexus-root #nx-panel h2{font-family:var(--display);font-weight:700;font-size:19px;color:#f2fbff;line-height:1.2;letter-spacing:.01em;text-shadow:0 0 20px rgba(0,200,255,0.25);word-break:break-word;padding-right:26px;margin:0;}
.nexus-root #nx-panel .badge{display:inline-flex;align-items:center;gap:7px;margin-top:12px;font-family:var(--code);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;padding:5px 11px;border-radius:20px;border:1px solid currentColor;align-self:flex-start;}
.nexus-root #nx-panel .badge .bd{width:7px;height:7px;border-radius:50%;background:currentColor;box-shadow:0 0 8px currentColor;animation:nxblink 1.8s ease-in-out infinite;}
.nexus-root #nx-panel .desc{margin-top:14px;font-size:12.5px;line-height:1.65;color:#a9cdda;letter-spacing:.01em;}
.nexus-root #nx-panel .stats{margin-top:16px;display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.nexus-root #nx-panel .stat{background:rgba(255,255,255,0.028);border:1px solid rgba(255,255,255,0.07);border-radius:10px;padding:10px 11px;}
.nexus-root #nx-panel .stat .k{font-family:var(--code);font-size:9px;letter-spacing:.18em;color:#5f8ea0;text-transform:uppercase;}
.nexus-root #nx-panel .stat .v{font-family:var(--code);font-size:14.5px;color:#eafaff;margin-top:4px;font-weight:500;}
.nexus-root #nx-panel .conn{margin-top:16px;}
.nexus-root #nx-panel .conn .lbl{font-family:var(--code);font-size:9px;letter-spacing:.18em;color:#5f8ea0;text-transform:uppercase;margin-bottom:9px;}
.nexus-root #nx-panel .chips{display:flex;flex-wrap:wrap;gap:7px;}
.nexus-root #nx-panel .chip{font-family:var(--code);font-size:10.5px;color:#bfe6f2;padding:4px 9px;border-radius:6px;background:rgba(0,180,220,0.06);border:1px solid rgba(0,180,220,0.16);}
.nexus-root #nx-panel .open{margin-top:18px;flex:none;display:flex;align-items:center;justify-content:center;gap:10px;font-family:var(--code);font-size:11.5px;letter-spacing:.18em;text-transform:uppercase;padding:12px;border-radius:11px;cursor:pointer;color:#04121f;font-weight:700;background:var(--pcol,var(--cyan));box-shadow:0 0 26px var(--pcol,var(--cyan)),inset 0 0 18px rgba(255,255,255,0.35);transition:transform .16s, box-shadow .16s;}
.nexus-root #nx-panel .open:hover{transform:translateY(-2px);box-shadow:0 4px 40px var(--pcol,var(--cyan)),inset 0 0 22px rgba(255,255,255,0.5);}
.nexus-root #nx-panel::-webkit-scrollbar{width:6px;}
.nexus-root #nx-panel::-webkit-scrollbar-thumb{background:rgba(0,180,220,0.25);border-radius:3px;}
`

const HTML = `
  <style>${STYLE}</style>
  <div id="nx-stage"><canvas id="nx-graph"></canvas></div>
  <div id="nx-scanlines"></div>
  <div id="nx-hexgrid"></div>
  <div id="nx-vignette"></div>
  <div id="nx-back" data-nav="home">◄ HOME</div>
  <div id="nx-hud">
    <div class="brand"><span class="glyph">◈</span> NEXUS · CORTEX</div>
    <div class="stats">
      <input id="nx-search" placeholder="⌕ find node…" spellcheck="false"
        style="pointer-events:auto;width:150px;background:rgba(8,18,34,0.6);border:1px solid rgba(0,229,255,0.22);border-radius:8px;color:#cfefff;font-family:'JetBrains Mono',monospace;font-size:11px;padding:6px 10px;outline:none;" />
      <span><span class="dot"></span><b id="nx-s-nodes">—</b> nodes</span>
      <span><b id="nx-s-lobes">—</b> lobes online</span>
      <span class="fire"><b id="nx-s-fire">—</b> firing</span>
    </div>
  </div>
  <div id="nx-legend">
    <div class="ttl">CORTEX MAP</div>
    <div class="row"><span class="sw" style="background:#00e5ff;box-shadow:0 0 8px #00e5ff"></span> Core agent</div>
    <div class="row"><span class="sw" style="background:#22e0c8;box-shadow:0 0 8px #22e0c8"></span> Integrations</div>
    <div class="row"><span class="sw" style="background:#9a7bff;box-shadow:0 0 8px #9a7bff"></span> Knowledge</div>
    <div class="row"><span class="sw" style="background:#ffbf47;box-shadow:0 0 8px #ffbf47"></span> Time</div>
    <div class="row"><span class="sw" style="background:#ff6a3d;box-shadow:0 0 8px #ff6a3d"></span> Action</div>
    <div class="row"><span class="sw" style="background:#43f5b0;box-shadow:0 0 8px #43f5b0"></span> Senses</div>
    <div class="row"><span class="sw" style="background:#ff5ec4;box-shadow:0 0 8px #ff5ec4"></span> People</div>
  </div>
  <div id="nx-hint"><b>drag</b> rotate &nbsp;·&nbsp; <b>scroll</b> zoom &nbsp;·&nbsp; <b>click</b> inspect &nbsp;·&nbsp; <b>dbl-click</b> reset</div>
  <div id="nx-tip"><div class="n"></div><div class="t"></div></div>
  <div id="nx-panel">
    <div class="close" id="nx-p-close">✕</div>
    <div class="kicker" id="nx-p-kicker">NODE</div>
    <h2 id="nx-p-name">—</h2>
    <div class="badge" id="nx-p-badge"><span class="bd"></span><span id="nx-p-status">ONLINE</span></div>
    <div class="desc" id="nx-p-desc"></div>
    <div class="stats" id="nx-p-stats"></div>
    <div class="conn"><div class="lbl">Linked to</div><div class="chips" id="nx-p-chips"></div></div>
    <div class="open" id="nx-p-open">OPEN <span>▸</span></div>
  </div>
`

export default function NexusPage({ onNavigate }: { page?: Page; onNavigate?: (p: Page) => void }) {
  const rootRef = useRef<HTMLDivElement>(null)
  const navRef = useRef(onNavigate)
  navRef.current = onNavigate

  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    // Scoped element lookup — the ported script used document.getElementById; here
    // everything is prefixed nx- and resolved within the mounted subtree.
    const $ = (id: string) => root.querySelector('#nx-' + id) as HTMLElement | null

    let disposed = false
    const intervals: ReturnType<typeof setInterval>[] = []
    const winListeners: [string, EventListener][] = []
    const addWin = (ev: string, fn: EventListener) => { window.addEventListener(ev, fn); winListeners.push([ev, fn]) }

    root.querySelectorAll('[data-nav]').forEach(el =>
      el.addEventListener('click', () => navRef.current?.((el as HTMLElement).dataset.nav as Page)))

    const run = (NX: typeof FALLBACK) => {
      if (disposed) return
      const RM = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches

      const COL = { core: '#00e5ff', teal: '#22e0c8', violet: '#9a7bff', amber: '#ffbf47',
        coral: '#ff6a3d', green: '#43f5b0', pink: '#ff5ec4' }
      const RED = '#ff3b3b', WHT = '#ffffff'
      const CAT: Record<string, any> = {
        core: { c: COL.core, super: 'Core agent', label: 'Core' },
        connectors: { c: COL.teal, super: 'Integrations', label: 'Connectors' },
        mcp: { c: COL.teal, super: 'Integrations', label: 'MCP Servers' },
        skills: { c: COL.violet, super: 'Knowledge', label: 'Skills' },
        memory: { c: COL.violet, super: 'Knowledge', label: 'Memory' },
        kinesis: { c: COL.coral, super: 'Action', label: 'Kinesis Macros' },
        schedule: { c: COL.amber, super: 'Time', label: 'Schedule' },
        meetings: { c: COL.amber, super: 'Time', label: 'Meetings' },
        watchers: { c: COL.green, super: 'Senses', label: 'Watchers' },
        dossier: { c: COL.pink, super: 'People', label: 'Dossier' },
      }
      // Node category → the app page that manages it (panel OPEN button).
      const CAT_PAGE: Record<string, Page> = {
        connectors: 'mcps', mcp: 'mcps', skills: 'skills', kinesis: 'kinesis',
        watchers: 'vision', dossier: 'dossier', schedule: 'agent',
        meetings: 'agent', memory: 'agent', core: 'agent',
      }

      let _s = 20260711
      function rnd() { _s = (_s * 1664525 + 1013904223) & 0x7fffffff; return _s / 0x7fffffff }

      const LOBES = NX.lobes
      const MEMORY = NX.memory
      const CORE_STATS = NX.core?.stats || FALLBACK.core.stats
      const AGO = ['just now', '2m ago', '14m ago', '1h ago', '3h ago', 'yesterday', '2d ago']
      const pick = (a: any[]) => a[Math.floor(rnd() * a.length)]

      const nodes: any[] = [], edges: any[] = [], byId: Record<string, any> = {}
      function addNode(o: any) { o.hoverT = 0; o.flare = 0; o.pulse = rnd() * Math.PI * 2; o.links = o.links || []; nodes.push(o); byId[o.id] = o; return o }
      function addEdge(aId: string, bId: string, w: number, kind: string) {
        const e: any = { a: aId, b: bId, w: w, curv: (rnd() - 0.5) * (kind === 'trunk' ? 0.30 : 0.38), kind: kind, shim: rnd() * Math.PI * 2, parts: [] }
        const n = kind === 'trunk' ? 4 : kind === 'mesh' ? 1 : kind === 'callosum' ? 2 : 3
        for (let i = 0; i < n; i++) e.parts.push({ t: rnd(), sp: (0.10 + rnd() * 0.16) * (kind === 'trunk' ? 1.5 : 1), sz: 1.6 + rnd() * 2.2 })
        edges.push(e); byId[aId].links.push(bId); byId[bId].links.push(aId); return e
      }
      function norm3(v: number[], len: number) { const m = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / m * len, v[1] / m * len, v[2] / m * len] }

      const R_HUB = 300, R_KID = 96, R_MEM = 132

      addNode({ id: 'core', label: 'COSMOS', cat: 'core', kind: 'Central agent', status: 'online',
        desc: 'The orchestrating intelligence. COSMOS routes intent across every connector, skill and macro — planning, calling tools, and firing synapses out to the cortex in real time.',
        pos: [0, 0, 0], r: 30, weight: 1, stats: CORE_STATS })

      LOBES.forEach((lb: any) => {
        const hubPos = norm3(lb.dir, R_HUB)
        const kidsArr = lb.kids || []
        const hub = addNode({
          id: 'hub_' + lb.id, label: CAT[lb.cat].label, cat: lb.cat, kind: 'Lobe · ' + kidsArr.length + ' nodes',
          status: 'online', desc: lb.desc, pos: hubPos, r: 14, weight: 0.92, isHub: true,
          stats: { Nodes: String(kidsArr.length), Category: CAT[lb.cat].super, 'Last fire': pick(AGO), Load: (20 + Math.floor(rnd() * 70)) + '%' },
        })
        addEdge('core', hub.id, 1.0, 'trunk')
        const kidNodes: any[] = []
        kidsArr.forEach((k: any[], ki: number) => {
          const ang = (ki / Math.max(1, kidsArr.length)) * Math.PI * 2 + rnd() * 0.6
          const d = norm3(lb.dir, 1)
          let up = [0, 1, 0]; if (Math.abs(d[1]) > 0.9) up = [1, 0, 0]
          const rt = norm3([d[1] * up[2] - d[2] * up[1], d[2] * up[0] - d[0] * up[2], d[0] * up[1] - d[1] * up[0]], 1)
          const ub = [d[1] * rt[2] - d[2] * rt[1], d[2] * rt[0] - d[0] * rt[2], d[0] * rt[1] - d[1] * rt[0]]
          const rr = R_KID * (0.7 + rnd() * 0.7)
          const ox = Math.cos(ang) * rr, oy = Math.sin(ang) * rr
          const out = R_KID * 0.45 * (0.4 + rnd())
          const pos = [
            hubPos[0] + rt[0] * ox + ub[0] * oy + d[0] * out,
            hubPos[1] + rt[1] * ox + ub[1] * oy + d[1] * out,
            hubPos[2] + rt[2] * ox + ub[2] * oy + d[2] * out,
          ]
          const kn = addNode({
            id: lb.id + '_' + ki, label: k[0], cat: lb.cat, kind: CAT[lb.cat].label.replace(/s$/, ''),
            status: k[1], desc: k[2], pos: pos, r: 7, weight: 0.35 + rnd() * 0.6,
            stats: { Status: k[1], Detail: k[3], Calls: String(Math.floor(rnd() * 400)), 'Last used': pick(AGO) },
          })
          addEdge(hub.id, kn.id, 0.4 + rnd() * 0.55, 'branch')
          kidNodes.push(kn)
        })
        for (let i = 0; i < kidNodes.length; i++) {
          addEdge(kidNodes[i].id, kidNodes[(i + 1) % kidNodes.length].id, 0.16 + rnd() * 0.18, 'mesh')
          if (rnd() > 0.55 && kidNodes.length > 3) addEdge(kidNodes[i].id, kidNodes[(i + 2) % kidNodes.length].id, 0.12 + rnd() * 0.14, 'mesh')
        }
      })

      MEMORY.forEach((m: any[], mi: number) => {
        const a = (mi / Math.max(1, MEMORY.length)) * Math.PI * 2
        const tilt = (mi % 2 ? 1 : -1) * 0.5
        const pos = [Math.cos(a) * R_MEM, Math.sin(a) * R_MEM * 0.5 + tilt * 40, Math.sin(a) * R_MEM * 0.72 * (mi % 2 ? 1 : -1)]
        addNode({
          id: 'mem_' + mi, label: m[0], cat: 'memory', kind: 'Memory shell', status: m[1], desc: m[2],
          pos: pos, r: 5.6, weight: 0.3 + rnd() * 0.4, isMem: true,
          stats: { Store: m[3], Recall: pick(AGO), Confidence: (78 + Math.floor(rnd() * 20)) + '%', Status: m[1] },
        })
        addEdge('core', 'mem_' + mi, 0.5 + rnd() * 0.4, 'mem')
      })
      const memNodes = nodes.filter(n => n.isMem)
      for (let i = 0; i < memNodes.length; i++) addEdge(memNodes[i].id, memNodes[(i + 1) % memNodes.length].id, 0.12, 'mesh')

      function d3(a: any, b: any) { return Math.hypot(a.pos[0] - b.pos[0], a.pos[1] - b.pos[1], a.pos[2] - b.pos[2]) }
      const hubs = nodes.filter(n => n.isHub)
      const seenH: Record<string, number> = {}
      hubs.forEach(h => {
        hubs.filter(o => o !== h).sort((a, b) => d3(h, a) - d3(h, b)).slice(0, 2).forEach(o => {
          const key = [h.id, o.id].sort().join('|')
          if (seenH[key]) return; seenH[key] = 1
          addEdge(h.id, o.id, 0.5, 'callosum')
          let best = null as any, bd = 1e9
          const kidsA = nodes.filter(n => n.cat === h.cat && !n.isHub && !n.isMem)
          const kidsB = nodes.filter(n => n.cat === o.cat && !n.isHub && !n.isMem)
          kidsA.forEach(a => kidsB.forEach(b => { const d = d3(a, b); if (d < bd) { bd = d; best = [a, b] } }))
          if (best) addEdge(best[0].id, best[1].id, 0.14, 'mesh')
        })
      })

      nodes.forEach(n => { n.dist = Math.hypot(n.pos[0], n.pos[1], n.pos[2]); n.wb = 0 })

      const cloud: any[] = []
      for (let i = 0; i < 620; i++) {
        const r = (0.4 + Math.pow(rnd(), 0.6) * 1.4) * 55
        const ph = Math.acos(2 * rnd() - 1)
        cloud.push({ r: r, th: rnd() * Math.PI * 2, sph: Math.sin(ph), cph: Math.cos(ph), sz: 1.1 + rnd() * 2.1, wht: rnd() > 0.9, dir: rnd() > 0.25 ? 1 : -1 })
      }
      const bands: any[] = []
      ;[[68, 0.10 * Math.PI, 0, 0.42], [84, 0.35 * Math.PI, 0.2 * Math.PI, -0.30], [97, 0.55 * Math.PI, 0.4 * Math.PI, 0.21]].forEach(v => {
        const pts: any[] = []
        for (let i = 0; i < 150; i++) pts.push({ a: i / 150 * Math.PI * 2 + rnd() * 0.25, j: (rnd() - 0.5) * 13, y: (rnd() - 0.5) * 10, sz: 1.4 + rnd() * 1.5 })
        bands.push({ r: v[0], sp: v[3], pts: pts, c1: Math.cos(v[1]), s1: Math.sin(v[1]), c2: Math.cos(v[2]), s2: Math.sin(v[2]) })
      })

      const edgeKey: Record<string, number> = {}; edges.forEach((e, i) => { edgeKey[e.a + '|' + e.b] = i; edgeKey[e.b + '|' + e.a] = i })
      function edgeBetween(a: string, b: string) { return edgeKey[a + '|' + b] }
      nodes.forEach(n => {
        if (n.id === 'core') { n.firePath = null; return }
        let chain
        if (n.isMem || n.isHub) chain = [edgeBetween('core', n.id)]
        else { const hub = 'hub_' + n.cat; chain = [edgeBetween('core', hub), edgeBetween(hub, n.id)] }
        n.firePath = chain.every(x => x != null) ? chain : null
      })
      const fireable = nodes.filter(n => n.firePath && n.status !== 'needs-setup')

      edges.forEach(e => {
        if (e.kind === 'trunk' || e.kind === 'mem') { e.i0 = 1.25 + rnd() * 0.35; e.idur = 0.6 }
        else if (e.kind === 'branch') { e.i0 = 2.05 + rnd() * 0.6; e.idur = 0.45 }
        else { e.i0 = 2.85 + rnd() * 0.6; e.idur = 0.55 }
      })
      nodes.forEach(n => { n.birth = RM ? 1 : 0; n.introAt = null })
      byId.core.introAt = 0
      edges.forEach(e => {
        const t = e.i0 + e.idur
        if (e.kind === 'trunk' || e.kind === 'mem' || e.kind === 'branch') {
          const nb = byId[e.b]
          if (nb.id !== 'core' && (nb.introAt == null || t < nb.introAt)) nb.introAt = t
        }
      })
      nodes.forEach(n => { if (n.introAt == null) n.introAt = 2.6 })

      const canvas = $('graph') as HTMLCanvasElement
      const ctx = canvas.getContext('2d')!
      let W = 0, H = 0, DPR = 1, cx = 0, cy = 0, fit = 1
      function resize() {
        DPR = Math.min(window.devicePixelRatio || 1, 2)
        W = root!.clientWidth || window.innerWidth; H = root!.clientHeight || window.innerHeight
        canvas.width = W * DPR; canvas.height = H * DPR
        canvas.style.width = W + 'px'; canvas.style.height = H + 'px'
        ctx.setTransform(DPR, 0, 0, DPR, 0, 0)
        cx = W / 2; cy = H / 2 + 8
        fit = Math.min(W, H) / 840
      }
      addWin('resize', resize as EventListener); resize()

      const cam = { yaw: 0.5, pitch: -0.30, zoom: RM ? 1 : 1.55, targetZoom: 1, panX: 0, panY: 0 }
      let autoYaw = 0
      const FOCAL = 920
      const ROT = { cy: 1, sy: 0, cp: 1, sp: 0 }
      function updateRot() {
        ROT.cy = Math.cos(cam.yaw + autoYaw); ROT.sy = Math.sin(cam.yaw + autoYaw)
        ROT.cp = Math.cos(cam.pitch); ROT.sp = Math.sin(cam.pitch)
      }
      function project(px: number, py: number, pz: number) {
        let x = px * ROT.cy + pz * ROT.sy
        let z = -px * ROT.sy + pz * ROT.cy
        let y = py * ROT.cp - z * ROT.sp
        z = py * ROT.sp + z * ROT.cp
        const persp = FOCAL / Math.max(220, FOCAL - z)
        return { x: cx + x * persp * cam.zoom * fit + cam.panX, y: cy + y * persp * cam.zoom * fit + cam.panY, z: z, s: persp * cam.zoom }
      }

      function hex2rgb(h: string) { return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)] }
      const _rgbC: Record<string, number[]> = {}
      function rgba(h: string, a: number) { const c = _rgbC[h] || (_rgbC[h] = hex2rgb(h)); return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + a + ')' }
      function makeSprite(color: string, hot: boolean) {
        const s = document.createElement('canvas'); s.width = s.height = 128
        const c = s.getContext('2d')!
        const g = c.createRadialGradient(64, 64, 0, 64, 64, 64)
        if (hot) {
          g.addColorStop(0, 'rgba(255,255,255,1)'); g.addColorStop(0.14, 'rgba(255,255,255,0.85)')
          g.addColorStop(0.30, rgba(color, 0.65)); g.addColorStop(0.55, rgba(color, 0.16)); g.addColorStop(1, rgba(color, 0))
        } else {
          g.addColorStop(0, rgba(color, 0.55)); g.addColorStop(0.45, rgba(color, 0.18)); g.addColorStop(1, rgba(color, 0))
        }
        c.fillStyle = g; c.fillRect(0, 0, 128, 128); return s
      }
      const HOT: Record<string, HTMLCanvasElement> = {}, HALO: Record<string, HTMLCanvasElement> = {}
      ;[COL.core, COL.teal, COL.violet, COL.amber, COL.coral, COL.green, COL.pink, RED, WHT]
        .forEach(c => { HOT[c] = makeSprite(c, true); HALO[c] = makeSprite(c, false) })
      function spr(map: any, x: number, y: number, r: number, color: string, a: number) {
        if (!isFinite(x) || !isFinite(y) || !isFinite(r) || r <= 0 || a <= 0) return
        ctx.globalAlpha = Math.min(1, a); ctx.drawImage(map[color] || map[WHT], x - r, y - r, r * 2, r * 2); ctx.globalAlpha = 1
      }

      const stars: any[] = []
      for (let i = 0; i < 240; i++) stars.push({ x: rnd(), y: rnd(), z: rnd() * 0.8 + 0.2, r: rnd() * 1.3 + 0.3, tw: rnd() * Math.PI * 2, sp: 0.4 + rnd() })
      const NEB = [
        { x: 0.30, y: 0.36, r: 0.5, c: COL.teal, a: 0.045 }, { x: 0.72, y: 0.30, r: 0.55, c: COL.violet, a: 0.045 },
        { x: 0.60, y: 0.74, r: 0.6, c: COL.core, a: 0.04 }, { x: 0.20, y: 0.72, r: 0.5, c: COL.coral, a: 0.03 },
      ]
      const dust: any[] = []
      for (let i = 0; i < 160; i++) {
        const r = 150 + rnd() * 420, th = rnd() * Math.PI * 2, ph = Math.acos(2 * rnd() - 1)
        dust.push({ r: r, th: th, ph: ph, sp: (rnd() - 0.5) * 0.05, sz: 0.8 + rnd() * 1.8, c: [COL.core, COL.teal, COL.violet, COL.amber][Math.floor(rnd() * 4)], tw: rnd() * Math.PI * 2, sharp: rnd() > 0.45 })
      }
      function drawBackground(t: number) {
        const bg = ctx.createRadialGradient(cx, cy * 0.9, 50, cx, cy, Math.max(W, H) * 0.85)
        bg.addColorStop(0, '#081527'); bg.addColorStop(0.5, '#050d1c'); bg.addColorStop(1, '#02060f')
        ctx.fillStyle = bg; ctx.fillRect(0, 0, W, H)
        ctx.globalCompositeOperation = 'lighter'
        const drift = RM ? 0 : t * 0.00003
        NEB.forEach((n, i) => {
          const px = (n.x + Math.sin(drift + i) * 0.02) * W, py = (n.y + Math.cos(drift * 1.3 + i) * 0.02) * H
          spr(HALO, px, py, n.r * Math.max(W, H) * 0.7, n.c, n.a * 3)
        })
        const yawOff = (cam.yaw + autoYaw) * 0.03
        for (const s of stars) {
          let px = ((s.x + yawOff * s.z) % 1 + 1) % 1 * W, py = s.y * H
          let a = 0.5 + 0.5 * Math.sin(RM ? s.tw : t * 0.001 * s.sp + s.tw)
          a = 0.10 + a * 0.55 * s.z
          ctx.fillStyle = 'rgba(180,225,255,' + a.toFixed(3) + ')'
          ctx.beginPath(); ctx.arc(px, py, s.r * s.z, 0, 7); ctx.fill()
        }
        const dtw = RM ? 0 : t * 0.001
        for (const d of dust) {
          const th = d.th + dtw * d.sp
          const px3 = d.r * Math.sin(d.ph) * Math.cos(th), py3 = d.r * Math.cos(d.ph) * 0.8, pz3 = d.r * Math.sin(d.ph) * Math.sin(th)
          const p = project(px3, py3, pz3)
          const a = (0.05 + 0.09 * (0.5 + 0.5 * Math.sin(dtw * 1.4 + d.tw))) * Math.min(1.4, p.s)
          if (d.sharp) spr(HOT, p.x, p.y, d.sz * 2.2 * p.s, d.c, a * 4)
          else spr(HALO, p.x, p.y, d.sz * 3.2 * p.s, d.c, a * 3)
        }
        ctx.globalCompositeOperation = 'source-over'
      }

      function edgeCtrl(A: any, B: any, curv: number) {
        const mx = (A.sx + B.sx) / 2, my = (A.sy + B.sy) / 2
        const dx = B.sx - A.sx, dy = B.sy - A.sy
        return { x: mx - dy * curv, y: my + dx * curv }
      }
      function qx(A: any, C: any, B: any, t: number) { const u = 1 - t; return u * u * A.sx + 2 * u * t * C.x + t * t * B.sx }
      function qy(A: any, C: any, B: any, t: number) { const u = 1 - t; return u * u * A.sy + 2 * u * t * C.y + t * t * B.sy }
      function eob(x: number) { const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.pow(x - 1, 3) + c1 * Math.pow(x - 1, 2) }

      const fires: any[] = [], ripples: any[] = [], waves: any[] = []
      const WMAX = 560
      function spawnFire() {
        if (RM || !fireable.length) return
        if (startT == null || (performance.now() - startT) / 1000 < 3.6) return
        const n = fireable[Math.floor(Math.random() * fireable.length)]
        if (!n.firePath) return
        fires.push({ node: n, path: n.firePath.slice(), seg: 0, t: 0, speed: 1.0 + Math.random() * 0.9, color: CAT[n.cat].c })
      }
      function ripple(n: any, big: boolean) {
        ripples.push({ n: n, r: n.r * n.ss * 1.6, max: (big ? 120 : 55) * n.ss, a: big ? 0.85 : 0.5, c: CAT[n.cat].c, w: big ? 2.2 : 1.4 })
      }
      if (!RM) {
        intervals.push(setInterval(() => { if (fires.length < 12 && document.visibilityState === 'visible') { spawnFire(); if (Math.random() > 0.55) spawnFire() } }, 340))
        intervals.push(setInterval(() => { if (document.visibilityState === 'visible' && waves.length < 3) waves.push({ r: 0 }) }, 3400))
      }

      let hoverNode: any = null, selNode: any = null, dimAmt = 0, prevZoom = 1, returnPan = false
      let last = performance.now(), startT: number | null = null, introWaved = false
      const hudFire = $('s-fire')!

      const panel = $('panel')!
      const STATUS_TXT: Record<string, string> = { online: 'ONLINE', idle: 'IDLE', 'needs-setup': 'NEEDS SETUP' }
      const STATUS_COL: Record<string, string> = { online: '#43f5b0', idle: '#7f9cad', 'needs-setup': '#ff5a5a' }
      function selectNode(n: any) {
        if (!selNode) prevZoom = cam.targetZoom
        selNode = n
        cam.targetZoom = Math.max(prevZoom, 1.45)
        returnPan = false
        const c = CAT[n.cat].c
        panel.style.setProperty('--pcol', c)
        $('p-kicker')!.textContent = CAT[n.cat].label
        $('p-name')!.textContent = n.label
        const badge = $('p-badge')!
        badge.style.color = STATUS_COL[n.status] || '#43f5b0'
        $('p-status')!.textContent = STATUS_TXT[n.status] || 'ONLINE'
        $('p-desc')!.textContent = n.desc
        const sw = $('p-stats')!; sw.innerHTML = ''
        Object.entries(n.stats).forEach(([k, v]) => {
          const d = document.createElement('div'); d.className = 'stat'
          const kEl = document.createElement('div'); kEl.className = 'k'; kEl.textContent = String(k)
          const vEl = document.createElement('div'); vEl.className = 'v'; vEl.textContent = String(v)
          d.appendChild(kEl); d.appendChild(vEl)
          sw.appendChild(d)
        })
        const chips = $('p-chips')!; chips.innerHTML = ''
        ;[...new Set(n.links as string[])].slice(0, 8).forEach(id => {
          const ln = byId[id]; if (!ln) return
          const s = document.createElement('span'); s.className = 'chip'
          s.textContent = ln.label.length > 18 ? ln.label.slice(0, 17) + '…' : ln.label
          s.style.borderColor = rgba(CAT[ln.cat].c, 0.28)
          chips.appendChild(s)
        })
        panel.classList.add('on')
      }
      function deselect() {
        if (selNode) { cam.targetZoom = prevZoom; returnPan = true }
        selNode = null; panel.classList.remove('on')
      }

      let raf = 0
      function frame(now: number) {
        if (disposed) return
        if (startT == null) startT = now
        const introT = RM ? 99 : (now - startT) / 1000
        const dt = Math.min(0.05, (now - last) / 1000); last = now
        const T = now * 0.001
        if (!RM) autoYaw += dt * 0.05
        cam.zoom += (cam.targetZoom - cam.zoom) * Math.min(1, dt * 8)
        if (selNode && isFinite(selNode.sx)) {
          const tx = cx - (W > 760 ? 110 : 0), ty = cy + 6
          cam.panX += (tx - selNode.sx) * Math.min(1, dt * 3.5)
          cam.panY += (ty - selNode.sy) * Math.min(1, dt * 3.5)
        } else if (returnPan) {
          const k = 1 - Math.min(1, dt * 4)
          cam.panX *= k; cam.panY *= k
          if (Math.abs(cam.panX) + Math.abs(cam.panY) < 1) { cam.panX = cam.panY = 0; returnPan = false }
        }
        updateRot()
        if (!RM && !introWaved && introT > 1.15) { waves.push({ r: 0 }); introWaved = true }
        drawBackground(now)

        const bob = RM ? 0 : 1
        for (const n of nodes) {
          const wob = n.id === 'core' ? 0 : 4.5 * bob
          const p = project(
            n.pos[0] + Math.sin(T * 0.5 + n.pulse) * wob,
            n.pos[1] + Math.sin(T * 0.62 + n.pulse * 1.7) * wob,
            n.pos[2] + Math.cos(T * 0.55 + n.pulse) * wob)
          n.sx = p.x; n.sy = p.y; n.sz = p.z; n.ss = p.s
          if (introT > n.introAt) n.birth = Math.min(1, n.birth + dt * (n.id === 'core' ? 0.85 : 3.2))
          const target = (n === hoverNode || n === selNode) ? 1 : 0
          n.hoverT += (target - n.hoverT) * Math.min(1, dt * 12)
          n.flare *= (1 - Math.min(1, dt * 2.2))
        }
        for (let i = waves.length - 1; i >= 0; i--) { waves[i].r += dt * 230; if (waves[i].r > WMAX) waves.splice(i, 1) }
        for (const n of nodes) {
          let b = 0
          for (const w of waves) { const d = (n.dist - w.r) / 42; b += Math.exp(-d * d) * (1 - w.r / WMAX) }
          n.wb = Math.min(1.2, b)
        }

        const focus = selNode || hoverNode
        const expo = Math.pow(1 / cam.zoom, 0.65)
        dimAmt += ((focus ? 1 : 0) - dimAmt) * Math.min(1, dt * 8)
        let related: Set<string> | null = null
        if (focus) { related = new Set(focus.links); related.add(focus.id) }

        ctx.globalCompositeOperation = 'lighter'
        ctx.lineCap = 'round'
        for (const n of nodes) {
          if (!n.isHub) continue
          const em2 = focus ? (related!.has(n.id) ? 1 : (1 - dimAmt * 0.6)) : 1
          spr(HALO, n.sx, n.sy, R_KID * 2.1 * n.ss * fit, CAT[n.cat].c, 0.16 * em2 * (1 + n.wb) * expo * n.birth)
        }
        for (const e of edges) {
          const A = byId[e.a], B = byId[e.b]
          const C = edgeCtrl(A, B, e.curv)
          const gw = RM ? 1 : Math.max(0, Math.min(1, (introT - e.i0) / e.idur))
          if (gw <= 0) continue
          if (gw < 1) {
            const cT = CAT[B.cat].c
            ctx.strokeStyle = rgba(cT, 0.55 * Math.min(1, gw * 1.4))
            ctx.lineWidth = Math.max(0.6, (e.kind === 'trunk' ? 2.4 : 1.2) * ((A.ss + B.ss) / 2))
            ctx.beginPath(); ctx.moveTo(A.sx, A.sy)
            for (let k = 1; k <= 14; k++) { const tt = gw * k / 14; ctx.lineTo(qx(A, C, B, tt), qy(A, C, B, tt)) }
            ctx.stroke()
            const tx = qx(A, C, B, gw), ty = qy(A, C, B, gw)
            spr(HOT, tx, ty, 7 * B.ss, WHT, 0.85); spr(HALO, tx, ty, 16 * B.ss, cT, 0.55)
            continue
          }
          const cA = CAT[A.cat].c, cB = CAT[B.cat].c
          const onFocus = focus && related!.has(e.a) && related!.has(e.b)
          let em = focus ? (onFocus ? 1.5 : (1 - dimAmt * 0.85)) : 1
          const shim = RM ? 1 : (0.75 + 0.25 * Math.sin(now * 0.0016 * (0.6 + e.w) + e.shim))
          const depth = Math.max(0.35, Math.min(1, (A.ss + B.ss) / 2 * 0.9))
          const lw = (e.kind === 'trunk' ? 2.4 : e.kind === 'mesh' ? 0.8 : 1.4) * ((A.ss + B.ss) / 2)
          const g = ctx.createLinearGradient(A.sx, A.sy, B.sx, B.sy)
          let base = (e.kind === 'trunk' ? 0.34 : e.kind === 'mesh' ? 0.10 : e.kind === 'callosum' ? 0.13 : 0.22) * (0.5 + e.w * 0.7) * shim * em * depth
          base *= 1 + (A.wb + B.wb) * 0.9
          g.addColorStop(0, rgba(cA, Math.min(0.85, base)))
          g.addColorStop(0.5, rgba(cB, Math.min(0.85, base * 1.25)))
          g.addColorStop(1, rgba(cB, Math.min(0.85, base)))
          ctx.strokeStyle = g; ctx.lineWidth = Math.max(0.5, lw)
          ctx.beginPath(); ctx.moveTo(A.sx, A.sy); ctx.quadraticCurveTo(C.x, C.y, B.sx, B.sy); ctx.stroke()
          if (e.kind === 'trunk') {
            ctx.strokeStyle = rgba(cB, Math.min(0.35, base * 0.5)); ctx.lineWidth = Math.max(1, lw * 3.4)
            ctx.beginPath(); ctx.moveTo(A.sx, A.sy); ctx.quadraticCurveTo(C.x, C.y, B.sx, B.sy); ctx.stroke()
          }
          if (e.kind !== 'mesh') {
            ctx.setLineDash([3, 22])
            ctx.lineDashOffset = RM ? 0 : -(now * 0.05 * (0.8 + e.w) + e.shim * 30)
            ctx.strokeStyle = rgba(cB, Math.min(0.9, base * 2.6)); ctx.lineWidth = Math.max(0.6, lw * 0.85)
            ctx.beginPath(); ctx.moveTo(A.sx, A.sy); ctx.quadraticCurveTo(C.x, C.y, B.sx, B.sy); ctx.stroke()
            ctx.setLineDash([])
          }
          const spd = onFocus ? 2.4 : 1
          for (const fp of e.parts) {
            if (!RM) fp.t = (fp.t + dt * fp.sp * spd) % 1
            const px = qx(A, C, B, fp.t), py = qy(A, C, B, fp.t)
            const pa = (e.kind === 'mesh' ? 0.35 : 0.65) * em * depth * (0.6 + 0.4 * Math.sin(fp.t * Math.PI))
            spr(HOT, px, py, fp.sz * 2.2 * ((A.ss + B.ss) / 2), cB, pa)
          }
        }

        for (const w of waves) {
          const wa = 0.13 * (1 - w.r / WMAX)
          if (wa <= 0.01) continue
          ctx.strokeStyle = rgba(COL.core, wa); ctx.lineWidth = 1.4
          ctx.beginPath()
          for (let k = 0; k <= 56; k++) {
            const a = k / 56 * Math.PI * 2
            const p = project(Math.cos(a) * w.r, Math.sin(a * 3) * 14, Math.sin(a) * w.r)
            k === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)
          }
          ctx.stroke()
        }

        for (let i = fires.length - 1; i >= 0; i--) {
          const f = fires[i]
          f.t += dt * f.speed * 1.5
          if (f.t >= 1) { f.seg++; f.t = 0; if (f.seg >= f.path.length) { f.node.flare = 1; ripple(f.node, false); fires.splice(i, 1); continue } }
          const e = edges[f.path[f.seg]]
          let A = byId[e.a], B = byId[e.b]
          if (e.b === 'core') { const tmp = A; A = B; B = tmp }
          else if (f.seg === 1 && e.b === 'hub_' + f.node.cat) { const tmp = A; A = B; B = tmp }
          const C = edgeCtrl(A, B, e.curv)
          for (let k = 7; k >= 0; k--) {
            const tt = Math.max(0, f.t - k * 0.035)
            const px = qx(A, C, B, tt), py = qy(A, C, B, tt)
            spr(HOT, px, py, (10 - k) * 1.6 * B.ss, f.color, 0.24 * (1 - k / 8))
          }
          const hx = qx(A, C, B, f.t), hy = qy(A, C, B, f.t)
          spr(HOT, hx, hy, 9 * B.ss, WHT, 0.95); spr(HALO, hx, hy, 26 * B.ss, f.color, 0.8)
        }

        const breathe = RM ? 1 : (1 + 0.04 * Math.sin(now * 0.0012))
        const core = byId.core
        const coreB = core.birth
        const conv = 2.8 - 1.8 * (1 - Math.pow(1 - coreB, 3))
        let coreEm = focus ? (related!.has('core') ? 1 : (1 - dimAmt * 0.6)) : 1
        const cAlpha = 0.62 * coreEm * expo * coreB
        const bandA = Math.max(0, Math.min(1, (coreB - 0.45) * 2))
        for (const s of cloud) {
          const th = RM ? s.th : s.th + T * 0.14 * s.dir
          const sr = s.r * breathe * conv
          const p = project(sr * s.sph * Math.cos(th), sr * s.cph * 0.9, sr * s.sph * Math.sin(th))
          spr(HOT, p.x, p.y, s.sz * p.s, s.wht ? WHT : COL.core, cAlpha * Math.min(1.25, p.s))
        }
        for (const b of bands) {
          const off = RM ? 0 : T * b.sp
          for (const pt of b.pts) {
            const a = pt.a + off
            const rr = (b.r + pt.j) * breathe
            let x = rr * Math.cos(a), y = pt.y * breathe, z = rr * Math.sin(a)
            const y2 = y * b.c1 - z * b.s1, z2 = y * b.s1 + z * b.c1
            const x3 = x * b.c2 - y2 * b.s2, y3 = x * b.s2 + y2 * b.c2
            const p = project(x3, y3, z2)
            spr(HOT, p.x, p.y, pt.sz * p.s, COL.core, 0.8 * coreEm * expo * bandA * Math.min(1.25, p.s))
          }
        }

        const order = nodes.slice().sort((a, b) => a.sz - b.sz)
        for (const n of order) {
          const c = CAT[n.cat].c
          const isCore = n.id === 'core'
          let em = 1
          if (focus) em = related!.has(n.id) ? 1 : (1 - dimAmt * 0.72)
          if (n.birth <= 0.01) continue
          em *= Math.min(1, n.birth * 1.4)
          const bs = eob(n.birth)
          const hv = n.hoverT, fl = n.flare + n.wb * 0.7
          const rad = n.r * n.ss * (isCore ? breathe : 1) * (1 + hv * 0.5 + fl * 0.55) * bs
          if (n.status === 'needs-setup') {
            const wpulse = RM ? 0.8 : (0.6 + 0.4 * Math.sin(now * 0.004 + n.pulse))
            spr(HALO, n.sx, n.sy, rad * 5, RED, 0.5 * em * wpulse)
            ctx.strokeStyle = rgba(RED, 0.55 * em * wpulse); ctx.lineWidth = 1.2
            ctx.beginPath(); ctx.arc(n.sx, n.sy, rad * 2.6, 0, 7); ctx.stroke()
          }
          if (isCore) {
            spr(HALO, n.sx, n.sy, rad * 6.0, c, 0.8 * em * expo * (1 + hv * 0.5))
            spr(HOT, n.sx, n.sy, rad * 1.9, c, 0.9 * em)
            spr(HOT, n.sx, n.sy, rad * 0.85, WHT, 0.95)
            continue
          }
          const st = n.status === 'idle' ? 0.55 : 1
          spr(HALO, n.sx, n.sy, rad * (n.isHub ? 5.2 : 4.4), c, (n.isHub ? 0.75 : 0.55) * em * st * expo * (1 + hv * 1.1 + fl * 1.2))
          spr(HOT, n.sx, n.sy, rad * (n.isHub ? 2.0 : 1.9), c, (0.95) * em * st * (1 + fl * 0.5))
          if (n.isHub) {
            const rot = (RM ? 0 : now * 0.0008) + n.pulse
            ctx.strokeStyle = rgba(c, 0.7 * em); ctx.lineWidth = 1.6
            ctx.beginPath(); ctx.arc(n.sx, n.sy, rad * 1.85, rot, rot + 1.5); ctx.stroke()
            ctx.beginPath(); ctx.arc(n.sx, n.sy, rad * 1.85, rot + Math.PI, rot + Math.PI + 1.5); ctx.stroke()
            ctx.strokeStyle = rgba(c, 0.35 * em); ctx.lineWidth = 1
            ctx.beginPath(); ctx.arc(n.sx, n.sy, rad * 2.35, -rot * 1.4, -rot * 1.4 + 0.9); ctx.stroke()
            const sa = RM ? n.pulse : now * 0.0014 + n.pulse
            spr(HOT, n.sx + Math.cos(sa) * rad * 2.35, n.sy + Math.sin(sa) * rad * 2.35 * 0.92, 3.0 * n.ss, c, 0.85 * em)
            const hr = rad * 2.9, hrot = (RM ? 0 : -now * 0.0003) + n.pulse
            ctx.strokeStyle = rgba(c, 0.22 * em); ctx.lineWidth = 1
            ctx.beginPath()
            for (let k = 0; k < 6; k++) { const a = hrot + k * Math.PI / 3; const px = n.sx + Math.cos(a) * hr, py = n.sy + Math.sin(a) * hr; k === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py) }
            ctx.closePath(); ctx.stroke()
          } else {
            const dr = rad * 1.6, rot = (RM ? 0 : now * 0.0005) + n.pulse
            ctx.strokeStyle = rgba(c, (0.5 + hv * 0.4) * em * st); ctx.lineWidth = 1.1
            ctx.beginPath()
            for (let k = 0; k < 6; k++) { const a = rot + k * Math.PI / 3; const px = n.sx + Math.cos(a) * dr, py = n.sy + Math.sin(a) * dr; k === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py) }
            ctx.closePath(); ctx.stroke()
            spr(HOT, n.sx + Math.cos(rot) * dr, n.sy + Math.sin(rot) * dr, 2.2 * n.ss, c, 0.65 * em * st)
          }
        }

        for (let i = ripples.length - 1; i >= 0; i--) {
          const r = ripples[i]
          r.r += (r.max * 2.2) * dt; r.a -= dt * (r.a > 0.4 ? 1.6 : 1.1)
          if (r.a <= 0.02 || r.r > r.max) { ripples.splice(i, 1); continue }
          ctx.strokeStyle = rgba(r.c, Math.max(0, r.a)); ctx.lineWidth = r.w
          ctx.beginPath(); ctx.arc(r.n.sx, r.n.sy, r.r, 0, 7); ctx.stroke()
        }

        ctx.globalCompositeOperation = 'source-over'
        for (const n of order) {
          const isCore = n.id === 'core'
          if (n.birth < 0.65) continue
          const showLabel = isCore || n.isHub || n.hoverT > 0.1 || (selNode && related && related.has(n.id) && cam.zoom > 0.9)
          if (!showLabel) continue
          let em = 1; if (focus) em = related!.has(n.id) ? 1 : (1 - dimAmt * 0.7)
          if (em < 0.14) continue
          const c = CAT[n.cat].c
          const rad = n.r * n.ss
          if (isCore) {
            ctx.font = '800 ' + (15 * Math.min(1.4, n.ss)).toFixed(0) + 'px Orbitron, monospace'
            ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
            ctx.shadowColor = c; ctx.shadowBlur = 18; ctx.fillStyle = '#eafaff'
            ctx.fillText('COSMOS', n.sx, n.sy + rad * 2.9); ctx.shadowBlur = 0
            continue
          }
          const fs = n.isHub ? 12.5 : 10.5
          ctx.font = (n.isHub ? '700 ' : '400 ') + fs + "px 'Share Tech Mono', monospace"
          ctx.textAlign = 'center'; ctx.textBaseline = 'top'
          const ly = n.sy + rad * (n.isHub ? 2.6 : 2.0) + 4
          ctx.shadowColor = rgba(c, 0.9); ctx.shadowBlur = n.isHub ? 10 : 6
          ctx.fillStyle = n.isHub ? rgba(c, em) : 'rgba(226,244,252,' + (em * (n.hoverT > 0.1 ? 1 : 0.82)).toFixed(2) + ')'
          let txt = n.label; if (txt.length > 22 && n.hoverT < 0.5) txt = txt.slice(0, 21) + '…'
          if (n.isHub) txt = txt.toUpperCase()
          ctx.fillText(txt, n.sx, ly); ctx.shadowBlur = 0
        }

        if (selNode && isFinite(selNode.sx)) {
          const pw = panel.offsetWidth || 300, ph = panel.offsetHeight || 320
          const gap = Math.max(26, selNode.r * selNode.ss * 2.4)
          let side = 1, px = selNode.sx + gap + 22
          if (px + pw > W - 14) { side = -1; px = selNode.sx - gap - 22 - pw }
          if (px < 14) px = 14
          let py = Math.max(66, Math.min(H - ph - 14, selNode.sy - ph * 0.42))
          const rect = root!.getBoundingClientRect()
          panel.style.left = (rect.left + px).toFixed(0) + 'px'; panel.style.top = (rect.top + py).toFixed(0) + 'px'
          const c = CAT[selNode.cat].c
          const ax = side === 1 ? px : px + pw
          const ay = Math.max(py + 20, Math.min(py + ph - 20, selNode.sy))
          ctx.globalCompositeOperation = 'lighter'
          ctx.strokeStyle = rgba(c, 0.55); ctx.lineWidth = 1.2
          ctx.beginPath(); ctx.moveTo(selNode.sx + side * gap * 0.6, selNode.sy); ctx.lineTo(ax - side * 12, ay); ctx.lineTo(ax, ay); ctx.stroke()
          spr(HOT, selNode.sx + side * gap * 0.6, selNode.sy, 3.4, c, 0.9); spr(HOT, ax, ay, 3, c, 0.8)
          ctx.globalCompositeOperation = 'source-over'
        }

        hudFire.textContent = String(fires.length)
        raf = requestAnimationFrame(frame)
      }

      const tip = $('tip')!
      const tipN = tip.querySelector('.n') as HTMLElement, tipT = tip.querySelector('.t') as HTMLElement
      function hitTest(mx: number, my: number) {
        let best = null as any, bd = 1e9
        for (const n of nodes) {
          if (n.birth < 0.5) continue
          const r = Math.max(10, n.r * n.ss * 1.3 + 8)
          const d = Math.hypot(mx - n.sx, my - n.sy)
          if (d < r && d < bd) { bd = d; best = n }
        }
        return best
      }
      const clientToLocal = (e: PointerEvent | MouseEvent) => { const r = root!.getBoundingClientRect(); return { mx: e.clientX - r.left, my: e.clientY - r.top } }

      let dragging = false, moved = false, lx = 0, ly = 0
      canvas.addEventListener('pointerdown', e => { dragging = true; moved = false; lx = e.clientX; ly = e.clientY; canvas.classList.add('dragging'); canvas.setPointerCapture(e.pointerId) })
      canvas.addEventListener('pointermove', e => {
        const { mx, my } = clientToLocal(e)
        if (dragging) {
          const dx = e.clientX - lx, dy = e.clientY - ly; lx = e.clientX; ly = e.clientY
          if (Math.abs(dx) + Math.abs(dy) > 2) moved = true
          if (e.shiftKey) { cam.panX += dx; cam.panY += dy } else { cam.yaw += dx * 0.006; cam.pitch += dy * 0.006; cam.pitch = Math.max(-1.2, Math.min(1.2, cam.pitch)) }
          tip.classList.remove('on'); return
        }
        const h = hitTest(mx, my)
        if (h !== hoverNode) { hoverNode = h; if (h) ripple(h, false) }
        if (h) {
          canvas.style.cursor = 'pointer'
          const r = root!.getBoundingClientRect()
          tip.style.left = (r.left + h.sx) + 'px'; tip.style.top = (r.top + h.sy - h.r * h.ss) + 'px'
          tipN.textContent = h.label
          tipT.textContent = CAT[h.cat].label + ' · ' + (h.status || 'online')
          tipT.style.color = CAT[h.cat].c
          tip.classList.add('on')
        } else { canvas.style.cursor = 'grab'; tip.classList.remove('on') }
      })
      function endDrag(e: PointerEvent) {
        if (!dragging) return
        dragging = false; canvas.classList.remove('dragging')
        if (!moved) { const { mx, my } = clientToLocal(e); const h = hitTest(mx, my); if (h) { selectNode(h); ripple(h, true) } else deselect() }
      }
      canvas.addEventListener('pointerup', endDrag as EventListener)
      canvas.addEventListener('pointercancel', () => { dragging = false; canvas.classList.remove('dragging') })
      canvas.addEventListener('contextmenu', e => e.preventDefault())
      canvas.addEventListener('dblclick', () => { deselect(); cam.targetZoom = 1; cam.panX = 0; cam.panY = 0; cam.yaw = 0.5; cam.pitch = -0.30 })
      canvas.addEventListener('wheel', e => { e.preventDefault(); const f = Math.exp(-e.deltaY * 0.0011); cam.targetZoom = Math.max(0.45, Math.min(3.0, cam.targetZoom * f)) }, { passive: false })
      canvas.addEventListener('pointerleave', () => { hoverNode = null; tip.classList.remove('on') })
      $('p-close')!.addEventListener('click', deselect)
      // OPEN ▸ — jump to the page that manages the selected node's subsystem.
      $('p-open')!.addEventListener('click', (e: any) => {
        const b = e.currentTarget; b.style.transform = 'scale(0.96)'
        setTimeout(() => b.style.transform = '', 140)
        if (selNode) {
          const page = CAT_PAGE[selNode.cat]
          if (page) navRef.current?.(page)
        }
      })
      // Search — type in the HUD box, Enter jumps to the first matching node.
      const searchEl = root!.querySelector('#nx-search') as HTMLInputElement | null
      if (searchEl) {
        searchEl.addEventListener('keydown', (e: KeyboardEvent) => {
          e.stopPropagation()                    // don't trigger the Esc deselect
          if (e.key !== 'Enter') return
          const q = searchEl.value.trim().toLowerCase()
          if (!q) return
          const hitN = nodes.find(n => n.birth > 0.5 && (n.label || '').toLowerCase().includes(q))
          if (hitN) { selectNode(hitN); ripple(hitN, true) }
        })
      }
      const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') deselect() }
      addWin('keydown', onEsc as EventListener)

      $('s-nodes')!.textContent = String(nodes.length)
      $('s-lobes')!.textContent = String(LOBES.length)
      hudFire.textContent = '0'

      raf = requestAnimationFrame(frame)
      cleanups.push(() => cancelAnimationFrame(raf))
    }

    const cleanups: (() => void)[] = []

    fetch('/api/nexus')
      .then(r => r.ok ? r.json() : Promise.reject())
      .then((data: any) => {
        if (disposed) return
        if (data && Array.isArray(data.lobes) && data.lobes.length) run(data)
        else run(FALLBACK)
      })
      .catch(() => { if (!disposed) run(FALLBACK) })

    return () => {
      disposed = true
      cleanups.forEach(fn => fn())
      intervals.forEach(clearInterval)
      winListeners.forEach(([ev, fn]) => window.removeEventListener(ev, fn))
      // The tooltip + panel are position:fixed and portalled by the browser to the
      // page — they live inside root and unmount with it, no extra cleanup needed.
    }
  }, [])

  return <div ref={rootRef} className="nexus-root" dangerouslySetInnerHTML={{ __html: HTML }} />
}
