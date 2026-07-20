// Pure speech-text helpers — extracted from useVoice so they're testable and
// shared by the streaming voice path.

// Dev-vocabulary lexicon: stop TTS from garbling the user's daily terms.
// Case-sensitive where it matters (\bPRs?\b must not hit "prs" inside words,
// and "pr" lowercase inside prose is usually not "pull request").
const LEXICON: [RegExp, string][] = [
  [/\bgithub\b/gi, 'git hub'],
  [/\bkubectl\b/gi, 'kube control'],
  [/\bk8s\b/gi, 'kubernetes'],
  [/\bSSO\b/g, 'S S O'],
  [/\bTCC\b/g, 'T C C'],
  [/\bnginx\b/gi, 'engine x'],
  [/\bpostgres\b/gi, 'post-gress'],
  [/\bCI\b/g, 'C I'],
  [/\bdocker\b/gi, 'Docker'],
  [/\bPRs\b/g, 'pull requests'],
  [/\bPR\b/g, 'pull request'],
  [/\bnpm\b/g, 'N P M'],
  [/\bAPI\b/g, 'A P I'],
  [/\bUI\b/g, 'U I'],
  [/\bCLI\b/g, 'C L I'],
]

export function fixPronunciation(text: string): string {
  let out = text
    // File paths → natural language
    .replace(/\/Users\/[^\s/]+\/([^\s]+)/g, (_, rest) => {
      const parts = rest.replace(/\//g, ' ').trim()
      return parts.length > 30 ? parts.split(' ').slice(-2).join(' ') + ' folder' : parts
    })
    // Common abbreviations
    .replace(/\bsrc\b/gi, 'source')
    .replace(/\bpkg\b/gi, 'package')
    .replace(/\bauth\b/gi, 'authentication')
    .replace(/\brepo\b/gi, 'repository')
    .replace(/\btodo\b/gi, 'to-do')
  for (const [re, sub] of LEXICON) out = out.replace(re, sub)
  return out
    // snake_case → words, but ONLY short 2-3 segment identifiers — long
    // snakes are code the user doesn't want read out transformed.
    .replace(/\b([a-z]+)_([a-z]+)(?:_([a-z]+))?\b/g, (m, a, b, c) =>
      m.length <= 24 ? [a, b, c].filter(Boolean).join(' ') : m)
    // File extensions — don't read them aloud
    .replace(/\.(py|js|ts|tsx|go|rb|sh|md|json|yaml|env)\b/g, ' file')
    // Long paths — just say "that file" if buried
    .replace(/[~./][\w./\\-]{30,}/g, 'that file')
}

/** Everything before the first blank line / list / heading — the spoken lead. */
export function extractLead(text: string): string {
  const lines = text.replace(/\r\n/g, '\n').split('\n')
  const leadLines: string[] = []
  for (const raw of lines) {
    const l = raw.trim()
    if (!l) { if (leadLines.length) break; else continue }
    if (/^([-*•]\s|#{1,4}\s|\d+[.)]\s|\|)/.test(l)) break
    leadLines.push(l)
  }
  return (leadLines.join(' ') || text).trim()
}

export function cleanForSpeech(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, '')
    .replace(/`[^`]+`/g, m => m.replace(/`/g, ''))
    .replace(/[*_#>\-]/g, '')
    .replace(/\n+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

export function truncateForSpeech(text: string): string {
  const clean = cleanForSpeech(extractLead(text))
  const sentences = clean.match(/[^.!?]+[.!?]+/g) ?? [clean]
  const brief = sentences.slice(0, 2).join(' ').trim()
  const truncated = brief.length > 180 ? brief.slice(0, 177) + '...' : brief || clean.slice(0, 180)
  return fixPronunciation(truncated)
}

/** The rest of the reply as speakable sentence chunks — powers
    "cosmos, read the rest" after the truncated lead. */
export function remainderChunks(fullText: string, chunkSentences = 3): string[] {
  const clean = cleanForSpeech(
    fullText.replace(/\r\n/g, '\n').split('\n')
      .map(l => l.trim().replace(/^([-*•]\s|#{1,4}\s|\d+[.)]\s)/, ''))
      .join(' '))
  const sentences = clean.match(/[^.!?]+[.!?]+/g) ?? []
  const spokenAlready = 2                      // truncateForSpeech spoke these
  const rest = sentences.slice(spokenAlready)
  const chunks: string[] = []
  for (let i = 0; i < rest.length; i += chunkSentences) {
    const chunk = rest.slice(i, i + chunkSentences).join(' ').trim()
    if (chunk) chunks.push(fixPronunciation(chunk))
  }
  return chunks
}

export function isReadTheRest(text: string): boolean {
  return /^(continue|go on|keep going|read (the rest|it all|more|on)|more)[.!?]?$/i
    .test(text.trim())
}
