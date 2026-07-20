"""Shared persistent HTTP transport — one lazy httpx.AsyncClient PER SERVICE.

Replaces the per-call `curl` subprocesses (process spawn + fresh TCP + TLS on
every API call, ~300-600ms each) with keep-alive connections that make repeat
calls to the same host ~50-150ms. One client per service name gives fault
isolation: a wedged or closed client for one connector never affects another.

Zscaler caveat (verified on this machine): the corporate MITM proxy re-signs
TLS, so httpx's bundled certifi store fails with CERTIFICATE_VERIFY_FAILED
(e.g. on api.elevenlabs.io). `truststore.inject_into_ssl()` makes Python's SSL
use the macOS system trust store (where the Zscaler Root CA lives) — it is
injected lazily, exactly once, before the first client is built. Injection
failure is non-fatal: we fall back to certifi and let per-service code handle
any SSL errors (tts.py gates its cutover on exactly that).

Usage:
    from services import http_pool
    client = http_pool.get_client("slack")           # cached, keep-alive
    r = await client.post(url, headers=..., data=..., timeout=20)

Secrets ride in in-process header dicts — never argv, never temp files.

TODO(orchestrator): wire shutdown into main.py's lifespan teardown (this module
must not touch main.py itself):

    from services import http_pool
    ...
    # in the lifespan shutdown section:
    await http_pool.aclose_all()
"""

import httpx

_LIMITS = httpx.Limits(max_keepalive_connections=20, keepalive_expiry=120)
_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

_clients: dict[str, httpx.AsyncClient] = {}
_truststore_done = False


def _inject_truststore() -> None:
    """Make Python SSL trust the macOS system keychain (Zscaler Root CA).
    Guarded — runs at most once per process; never raises."""
    global _truststore_done
    if _truststore_done:
        return
    _truststore_done = True
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception as e:
        print(f"[http-pool] truststore injection failed "
              f"({type(e).__name__}: {str(e)[:120]}) — staying on certifi")


def get_client(name: str,
               timeout: float | httpx.Timeout | None = None) -> httpx.AsyncClient:
    """The persistent keep-alive client for `name`, built lazily on first use
    (and rebuilt if something closed it). `timeout` sets the client-level
    DEFAULT and only applies at construction — prefer passing per-request
    timeouts to the call itself (`client.post(..., timeout=20)`)."""
    client = _clients.get(name)
    if client is not None and not client.is_closed:
        return client
    _inject_truststore()
    client = httpx.AsyncClient(
        limits=_LIMITS,
        timeout=timeout if timeout is not None else _DEFAULT_TIMEOUT,
    )
    _clients[name] = client
    return client


async def aclose_all() -> None:
    """Close every pooled client (lifespan shutdown). Never raises."""
    for name in list(_clients):
        client = _clients.pop(name, None)
        if client is None:
            continue
        try:
            await client.aclose()
        except Exception:
            pass
