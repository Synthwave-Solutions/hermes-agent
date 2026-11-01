"""Compression media.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

from typing import Any, Optional

def _image_payload(msg: dict[str, Any]) -> list[str]:
    """Data URL / base64 string of each image block in a message (``""`` when unmeasurable).

    The provider counts BLOCKS (one ``tool_result`` with three screenshots is three), so the
    block count is the list length. Bytes are the summed string lengths: the payload is ASCII and
    the JSON framing around it is noise against a 24 MB budget. The strings also let the caller
    read dimensions from headers only, without a second walk or a full decode.
    """
    from agent.context_compressor import (
        _is_image_part, _tool_result_parts
    )
    parts = _tool_result_parts(msg.get("content"))
    if not isinstance(parts, list):
        return []
    sources: list[str] = []
    for p in parts:
        if not _is_image_part(p):
            continue
        image_url = p.get("image_url")
        source = p.get("source")
        data = (
            (image_url.get("url") if isinstance(image_url, dict) else image_url)
            or (source.get("data") if isinstance(source, dict) else None)
        )
        sources.append(data if isinstance(data, str) else "")
    return sources


def evict_stale_outbound_tool_images(api_messages: list[dict[str, Any]], retired_ids: Optional[set[str]] = None) -> int:
    """Drop stale screenshot/vision payloads from the per-call API copy.

    Compression's keep-newest pass only runs when prune/compress fires, and the Anthropic
    adapter's screenshot eviction only sees nested ``tool_result`` blocks. OpenAI-style
    ``image_url`` tool results otherwise ride every subsequent request until a 413 forces
    the reactive strip (#89286). Call this on the cloned ``api_messages`` list after
    sanitization (#89296). Do not pass persisted history — the rewrite is send-path only.

    Eviction is driven by the provider limit, counted in image BLOCKS, with user uploads
    reserved against the ceiling but never rewritten; the block ceiling depends on the
    request's image dimensions — policy and rationale in :mod:`agent.image_eviction_policy`.
    Returns the number of messages rewritten.
    ``retired_ids`` latches the frontier per session: see :func:`latched_retire_count`.
    """
    from agent.context_compressor import (
        _strip_images_from_tool_msg, images_all_within_many_image_limit, latched_retire_count, outbound_image_retire_count
    )
    carriers: list[tuple[int, list[str]]] = []  # (index, image sources), newest first
    reserved: list[str] = []
    for i in range(len(api_messages) - 1, -1, -1):
        msg = api_messages[i]
        sources = _image_payload(msg) if isinstance(msg, dict) else []
        if sources and msg.get("role") == "tool":
            carriers.append((i, sources))
        else:
            reserved.extend(sources)
    retire = outbound_image_retire_count(
        [len(sources) for _, sources in carriers],
        len(reserved),
        carrier_bytes_newest_first=[sum(map(len, sources)) for _, sources in carriers],
        reserved_bytes=sum(map(len, reserved)),
        many_image_safe=images_all_within_many_image_limit([*reserved, *(x for _, ss in carriers for x in ss)]),
    )
    retire = latched_retire_count(retire, [api_messages[i].get("tool_call_id") for i, _ in carriers], retired_ids)
    pruned = 0
    for i, _ in carriers[len(carriers) - retire:]:
        new_msg = _strip_images_from_tool_msg(api_messages[i])
        if new_msg is not None:
            api_messages[i] = new_msg
            pruned += 1
    return pruned
