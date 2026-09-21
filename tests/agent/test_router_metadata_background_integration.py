"""The real detached fork keeps router context discovery and patience together."""
import pytest
from agent import background_review as review
from agent import model_metadata as metadata
from tests.agent.test_explicit_router_metadata import endpoint, MODEL, URL, NATIVE
from run_agent import AIAgent


@pytest.mark.parametrize("provider", ["custom", "openai"])
def test_real_router_fork_retains_live_context_without_native_discovery(endpoint, provider):
    parent = AIAgent(model=MODEL, provider=provider, requested_provider="custom:omniroute",
                     api_key="fixture-only", base_url=URL, api_mode="chat_completions",
                     quiet_mode=True, skip_context_files=True, skip_memory=True,
                     reasoning_config={"enabled": True, "effort": "high"}, platform="webui")
    child = None
    try:
        assert parent.context_compressor.context_length == 98304
        # Require the detached constructor to resolve metadata itself, rather
        # than hiding a missing identity behind the parent's positive cache.
        metadata._LOCAL_CTX_PROBE_CACHE.clear()
        endpoint["paths"].clear()
        child, runtime, routed = review.build_cache_parity_fork(parent, {}, max_iterations=2)
        assert not routed and runtime["requested_provider"] == "custom:omniroute"
        assert child.requested_provider == "custom:omniroute"
        assert child.context_compressor.context_length == 98304
        assert "/v1/models/" + MODEL in endpoint["paths"]
        assert not NATIVE.intersection(endpoint["paths"])
        assert child._ollama_num_ctx is None
        assert child._compute_non_stream_stale_timeout({"input": "synthetic"}) == 90
        assert child.model == parent.model and child.reasoning_config == parent.reasoning_config
        assert child.tools == parent.tools and child._persist_disabled and child._session_db is None
    finally:
        if child is not None:
            child.close()
        parent.close()
