"""
Silent web browsing for Cosmos.
Persistent keep-alive httpx transport (services.http_pool — truststore supplies
the macOS system trust the old system-curl path relied on).
No API keys required.
"""

import json as _json
import re
from urllib.parse import quote

from services import http_pool, llm
from services.llm import extract_text

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"


async def _curl(url: str, timeout: int = 10) -> str | None:
    """Fetch URL over the pooled keep-alive client (follows redirects, like
    the old `curl -L`). None on any failure — never raises."""
    try:
        client = http_pool.get_client("web_search")
        r = await client.get(
            url, timeout=timeout, follow_redirects=True,
            headers={"User-Agent": UA,
                     "Accept": "text/html,application/xhtml+xml,application/json"})
        return r.text if r.text else None
    except Exception:
        return None


# ─── DuckDuckGo Instant Answer ────────────────────────────────

async def ddg_instant(query: str) -> str | None:
    url  = f"https://api.duckduckgo.com/?q={quote(query)}&format=json&no_html=1&skip_disambig=1"
    body = await _curl(url)
    if not body:
        return None
    try:
        d = _json.loads(body)
        if d.get("Abstract"):
            return d["Abstract"]
        for t in d.get("RelatedTopics", []):
            if isinstance(t, dict) and t.get("Text"):
                return t["Text"]
    except Exception:
        pass
    return None


# ─── DuckDuckGo HTML search ───────────────────────────────────

async def ddg_search(query: str, max_results: int = 5) -> list[dict]:
    url  = f"https://html.duckduckgo.com/html/?q={quote(query)}"
    body = await _curl(url, timeout=12)
    if not body:
        return []
    results = []
    titles   = re.findall(r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', body, re.DOTALL)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', body, re.DOTALL)
    for i, (href, title) in enumerate(titles[:max_results]):
        snippet = snippets[i] if i < len(snippets) else ""
        results.append({
            "title":   _strip_tags(title).strip(),
            "url":     href,
            "snippet": _strip_tags(snippet).strip(),
        })
    return results


# ─── URL content fetcher ──────────────────────────────────────

async def fetch_page_text(url: str, max_chars: int = 6000) -> str | None:
    if not url.startswith("http"):
        url = "https://" + url
    body = await _curl(url, timeout=14)
    if not body:
        return None
    text = _extract_text(body)
    return text[:max_chars]


# ─── Full search → answer pipeline ───────────────────────────

async def search_and_answer(query: str, model: str) -> str:
    """Search the web and return a spoken Jarvis-style answer.

    LLM summarisation runs on the fast fallback chain."""

    # 1. Try instant answer (fastest)
    instant = await ddg_instant(query)
    if instant and len(instant) > 20:
        return await _format_answer(query, instant, model)

    # 2. Full search
    results = await ddg_search(query, max_results=4)
    if not results:
        # Fallback: LLM knowledge only
        return await _llm_only_answer(query, model)

    # 3. Fetch top result for depth
    top_text = ""
    top_url  = results[0].get("url", "")
    if top_url and top_url.startswith("http"):
        top_text = await fetch_page_text(top_url, max_chars=3500) or ""

    context = "\n\n".join(
        f"{r['title']}\n{r['snippet']}" for r in results
    )
    if top_text:
        context = top_text[:2500] + "\n\n---\n" + context

    return await _format_answer(query, context, model)


async def _format_answer(query: str, context: str, model: str) -> str:
    prompt = (
        f"Question: {query}\n\n"
        f"Source:\n{context[:4000]}\n\n"
        f"Answer in 1-2 sentences. JARVIS style: terse, factual, end with 'sir'. "
        f"Include numbers (prices, distances, stats) when available."
    )
    try:
        resp = await llm.acreate(
            model=model, fallbacks=llm.FAST_FALLBACKS, max_tokens=150,
            system="You are COSMOS. Answer questions from source material concisely.",
            messages=[{"role": "user", "content": prompt}],
        )
        return extract_text(resp).strip()
    except Exception as e:
        return f"Search found results but summarisation failed, sir: {e}"


async def _llm_only_answer(query: str, model: str) -> str:
    """No web results — use LLM knowledge only."""
    prompt = (
        f"Answer this question using your knowledge: {query}\n"
        f"Context: user is in Bangalore, India.\n"
        f"One sentence, JARVIS style, end with 'sir'."
    )
    try:
        resp = await llm.acreate(
            model=model, fallbacks=llm.FAST_FALLBACKS, max_tokens=100,
            system="Answer concisely. One sentence. Jarvis style.",
            messages=[{"role": "user", "content": prompt}],
        )
        return extract_text(resp).strip()
    except Exception:
        return f"Couldn't find information on '{query}', sir."


# ─── HTML helpers ─────────────────────────────────────────────

def _strip_tags(html: str) -> str:
    return re.sub(r'<[^>]+>', '', html)


def _extract_text(html: str) -> str:
    html = re.sub(r'<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>',
                  '', html, flags=re.DOTALL | re.IGNORECASE)
    text = _strip_tags(html)
    return re.sub(r'\s+', ' ', text).strip()
