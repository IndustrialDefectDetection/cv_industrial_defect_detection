"""What one chat question is allowed to cost, and what it is allowed to break.

The frontend chat box reaches the MES agents through a chain the model
itself steers: the conversational agent decides when to call
ask_mes_supervisor, and each call is a full multi-agent workflow costing
minutes and real credit. These tests pin the limits that are enforced in
code rather than merely requested in a prompt.

No Anthropic calls: the supervisor is faked at the retry boundary.
"""
import pytest
from strands.types.exceptions import MaxTokensReachedException

from conftest import FakeResult, assistant, tool_result, tool_use, user


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
