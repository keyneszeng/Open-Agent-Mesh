"""v0.5 stream continuity, auth boundaries, SSE interop and fencing tests."""
import json
import time
from pathlib import Path

import pytest
import httpx
from fastapi.testclient import TestClient

from agentmesh.app import create_app
from agentmesh.store import Store
from agentmesh.remote_stream import run_a2a_stream, RemoteStreamError
from agentmesh.config import AgentSpec
from agentmesh.runner import run_agent, AgentExecutionError


def auth(who="alice"):
    return {"Authorization": "Bearer " + {"alice":"alice-token-123456",
            "bob":"bob-token-123456", "admin":"admin-token-123456"}[who]}


@pytest.fixture
def mesh(tmp_path):
    config = tmp_path / "agents.json"
    config.write_text(json.dumps({"agents":[
        {"id":"echo", "kind":"python", "handler":"agentmesh.builtins:echo"},
        {"id":"upper", "kind":"python", "handler":"agentmesh.builtins:uppercase"},
        {"id":"gated", "kind":"python", "handler":"agentmesh.builtins:echo", "requires_approval":True},
    ]}))
    tokens = {
        "alice-token-123456":{"name":"alice","allowed_agents":["echo","upper","gated"]},
        "bob-token-123456":{"name":"bob","allowed_agents":["echo","upper"]},
        "admin-token-123456":{"name":"admin","role":"admin","allowed_agents":["*"]},
    }
    app = create_app(config_path=str(config), db_path=str(tmp_path / "mesh.sqlite"), tokens=tokens)
    with TestClient(app) as client:
        yield client,app


def make_chat(client):
    response=client.post('/v1/conversations',headers=auth(),json={'title':'Live chat','participants':['echo','upper']})
    assert response.status_code == 201,response.text
    return response.json()['id']


def frames(text):
    return [(line.split(': ',1)[0], json.loads(line.split(': ',1)[1])) for line in text.splitlines() if line.startswith('data: ')]


def test_replay_stream_after_close(mesh):
    client, app = mesh
    cid=make_chat(client)
    assert client.post(f'/v1/conversations/{cid}/messages',headers=auth(),json={'to':'upper','text':'alpha'}).status_code==202
    deadline=time.monotonic()+5
    while time.monotonic()<deadline and len(app.state.conversations.messages(cid))<2:
        time.sleep(.02)
    assert len(app.state.conversations.messages(cid))==2
    assert client.post(f'/v1/conversations/{cid}/close',headers=auth()).status_code==200
    res=client.get(f'/v1/conversations/{cid}/stream',headers=auth())
    assert res.status_code == 200 and 'text/event-stream' in res.headers['content-type']
    events=frames(res.text)
    assert len(events) >= 5
    ids=[int(line[4:]) for line in res.text.splitlines() if line.startswith('id: ')]
    assert ids==sorted(set(ids))
    assert any(x[1]['kind']=='message' and x[1]['data']['content']=='ALPHA' for x in events)
    assert events[-1][1]['data']['kind']=='closed'
    after=ids[2]
    replay=client.get(f'/v1/conversations/{cid}/stream',headers={**auth(), 'Last-Event-ID':str(after)})
    replay_ids=[int(line[4:]) for line in replay.text.splitlines() if line.startswith('id: ')]
    assert replay_ids == ids[3:]
    assert client.get(f'/v1/conversations/{cid}/stream?after={after}',headers=auth()).status_code==200


def test_stream_auth_cursor_and_budget(mesh):
    client,app=mesh
    cid=make_chat(client)
    assert client.get(f'/v1/conversations/{cid}/stream',headers=auth('bob')).status_code==404
    assert client.get(f'/v1/conversations/{cid}/stream').status_code==401
    assert client.get(f'/v1/conversations/{cid}/stream',headers=auth(),params={'after':-1}).status_code==422
    assert client.get(f'/v1/conversations/{cid}/stream',headers={**auth(),'Last-Event-ID':'abc'}).status_code==422
    assert client.get(f'/v1/conversations/{cid}/stream',headers=auth(),params={'wait_seconds':100}).status_code==422
    res=client.get(f'/v1/conversations/{cid}/stream?wait_seconds=0.2',headers=auth())
    assert res.status_code==200
    assert len(frames(res.text))>=1
    assert res.headers['x-accel-buffering']=='no'
    assert client.post('/v1/conversations',headers=auth(),json={'title':'test','participants':['gated']}).status_code==403


