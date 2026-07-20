# Deep Research

For any research request ("find the best…", "compare…", "what's the latest on…"):

1. **Fan out**: 2-4 `web_search` calls with DIFFERENT phrasings (batch them in one turn —
   they run in parallel). Vary angle: product name vs category, "review" vs "vs" vs "price".
2. **Go deep**: `fetch_url` the 2-3 most promising result URLs (also batchable).
   Prefer primary sources: official docs, manufacturer pages, first-party announcements.
3. **Cross-check**: facts that matter (prices, dates, specs) must agree across 2+ sources.
   If sources conflict, say so and give the range.
4. **Synthesize**: short answer first, then the key facts, naming sources ("per The Verge…").
   Numbers beat adjectives — include prices, dates, percentages.

Rules:
- Never answer purely from training data when the question involves "latest", "current",
  prices, versions, or events after your knowledge cutoff — search first.
- If DuckDuckGo returns nothing useful, retry once with simpler keywords before giving up.
- Keep the spoken summary ≤2 sentences; put detail into the chat text only if asked.
- User context: use the machine's locale/timezone. Prefer local pricing and availability when relevant.
