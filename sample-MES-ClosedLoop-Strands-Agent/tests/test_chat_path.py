"""What one chat question is allowed to cost, and what it is allowed to break.

The frontend chat box reaches the MES agents through a chain the model
itself steers: the conversational agent decides when to call
ask_mes_supervisor, and each call is a full multi-agent workflow costing
minutes and real credit. These tests pin the limits that are enforced in
code rather than merely requested in a prompt.

No Anthropic calls: the supervisor is faked at the retry boundary.
"""
import threading

import pytest
from strands.hooks import (
    AfterInvocationEvent,
    BeforeInvocationEvent,
    HookRegistry,
)
from strands.types.exceptions import MaxTokensReachedException

import strands_agent
from conftest import FakeResult, assistant, tool_result, tool_use, user
from strands_agent import MESAgentManager, RunCancelled


def init_cancellation_state(manager):
    manager._cancelled = threading.Event()
    manager._chat_running = threading.Event()
    manager._active_chat_agent = None
    manager._active_agents_lock = threading.RLock()
    manager._active_agent_invocations = {}


def test_persisted_history_replaces_stale_process_state():
    manager = object.__new__(MESAgentManager)
    manager.conversational_agent = type(
        "ChatAgent",
        (),
        {"messages": [user("stale chat")]},
    )()
    reset_calls = []
    manager._reset_conversations = lambda: reset_calls.append(True)
    persisted_history = [
        {"role": "user", "content": "maintenance correlation"},
        {"role": "assistant", "content": "Which machines?"},
    ]

    manager.load_chat_history(persisted_history)

    assert reset_calls == [True]
    assert manager.conversational_agent.messages == [
        user("maintenance correlation"),
        assistant("Which machines?"),
    ]

    persisted_history[0]["content"] = "mutated after hydration"
    assert manager.conversational_agent.messages[0] == user(
        "maintenance correlation",
    )

    manager.load_chat_history([])
    assert manager.conversational_agent.messages == []


def test_invalid_persisted_history_cannot_leave_stale_chat_state():
    manager = object.__new__(MESAgentManager)
    manager.conversational_agent = type(
        "ChatAgent",
        (),
        {"messages": [user("stale chat")]},
    )()
    manager._reset_conversations = lambda: None

    with pytest.raises(ValueError):
        manager.load_chat_history([
            {"role": "system", "content": "not allowed"},
        ])

    assert manager.conversational_agent.messages == []

    with pytest.raises(ValueError):
        manager.load_chat_history([
            {"role": "user", "content": "dangling question"},
        ])


def test_each_chat_run_builds_a_fresh_agent(monkeypatch):
    built_agents = []

    class FakeChatAgent:
        def __init__(self):
            self.messages = []
            self.received_history = None

        def __call__(self, query):
            self.received_history = list(self.messages)
            return type(
                "ChatResult",
                (),
                {
                    "stop_reason": "end_turn",
                    "message": {"content": [{"text": query}]},
                },
            )()

    def fake_builder(model, supervisor, tracer, call_supervisor=None):
        agent = FakeChatAgent()
        built_agents.append(agent)
        return agent

    monkeypatch.setattr(
        strands_agent,
        "build_conversational_agent",
        fake_builder,
    )
    manager = object.__new__(MESAgentManager)
    manager.model = object()
    manager.supervisor_agent = object()
    manager.tracer = object()
    manager.conversational_agent = FakeChatAgent()
    init_cancellation_state(manager)
    manager._chat_history_limit = 12
    manager._chat_supervisor_calls = 0
    manager._reset_conversations = lambda: None
    manager._call_supervisor_for_chat = lambda request: request
    manager._track_cancellable_agent = lambda agent: None

    manager.run_chat("continue A", [
        {"role": "user", "content": "question A"},
        {"role": "assistant", "content": "answer A"},
    ])
    manager.run_chat("start B", [])

    assert len(built_agents) == 2
    assert built_agents[0] is not built_agents[1]
    assert built_agents[0].received_history == [
        user("question A"),
        assistant("answer A"),
    ]
    assert built_agents[1].received_history == []
    assert manager._active_chat_agent is None
    assert not manager._chat_running.is_set()


