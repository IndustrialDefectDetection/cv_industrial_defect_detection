from strands import Agent, tool
from agent_tracer import attach_tracer

SYSTEM_PROMPT = """
You are the conversational interface for an MES analysis system.

Answer greetings and general questions directly.

When a question requires manufacturing data, defect analysis,
maintenance correlation, root-cause analysis, or action planning,
call ask_mes_supervisor.

Treat the supervisor's response as authoritative. Preserve its exact
numbers, dates, machine names, findings, and certainty ratings.
Never invent MES results or claim that analysis ran when you did not
call the tool.
"""


def build_conversational_agent(model, supervisor_agent, tracer):
    @tool
    def ask_mes_supervisor(request: str) -> str:
        """Ask the MES supervisor to perform manufacturing analysis."""
        response = supervisor_agent(request)
        return response.message["content"][0]["text"]

    chat_agent = Agent(
        model=model,
        tools=[ask_mes_supervisor],
        system_prompt=SYSTEM_PROMPT
    )
    attach_tracer(chat_agent, "Chat", tracer)
    return chat_agent
