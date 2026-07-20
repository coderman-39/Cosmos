import { Fragment, ReactNode, memo } from 'react'

// ══════════════════════════════════════════════════════════════
// Lightweight markdown renderer for Cosmos's replies.
// Handles the shapes the model actually emits — headings, bullet
// & numbered lists, bold, and inline code — styled to match the
// HUD. No dependency; full control over the sci-fi look.
// ══════════════════════════════════════════════════════════════

// ── Clickable link (opens in a new tab; only http/https/mailto) ────
function safeHref(href: string): string | null {
  return /^(https?:|mailto:)/i.test(href.trim()) ? href.trim() : null
}

function Anchor({ href, label, k }: { href: string; label: string; k: string }) {
  return (
    <a key={k} href={href} target="_blank" rel="noopener noreferrer"
      style={{ color: 'var(--cyan)', textDecoration: 'underline',
        textUnderlineOffset: 2, textShadow: '0 0 6px rgba(0,212,255,0.4)',
        wordBreak: 'break-all' }}>
      {label}
    </a>
  )
}

// Autolink bare http(s) URLs inside a plain run, peeling trailing sentence
// punctuation off the href (so "see https://x.com." doesn't swallow the dot).
function linkifyRun(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = []
  text.split(/(https?:\/\/[^\s<>]+)/g).forEach((seg, j) => {
    if (!seg) return
    if (/^https?:\/\//.test(seg)) {
      const trail = (seg.match(/[.,;:!?)\]}'"]+$/) || [''])[0]
      const url = seg.slice(0, seg.length - trail.length)
      out.push(<Anchor key={`${key}-a${j}`} k={`${key}-a${j}`} href={url} label={url} />)
      if (trail) out.push(<Fragment key={`${key}-p${j}`}>{trail}</Fragment>)
    } else {
      out.push(<Fragment key={`${key}-s${j}`}>{seg}</Fragment>)
    }
  })
  return out
}

// ── Inline: **bold**, `code`, [links](url), bare URLs, plain runs ──
function renderInline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = []
  // Split on **bold**, `code`, or [label](url), keeping delimiters via captures.
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g)
  parts.forEach((part, i) => {
    if (!part) return
    const key = `${keyBase}-${i}`
    if (part.startsWith('**') && part.endsWith('**')) {
      out.push(
        <strong key={key} style={{ color: 'var(--text-hi)', fontWeight: 700 }}>
          {part.slice(2, -2)}
        </strong>,
      )
    } else if (part.startsWith('`') && part.endsWith('`')) {
      out.push(
        <code key={key} style={{
          fontFamily: 'var(--font-m)', fontSize: '0.92em',
          color: 'var(--cyan)', background: 'rgba(0,212,255,0.08)',
          border: '1px solid rgba(0,212,255,0.15)',
          borderRadius: 3, padding: '0.5px 4px', whiteSpace: 'nowrap',
        }}>
          {part.slice(1, -1)}
        </code>,
      )
    } else {
      // [label](url) markdown link?
      const md = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/)
      const href = md ? safeHref(md[2]) : null
      if (md && href) {
        out.push(<Anchor key={key} k={key} href={href} label={md[1]} />)
      } else {
        // Plain run — autolink any bare URLs within it.
        out.push(...linkifyRun(part, key))
      }
    }
  })
  return out
}

// ── Bullet glyph ───────────────────────────────────────────────
function Bullet() {
  return (
    <span style={{ color: 'var(--cyan)', flexShrink: 0, marginTop: 1,
      textShadow: '0 0 6px rgba(0,212,255,0.5)', fontSize: '0.9em' }}>
      ▸
    </span>
  )
}

type Block =
  | { kind: 'h'; level: number; text: string }
  | { kind: 'ul'; items: string[] }
  | { kind: 'ol'; items: string[] }
  | { kind: 'p'; text: string }