def test_chat_failure_clears_the_request_local_agent(monkeypatch):
    class FailingChatAgent:
        messages = []

        def __call__(self, query):
            raise RuntimeError("model failure")

    monkeypatch.setattr(
        strands_agent,
        "build_conversational_agent",
        lambda *args, **kwargs: FailingChatAgent(),
    )
    manager = object.__new__(MESAgentManager)
    manager.model = object()
    manager.supervisor_agent = object()
    manager.tracer = object()
    manager.conversational_agent = FailingChatAgent()
    init_cancellation_state(manager)
    manager._chat_history_limit = 12
    manager._chat_supervisor_calls = 0
    reset_calls = []
    manager._reset_conversations = lambda: reset_calls.append(True)
    manager._call_supervisor_for_chat = lambda request: request
    manager._track_cancellable_agent = lambda agent: None

    with pytest.raises(RuntimeError, match="model failure"):
        manager.run_chat("fail", [])

    assert reset_calls == [True, True]
    assert manager._active_chat_agent is None
    assert not manager._chat_running.is_set()


def test_cancel_targets_every_active_nested_agent_and_no_idle_agent():
    class CancellableAgent:
        def __init__(self):
            self.hooks = HookRegistry()
            self.cancel_calls = 0

        def cancel(self):
            self.cancel_calls += 1

    manager = object.__new__(MESAgentManager)
    init_cancellation_state(manager)
    chat_agent = CancellableAgent()
    supervisor_agent = CancellableAgent()
    analyzer_agent = CancellableAgent()
    idle_monitor_agent = CancellableAgent()
    idle_planner_agent = CancellableAgent()
    agents = [
        chat_agent,
        supervisor_agent,
        analyzer_agent,
        idle_monitor_agent,
        idle_planner_agent,
    ]
    for agent in agents:
        manager._track_cancellable_agent(agent)

    active_agents = [chat_agent, supervisor_agent, analyzer_agent]
    for agent in active_agents:
        agent.hooks.invoke_callbacks(BeforeInvocationEvent(agent=agent))

    manager.cancel()

    assert manager._cancelled.is_set()
    assert [agent.cancel_calls for agent in active_agents] == [1, 1, 1]
    assert idle_monitor_agent.cancel_calls == 0
    assert idle_planner_agent.cancel_calls == 0

    for agent in active_agents:
        agent.hooks.invoke_callbacks(AfterInvocationEvent(agent=agent))
    assert manager._active_agent_invocations == {}


def test_invocation_start_after_cancel_is_denied_without_poisoning_agent():
    class CancellableAgent:
        def __init__(self):
            self.hooks = HookRegistry()
            self.cancel_calls = 0

        def cancel(self):
            self.cancel_calls += 1

    manager = object.__new__(MESAgentManager)
    init_cancellation_state(manager)
    agent = CancellableAgent()
    manager._track_cancellable_agent(agent)
    manager._cancelled.set()

    before_event = BeforeInvocationEvent(agent=agent)
    agent.hooks.invoke_callbacks(before_event)

    assert before_event.cancel == "Investigation cancelled by the user"
    assert agent.cancel_calls == 0
    agent.hooks.invoke_callbacks(AfterInvocationEvent(agent=agent))
    assert manager._active_agent_invocations == {}


def test_cancel_during_hydration_stops_before_the_model_call(monkeypatch):
    class FakeChatAgent:
        def __init__(self):
            self.messages = []
            self.called = False

        def __call__(self, query):
            self.called = True
            raise AssertionError("cancelled turn reached the model")

    chat_agent = FakeChatAgent()
    monkeypatch.setattr(
        strands_agent,
        "build_conversational_agent",
        lambda *args, **kwargs: chat_agent,
    )
    manager = object.__new__(MESAgentManager)
    manager.model = object()
    manager.supervisor_agent = object()
    manager.tracer = object()
    manager.conversational_agent = FakeChatAgent()
    init_cancellation_state(manager)
    manager._chat_history_limit = 12
    manager._chat_supervisor_calls = 0
    manager._reset_conversations = lambda: None
    manager._call_supervisor_for_chat = lambda request: request
    manager._track_cancellable_agent = lambda agent: None

    def cancel_while_loading(history, active_chat_agent):
        manager._cancelled.set()

    manager.load_chat_history = cancel_while_loading

    with pytest.raises(RunCancelled):
        manager.run_chat("do not run", [])

    assert chat_agent.called is False
    assert manager._active_chat_agent is None
    assert not manager._chat_running.is_set()


