from fastapi import FastAPI
from pydantic import BaseModel
from strands_agent import MESAgentManager
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
# Allows Next.js to call API. Update origin if needed.
app.add_middleware(
    CORSMiddleware, 
    allow_origins=["http://localhost:3000"], 
    allow_methods=["POST"],
    allow_headers=["Content-Type"]
    )

agent_manager = MESAgentManager()
class ChatRequest(BaseModel):
    user_input: str

#Sends chat request to agent manaager
@app.post("/chat/")
def send_message(message: ChatRequest):
    query = message.user_input
    supervisor_agent = agent_manager.get_supervisor_agent()
    response = supervisor_agent(query)
    analysis_text = response.message["content"][0]["text"]
    return {"analysis" : analysis_text}
    

    