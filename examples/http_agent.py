"""Example adapter for an arbitrary existing agent. Do not expose without authentication in production."""
import hmac
import os
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI()

class Work(BaseModel):
    task_id: str
    input: str
    context: dict = {}
    parent_task_id: str | None = None

@app.post('/invoke')
async def invoke(work: Work, authorization: str | None = Header(default=None)):
    expected = os.environ.get('MESH_HTTP_AGENT_TOKEN')
    if not expected or not authorization or not hmac.compare_digest(authorization, 'Bearer ' + expected):
        raise HTTPException(401, 'Outbound agent credential required')
    # Replace with your own framework call, e.g. await your_agent.run(work.input).
    return {'output': f'processed by external agent: {work.input}'}

# uvicorn examples.http_agent:app --port 8766
# Add to agents config (admin-controlled):
# {"id":"external", "kind":"http_json", "url":"http://127.0.0.1:8766/invoke", "token_env":"MESH_HTTP_AGENT_TOKEN", "name":"External",
#  "description":"Example webhook adapter", "skills":["external"]}
