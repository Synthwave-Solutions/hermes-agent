"""Regression tests for parallel image-generation tool batches."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import run_agent
from agent import tool_executor


def _tool_call(name: str, args: dict, call_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(args),
        ),
    )


def test_image_generate_batch_routes_to_concurrent_executor():
    agent = SimpleNamespace()
    agent._execute_tool_calls = run_agent.AIAgent._execute_tool_calls.__get__(agent)
    agent._execute_tool_calls_concurrent = MagicMock()
    agent._execute_tool_calls_sequential = MagicMock()
    assistant_message = SimpleNamespace(
        tool_calls=[
            _tool_call("image_generate", {"prompt": "variation one"}, "img_1"),
            _tool_call("image_generate", {"prompt": "variation two"}, "img_2"),
        ],
    )

    agent._execute_tool_calls(assistant_message, [], "task-image-batch")

    agent._execute_tool_calls_concurrent.assert_called_once()
    agent._execute_tool_calls_sequential.assert_not_called()


def test_image_generate_parallel_worker_cap_defaults_to_four():
    runnable_calls = [
        (
            0,
            _tool_call("image_generate", {"prompt": "one"}, "img_1"),
            "image_generate",
            {},
        ),
        (
            1,
            _tool_call("image_generate", {"prompt": "two"}, "img_2"),
            "image_generate",
            {},
        ),
        (
            2,
            _tool_call("image_generate", {"prompt": "three"}, "img_3"),
            "image_generate",
            {},
        ),
        (
            3,
            _tool_call("image_generate", {"prompt": "four"}, "img_4"),
            "image_generate",
            {},
        ),
        (
            4,
            _tool_call("image_generate", {"prompt": "five"}, "img_5"),
            "image_generate",
            {},
        ),
    ]

    with patch("hermes_cli.config.load_config", return_value={}):
        assert tool_executor._max_workers_for_tool_batch(runnable_calls) == 4


def test_image_generate_parallel_worker_cap_can_be_configured_lower():
    runnable_calls = [
        (
            0,
            _tool_call("image_generate", {"prompt": "one"}, "img_1"),
            "image_generate",
            {},
        ),
        (
            1,
            _tool_call("image_generate", {"prompt": "two"}, "img_2"),
            "image_generate",
            {},
        ),
    ]

    with patch(
        "hermes_cli.config.load_config",
        return_value={"image_gen": {"max_parallel_requests": 1}},
    ):
        assert tool_executor._max_workers_for_tool_batch(runnable_calls) == 1


def test_browser_exec_batch_is_capped_below_the_general_worker_ceiling():
    """Each named session is its own browser: a batch of them is a batch of
    browsers, which will sink a small host at the full worker ceiling."""
    runnable_calls = [
        (
            i,
            _tool_call("browser_exec", {"code": "print(1)", "session": f"s{i}"}, f"b{i}"),
            "browser_exec",
            {},
        )
        for i in range(6)
    ]

    with patch("hermes_cli.config.load_config", return_value={}):
        assert (
            tool_executor._max_workers_for_tool_batch(runnable_calls)
            == tool_executor._MAX_BROWSER_EXEC_PARALLEL
        )
        assert tool_executor._MAX_BROWSER_EXEC_PARALLEL < tool_executor._MAX_TOOL_WORKERS


def test_read_only_terminal_batch_uses_the_general_worker_ceiling():
    runnable_calls = [
        (i, _tool_call("terminal", {"command": "ls -la"}, f"t{i}"), "terminal", {})
        for i in range(10)
    ]

    with patch("hermes_cli.config.load_config", return_value={}):
        assert (
            tool_executor._max_workers_for_tool_batch(runnable_calls)
            == tool_executor._MAX_TOOL_WORKERS
        )