def test_a2a_send_streaming_and_terminal_subscribe(mesh):
    client, app=mesh
    payload={'jsonrpc':'2.0','id':41,'method':'SendStreamingMessage',
             'params':{'message':{'messageId':'stream-one','role':'ROLE_USER','parts':[{'text':'hello stream'}]}}}
    res=client.post('/a2a/upper',headers={**auth(),'A2A-Version':'1.0'},json=payload)
    assert res.status_code==200,res.text
    assert 'text/event-stream' in res.headers['content-type']
    objects=[x for k,x in frames(res.text)]
    assert all(x['id']==41 for x in objects)
    assert 'task' in objects[0]['result']
    assert objects[-1]['result']['statusUpdate']['final'] is True
    assert objects[-1]['result']['statusUpdate']['status']['state']=='TASK_STATE_COMPLETED'
    task_id=objects[0]['result']['task']['id']
    sub=client.post('/a2a/upper',headers={**auth(),'A2A-Version':'1.0'},json={
        'jsonrpc':'2.0','id':42,'method':'SubscribeToTask','params':{'id':task_id}})
    assert sub.json()['error']['code']==-32602
    assert client.post('/a2a/upper',headers={**auth('bob'),'A2A-Version':'1.0'},json={
        'jsonrpc':'2.0','id':42,'method':'SubscribeToTask','params':{'id':task_id}}).json()['error']['code']==-32004


def test_fenced_delta_store_and_quota(tmp_path):
    store=Store(str(tmp_path/'fence.db'))
    task,_=store.create(agent_id='a',owner='owner',input_value='x',context={},request_key=None,parent_id=None,max_depth=5)
    running=store.claim(lease_seconds=3)
    assert store.append_delta(task['id'],running['lease_token'],'part 1')
    assert not store.append_delta(task['id'],'other-token','bad')
    assert store.deltas(task['id'])[0]['content']=='part 1'
    with pytest.raises(ValueError):
        store.append_delta(task['id'],running['lease_token'],'x'*4097)
    assert store.finish(task['id'],'completed',output='yes',lease_token=running['lease_token'])
    assert not store.append_delta(task['id'],running['lease_token'],'late')
    assert len(store.deltas(task['id']))==1


def test_conversation_delta_rejects_spoofed_lease(mesh):
    client,app=mesh
    cid=make_chat(client)
    engine=app.state.conversations
    assert not engine.emit_delta(cid,'not-a-task','fake','sensitive text')
    assert engine.stream_events(cid)[0]['kind']=='state'


def _mock_client(monkeypatch, module, handler):
    original=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs: original(transport=httpx.MockTransport(handler),timeout=kwargs['timeout']))


@pytest.mark.asyncio
async def test_http_sse_adapter_incremental(monkeypatch):
    from agentmesh import runner
    def handler(request):
        assert request.headers['accept']=='text/event-stream'
        assert request.headers['idempotency-key']=='id-1'
        content=('event: delta\ndata: {"text":"甲"}\n\nevent: delta\ndata: {"text":"乙"}\n\nevent: done\ndata: {"output":"甲乙"}\n\n').encode()
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=content)
    _mock_client(monkeypatch,runner,handler)
    result=[]
    async def on_delta(s): result.append(s)
    spec=AgentSpec(id='sse',kind='http_sse',name='sse',description='',skills=(),url='http://internal/invoke')
    assert await run_agent(spec,task_id='id-1',value='请求',context={},parent_id=None,on_delta=on_delta)=='甲乙'
    assert result == ['甲','乙']


@pytest.mark.asyncio
async def test_http_sse_adapter_truncation_fails(monkeypatch):
    from agentmesh import runner
    def handler(request):
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=b'event: delta\ndata: {"text":"unfinished"}\n\n')
    _mock_client(monkeypatch,runner,handler)
    spec=AgentSpec(id='sse',kind='http_sse',name='sse',description='',skills=(),url='http://internal/invoke')
    with pytest.raises(AgentExecutionError,match='without done'):
        await run_agent(spec,task_id='id-2',value='x',context={},parent_id=None)


@pytest.mark.asyncio
async def test_remote_a2a_stream_adapter_and_mismatch():
    def good(request):
        payload=json.loads(request.content)
        assert payload['method']=='SendStreamingMessage'
        content=('data: '+json.dumps({'jsonrpc':'2.0','id':'task-id','result':{'task':{'id':'remote-id','status':{'state':'TASK_STATE_WORKING'}}}})+'\n\n'
                 + 'data: '+json.dumps({'jsonrpc':'2.0','id':'task-id','result':{'artifactUpdate':{'artifact':{'parts':[{'text':'hello'}]},'append':False,'lastChunk':False}}})+'\n\n'
                 + 'data: '+json.dumps({'jsonrpc':'2.0','id':'task-id','result':{'statusUpdate':{'status':{'state':'TASK_STATE_COMPLETED'},'final':True}}})+'\n\n')
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=content.encode())
    events=[]
    async def cb(s):events.append(s)
    async with httpx.AsyncClient(transport=httpx.MockTransport(good)) as client:
        result=await run_a2a_stream(client,'http://a2a/',{},'task-id','input',{'_a2a_context_id':'abc'},cb)
        assert result == 'hello' and events==['hello']
    def wrong(request):
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=b'data: {"jsonrpc":"2.0","id":"WRONG","result":{}}\n\n')
    async with httpx.AsyncClient(transport=httpx.MockTransport(wrong)) as client:
        with pytest.raises(RemoteStreamError,match='id mismatch'):
            await run_a2a_stream(client,'http://a2a/',{},'task-id','input',{},None)