// ── Defensive normalisation ────────────────────────────────────
// The model sometimes jams bullets inline ("Highlights:• a • b") instead of
// newlined markdown. Break those onto their own lines so they render as a list.
function normalize(src: string): string {
  let s = src.replace(/\r\n/g, '\n')
  // Any "•" (with surrounding spaces) → a fresh bullet line.
  s = s.replace(/[ \t]*•[ \t]*/g, '\n- ')
  // A bold label glued to a following bullet → break onto its own line.
  s = s.replace(/(\*\*[^*]+\*\*)[ \t]*(?=- )/g, '$1\n')
  // A "Label:" glued to a following bullet → break after the colon.
  s = s.replace(/([^\n]):[ \t]*(?=\n?- )/g, '$1:\n')
  return s
}

// ── Group flat lines into semantic blocks ──────────────────────
function parseBlocks(src: string): Block[] {
  const lines = normalize(src).split('\n')
  const blocks: Block[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    const trimmed = line.trim()

    if (!trimmed) { i++; continue }

    // Heading  (#, ##, ###)
    const h = trimmed.match(/^(#{1,4})\s+(.*)$/)
    if (h) {
      blocks.push({ kind: 'h', level: h[1].length, text: h[2] })
      i++; continue
    }

    // Bullet list  (-, *, •)
    if (/^[-*•]\s+/.test(trimmed)) {
      const items: string[] = []
      while (i < lines.length && /^\s*[-*•]\s+/.test(lines[i])) {
        items.push(lines[i].trim().replace(/^[-*•]\s+/, ''))
        i++
      }
      blocks.push({ kind: 'ul', items })
      continue
    }

    // Numbered list  (1. 2. …)
    if (/^\d+[.)]\s+/.test(trimmed)) {
      const items: string[] = []
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(lines[i].trim().replace(/^\d+[.)]\s+/, ''))
        i++
      }
      blocks.push({ kind: 'ol', items })
      continue
    }

    // Paragraph — gather consecutive non-blank, non-structural lines
    const para: string[] = []
    while (i < lines.length) {
      const l = lines[i].trim()
      if (!l || /^(#{1,4}\s|[-*•]\s|\d+[.)]\s)/.test(l)) break
      para.push(l)
      i++
    }
    blocks.push({ kind: 'p', text: para.join(' ') })
  }
  return blocks
}

function Markdown({ text }: { text: string }) {
  const blocks = parseBlocks(text)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
      {blocks.map((b, bi) => {
        if (b.kind === 'h') {
          return (
            <div key={bi} style={{
              fontFamily: 'var(--font-d)', fontWeight: 700,
              fontSize: b.level <= 1 ? 12 : 10.5,
              letterSpacing: '0.08em', color: 'var(--cyan)',
              textShadow: '0 0 8px rgba(0,212,255,0.4)',
              marginTop: bi === 0 ? 0 : 2,
            }}>
              {renderInline(b.text, `h${bi}`)}
            </div>
          )
        }
        if (b.kind === 'ul' || b.kind === 'ol') {
          return (
            <div key={bi} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {b.items.map((it, ii) => (
                <div key={ii} style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
                  {b.kind === 'ul'
                    ? <Bullet />
                    : <span style={{ color: 'var(--cyan)', flexShrink: 0, marginTop: 1,
                        fontFamily: 'var(--font-m)', fontSize: '0.85em', minWidth: 14,
                        textAlign: 'right', opacity: 0.85 }}>{ii + 1}.</span>}
                  <span style={{ minWidth: 0, flex: 1 }}>{renderInline(it, `li${bi}-${ii}`)}</span>
                </div>
              ))}
            </div>
          )
        }
        return (
          <div key={bi}>{renderInline(b.text, `p${bi}`)}</div>
        )
      })}
    </div>
  )
}

// Memoized on `text` — completed chat messages never re-parse their markdown
// when the surrounding tree re-renders (streaming ticks, input keystrokes).
export default memo(Markdown)
