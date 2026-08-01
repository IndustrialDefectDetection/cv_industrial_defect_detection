"""Trace events must stay bounded and must not expose raw failure details."""

import ast
import threading
from pathlib import Path

import pytest

from agent_tracer import AgentTracer, attach_tracer
from strands_agent import MESAgentManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_tool_inputs_are_bounded_before_entering_the_trace():
    tracer = AgentTracer()
    tracer.tool_start(
        "Planner",
        "generate_pdf_report",
        {
            "long_text": "x" * 20_000,
            "many_values": list(range(100)),
        },
        "tool-1",
    )

    tool_input = tracer.snapshot()["events"][0]["tool_input"]
    assert len(tool_input["long_text"]) < 5_000
    assert len(tool_input["many_values"]) == 51
    assert "more entries" in tool_input["many_values"][-1]


def test_direct_tool_exception_does_not_enter_the_trace():
    tracer = AgentTracer()
    tracer.tool_start("Monitor", "query_database", {}, "tool-secret")
    tracer.tool_end(
        "Monitor",
        "query_database",
        "tool-secret",
        result=None,
        exception=RuntimeError("dsn=password-do-not-expose"),
    )

    snapshot = tracer.snapshot()
    event = snapshot["events"][-1]
    assert event["status"] == "error"
    assert event["result_preview"] == "RuntimeError: tool execution failed"
    assert "do-not-expose" not in str(snapshot)


def test_retry_exhaustion_does_not_return_or_trace_exception_secrets():
    manager = MESAgentManager.__new__(MESAgentManager)
    manager._agent_max_attempts = 1
    manager._cancelled = threading.Event()
    manager.tracer = AgentTracer()

    def fail(_prompt):
        raise RuntimeError("credential=do-not-expose")

    reply = manager._call_agent_with_retry("monitor", fail, "query")
    snapshot = manager.tracer.snapshot()

    assert "do-not-expose" not in reply
    assert "do-not-expose" not in str(snapshot)


def test_burst_failure_is_generic_outside_server_logs():
    manager = MESAgentManager.__new__(MESAgentManager)
    manager._cancelled = threading.Event()
    manager._reset_conversations = lambda: None
    manager.tracer = AgentTracer()

    def fail(_prompt):
        raise RuntimeError("database-password=do-not-expose")

    manager.supervisor_agent = fail
    result = manager.investigate_detection_burst(
        machine_id=1,
        defect_type="scratches",
        detection_count=1,
        window_start="2026-07-31T00:00:00Z",
        window_end="2026-07-31T00:00:30Z",
    )

    assert result["status"] == "failed"
    assert result["report"] == "Investigation failed"
    assert "do-not-expose" not in str(result)
    assert "do-not-expose" not in str(manager.tracer.snapshot())


def test_trace_neutralizes_terminal_sequences_in_model_and_query_text(capsys):
    class Hooks:
        def add_hook(self, _hook):
            pass

    class Agent:
        callback_handler = object()
        hooks = Hooks()

    tracer = AgentTracer()
    agent = Agent()
    attach_tracer(agent, "Monitor", tracer)

    agent.callback_handler(
        data="\x1b[31manswer\x1b[0m\x1b]0;spoofed\x07",
        complete=True,
    )
    tracer.log_query(
        "SELECT '\x1b[2Jvalue'",
        {"value": "\x9b31mparameter\x9b0m"},
        {"success": False, "error": "\x1b]8;;https://evil.invalid\x07failure"},
    )

    snapshot = tracer.snapshot()
    assert capsys.readouterr().out == ""
    assert "answer" in str(snapshot)
    assert "parameter" in str(snapshot)
    assert "failure" in str(snapshot)
    assert "\x1b" not in str(snapshot)
    assert "\x9b" not in str(snapshot)


@pytest.mark.parametrize("relative_path", ["chat_agent.py", "strands_agent.py"])
def test_every_strands_agent_explicitly_disables_default_stdout_callback(
    relative_path,
):
    tree = ast.parse((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
    agent_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Agent"
    ]

    assert agent_calls
    for call in agent_calls:
        callback = next(
            (
                keyword.value
                for keyword in call.keywords
                if keyword.arg == "callback_handler"
            ),
            None,
        )
        assert isinstance(callback, ast.Constant)
        assert callback.value is None
