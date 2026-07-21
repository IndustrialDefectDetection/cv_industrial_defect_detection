from fastapi import FastAPI
from pydantic import BaseModel
from app_factory.mes_agents.agent_manager import MESAgentManager

app = FastAPI()
agent_manager = MESAgentManager()
class ChatRequest(BaseModel):
    user_input: str

#Sends chat request to agent manaager
@app.post("/chat/")
async def send_message(message: ChatRequest):
    query = message.user_input
    response = await agent_manager.process_query(query)
    return response;
    

    