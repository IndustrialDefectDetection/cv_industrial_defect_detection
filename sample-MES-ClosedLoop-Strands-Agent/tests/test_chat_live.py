"""The frontend chat box, exercised for real against the MES agents.

Opt-in because it spends real credit and takes several minutes:

    RUN_FULL=1 pytest tests/test_chat_live.py -s

Everything else in tests/ runs free. This is the test that would have caught
the emoji crash, where the agent's own output killed the run mid-stream.
"""
import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_FULL") != "1",
    reason="spends real API credit and takes ~10 minutes; set RUN_FULL=1",
)


@pytest.fixture(scope="module")
def chat(request):
    """One manager for the whole module: the turns build on each other."""
    import sys
    from pathlib import Path
    backend = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(backend))
    os.chdir(backend)
    os.environ.setdefault("MES_DB_BACKEND", "postgres")

    from agent_tracer import AgentTracer
    from strands_agent import MESAgentManager

    tracer = AgentTracer()
    manager = MESAgentManager(tracer=tracer)
    return manager, manager.get_conversational_agent(), tracer


def _delegated(tracer):
    """Did this turn actually reach the MES supervisor?"""
    return any("ask_mes_supervisor" in str(event)
               for event in tracer.snapshot(0)["events"])


def test_a_greeting_does_not_spend_a_workflow(chat):
    manager, agent, tracer = chat
    tracer.reset()
    tracer.run_start("Chat: greeting")
    manager.prepare_chat_turn()

    reply = agent("hi, what can you help me with?")

    assert reply.message["content"][0]["text"]
    assert not _delegated(tracer), "a greeting should never cost a five-agent run"


def test_a_data_question_reaches_the_mes_agents(chat):
    manager, agent, tracer = chat
    tracer.reset()
    tracer.run_start("Chat: data question")
    manager.prepare_chat_turn()

    start = time.time()
    reply = agent("Which machine has the most defects in the last 7 days?")
    text = reply.message["content"][0]["text"]
    print(f"\n  data question took {time.time() - start:.0f}s")

    assert _delegated(tracer), "the question never reached the supervisor"
    agents_seen = {e.get("agent") for e in tracer.snapshot(0)["events"] if e.get("agent")}
    assert "Supervisor" in agents_seen, agents_seen
    assert len(text) > 200


def test_a_follow_up_keeps_the_thread(chat):
    """The supervisor is reset before every delegation, so if the thread did
    not live in the chat agent, "what about" would be meaningless."""
    manager, agent, tracer = chat
    tracer.reset()
    tracer.run_start("Chat: follow-up")
    manager.prepare_chat_turn()

    reply = agent("And what about over the last 90 days instead?")
    text = reply.message["content"][0]["text"]

    assert len(text) > 200
    # It must have understood the subject without being told again.
    assert "90" in text
