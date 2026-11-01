"""Model metadata protocol.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

import threading
from typing import Any, Iterator, Optional

def _targeted_probe_miss_seen(misses: dict[str, float], lock: threading.Lock, max_size: int, key: str, record: bool) -> bool:
    from agent.model_metadata import (
        _ENDPOINT_PROBE_FAILURE_TTL_SECONDS, time
    )
    now = time.monotonic()
    with lock:
        for stale_key, seen in list(misses.items()):
            if now - seen >= _ENDPOINT_PROBE_FAILURE_TTL_SECONDS:
                del misses[stale_key]
        if record:
            misses[key] = now
            while len(misses) > max_size:
                misses.pop(next(iter(misses)))
        return key in misses


def _ollama_probe_miss_seen(key: str, *, record: bool = False) -> bool:
    from agent.model_metadata import (
        _OLLAMA_PROBE_MISS_MAX_SIZE, _ollama_probe_miss_lock, _ollama_probe_misses
    )
    return _targeted_probe_miss_seen(_ollama_probe_misses, _ollama_probe_miss_lock, _OLLAMA_PROBE_MISS_MAX_SIZE, key, record)


def _lmstudio_probe_miss_seen(key: str, *, record: bool = False) -> bool:
    from agent.model_metadata import (
        _LMSTUDIO_PROBE_MISS_MAX_SIZE, _lmstudio_probe_miss_lock, _lmstudio_probe_misses
    )
    return _targeted_probe_miss_seen(_lmstudio_probe_misses, _lmstudio_probe_miss_lock, _LMSTUDIO_PROBE_MISS_MAX_SIZE, key, record)


def _credential_identity(api_key: object) -> str:
    """Non-reversible credential identity for cache scoping. Callable (minted) keys are not
    fingerprinted: doing so would mint on every cache read."""
    from agent.credential_persistence import fingerprint_secret_value
    return (fingerprint_secret_value(api_key) or "") if isinstance(api_key, str) else ""


def _speculative_protocol_outcomes(legs, serial_probe, headers: dict[str, str], verify) -> Iterator[tuple[Optional[str], Any]]:
    """Run the remaining protocol probes of ``detect_local_server_type`` concurrently and return
    their outcomes in priority order, stopping after the first match or connect timeout.

    One owned event loop runs on a joined worker thread (works even when called from inside a
    running loop); lower-priority probes still in flight are cancelled and drained before the
    async client closes. Each probe has a total budget of ``_SPECULATIVE_PROTOCOL_DEADLINE_SECONDS``
    (HTTPX's read timeout alone permits an indefinitely trickling response). A probe that expired
    is not treated as a negative verdict: detection resumes serially from that probe instead."""
    from agent.model_metadata import (
        _SPECULATION_INCOMPLETE, _SPECULATIVE_PROTOCOL_DEADLINE_SECONDS, _is_connect_timeout, itertools
    )
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    import httpx

    async def speculate():
        async with httpx.AsyncClient(timeout=2.0, headers=headers, verify=verify) as async_client:
            async def run(leg):
                name, urls, check = leg
                deadline = asyncio.timeout(_SPECULATIVE_PROTOCOL_DEADLINE_SECONDS)
                try:
                    async with deadline:
                        for url in urls:
                            r = await async_client.get(url)
                            if r.status_code == 200:
                                break
                        return (name if r.status_code == 200 and check(r) else None), None
                except TimeoutError as exc:
                    return None, _SPECULATION_INCOMPLETE if deadline.expired() else exc
                except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
                    return None, exc

            tasks = [asyncio.create_task(run(leg)) for leg in legs]
            outcomes = []
            try:
                for task in tasks:
                    outcome = await task
                    outcomes.append(outcome)
                    name, error = outcome
                    if name is not None or error is _SPECULATION_INCOMPLETE or (error is not None and _is_connect_timeout(error)):
                        break
                return outcomes
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="hermes-local-probe") as pool:
        outcomes = pool.submit(lambda: asyncio.run(speculate())).result()
    if outcomes and outcomes[-1][1] is _SPECULATION_INCOMPLETE:
        # Do not classify a large/slow native response as a negative verdict just because
        # speculation expired: resume serially (lazily, keeping the early exit) from that probe.
        return itertools.chain(outcomes[:-1], map(serial_probe, legs[len(outcomes) - 1:]))
    return iter(outcomes)