def test_cancel_after_request_preparation_is_not_cleared_by_run_chat(
    monkeypatch,
):
    class FakeChatAgent:
        def __init__(self):
            self.messages = []
            self.called = False

        def __call__(self, query):
            self.called = True
            raise AssertionError("cancelled turn reached the model")

    chat_agent = FakeChatAgent()
    monkeypatch.setattr(
        strands_agent,
        "build_conversational_agent",
        lambda *args, **kwargs: chat_agent,
    )
    manager = object.__new__(MESAgentManager)
    manager.model = object()
    manager.supervisor_agent = object()
    manager.tracer = object()
    manager.conversational_agent = FakeChatAgent()
    init_cancellation_state(manager)
    manager._chat_history_limit = 12
    manager._chat_supervisor_calls = 3
    manager._reset_conversations = lambda: None
    manager._call_supervisor_for_chat = lambda request: request
    manager._track_cancellable_agent = lambda agent: None

    manager.prepare_chat_request()
    assert manager._chat_supervisor_calls == 0
    manager.cancel()

    with pytest.raises(RunCancelled):
        manager.run_chat("do not run", [])

    assert manager._cancelled.is_set()
    assert chat_agent.called is False
    assert manager._active_chat_agent is None
    assert not manager._chat_running.is_set()


# --------------------------------------------------------------------------
# Retry budget
# --------------------------------------------------------------------------

def test_attempt_budget_comes_from_the_environment(make_manager):
    manager = make_manager(MES_AGENT_MAX_ATTEMPTS=4)
    attempts = []

    def always_over_limit(prompt):
        attempts.append(prompt)
        raise MaxTokensReachedException("output limit")

    reply = manager._call_agent_with_retry("planner", always_over_limit, "plan")

    assert len(attempts) == 4
    assert "after 4 attempts" in reply


def test_attempt_budget_is_clamped(make_manager):
    """Each attempt is a billed call, so the ceiling is not negotiable."""
    assert make_manager(MES_AGENT_MAX_ATTEMPTS=99)._agent_max_attempts == 5
    assert make_manager(MES_AGENT_MAX_ATTEMPTS=0)._agent_max_attempts == 1


def test_exhausted_agent_returns_a_sentence_not_an_exception(make_manager):
    """The supervisor is told to report this as a gap; a raise would instead
    reach it as a tool error to improvise around."""
    manager = make_manager()

    def always_fails(prompt):
        raise RuntimeError("connection reset")

    reply = manager._call_agent_with_retry("monitor", always_fails, "look")

    assert isinstance(reply, str)
    assert "unavailable" in reply and "state this gap explicitly" in reply


def test_cancelled_agent_result_never_starts_a_retry(make_manager):
    manager = make_manager(MES_AGENT_MAX_ATTEMPTS=3)
    attempts = []

    def cancelled_during_first_attempt(prompt):
        attempts.append(prompt)
        manager._cancelled.set()
        return type("Result", (), {"stop_reason": "cancelled"})()

    with pytest.raises(RunCancelled):
        manager._call_agent_with_retry(
            "analyzer",
            cancelled_during_first_attempt,
            "analyze",
        )

    assert attempts == ["analyze"]


# --------------------------------------------------------------------------
# Delegating from chat to the supervisor
# --------------------------------------------------------------------------

def test_delegating_resets_the_workflow_agents(make_manager):
    """Without this, the third question of a session re-reads the first two
    answers' entire transcripts, tool results included."""
    manager = make_manager()
    manager.supervisor_agent.messages.append(user("an earlier question"))
    manager.monitor_agent.messages.append(user("stale context"))

    # Patch the retry layer, NOT supervisor_agent: replacing the agent object
    # would mean _reset_conversations clears the replacement instead, and this
    # test would pass while proving nothing.
    manager._call_agent_with_retry = lambda name, agent, prompt: FakeResult()
    text = manager._call_supervisor_for_chat("which machine?")

    assert text == "supervisor findings"
    assert manager.supervisor_agent.messages == []
    assert manager.monitor_agent.messages == []


def test_unreadable_supervisor_reply_degrades_to_a_gap(make_manager):
    """A reply whose first block is not text must not raise inside the tool."""
    manager = make_manager()
    garbled = type("Garbled", (), {"message": {"content": [{"toolUse": {}}]}})()
    manager._call_agent_with_retry = lambda name, agent, prompt: garbled

    assert "no readable text" in manager._call_supervisor_for_chat("q")


def test_one_question_buys_one_workflow(make_manager):
    """The model alone decides when to call ask_mes_supervisor and nothing in
    it stops the model deciding repeatedly, so the limit lives here."""
    manager = make_manager(MES_CHAT_SUPERVISOR_CALLS=1)
    workflows = []
    manager._call_agent_with_retry = lambda name, agent, prompt: (
        workflows.append(prompt) or FakeResult())

    manager.prepare_chat_turn()
    replies = [manager._call_supervisor_for_chat(f"q{i}") for i in range(4)]

    assert len(workflows) == 1, "the delegation budget was breached"
    assert replies[0] == "supervisor findings"
    for refused in replies[1:]:
        assert "Do not call this tool again" in refused
        # It must still tell the model how to produce a useful answer.
        assert "findings you already have" in refused


def test_the_budget_is_per_question_not_per_session(make_manager):
    manager = make_manager(MES_CHAT_SUPERVISOR_CALLS=1)
    workflows = []
    manager._call_agent_with_retry = lambda name, agent, prompt: (
        workflows.append(prompt) or FakeResult())

    for _ in range(3):
        manager.prepare_chat_turn()
        manager._call_supervisor_for_chat("a new question")

    assert len(workflows) == 3


def test_delegation_budget_is_clamped(make_manager):
    assert make_manager(MES_CHAT_SUPERVISOR_CALLS=50)._chat_supervisor_budget == 3
    assert make_manager(MES_CHAT_SUPERVISOR_CALLS=0)._chat_supervisor_budget == 1


# --------------------------------------------------------------------------
# Trimming the chat's own history
# --------------------------------------------------------------------------

@pytest.mark.parametrize("history", [
    pytest.param([user("q1"), assistant("a1"), user("q2"), assistant("a2"),
                  user("q3"), assistant("a3")], id="plain-turns"),
    pytest.param([user("q1"), tool_use("t1"), tool_result("t1"), assistant("a1"),
                  user("q2"), tool_use("t2"), tool_result("t2"), assistant("a2")],
                 id="tool-pair-spans-the-cut"),
    pytest.param([user("q1"), tool_use("t1"), tool_result("t1"),
                  tool_use("t2"), tool_result("t2")], id="nothing-safe-to-cut"),
])
def test_trimming_never_orphans_a_tool_result(make_manager, history):
    """A toolResult whose toolUse has been trimmed away is a 400 from the API,
    so when no safe cut point exists the history must be left alone."""
    manager = make_manager(MES_CHAT_HISTORY_MESSAGES=4)
    manager.conversational_agent.messages[:] = list(history)

    manager.prepare_chat_turn()
    kept = manager.conversational_agent.messages

    assert kept, "trimmed the entire conversation away"
    assert kept[0]["role"] == "user"
    assert not any("toolResult" in block for block in kept[0]["content"])
    assert len(kept) <= len(history)


def test_a_short_history_is_left_untouched(make_manager):
    manager = make_manager(MES_CHAT_HISTORY_MESSAGES=12)
    history = [user("q1"), assistant("a1")]
    manager.conversational_agent.messages[:] = list(history)

    manager.prepare_chat_turn()

    assert manager.conversational_agent.messages == history
