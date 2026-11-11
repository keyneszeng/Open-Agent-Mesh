"""Real independent processes: token-first delivery, A2A streaming, replay and ACL."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import httpx

ROOT=Path(__file__).resolve().parents[1]
REMOTE, FRONT, LIVE=19911,19912,19913
TOKEN='mesh-live-network-token-123456'
AGENT_TOKEN='mesh-live-agent-token-123456'
processes=[]


def boot(command, **env_extra):
    env=dict(os.environ, PYTHONPATH=str(ROOT/'src'), MESH_TOKENS=json.dumps({TOKEN:{'name':'live-tester','role':'admin','allowed_agents':['*']}}),
             MESH_A2A_REMOTE_TOKEN=TOKEN,MESH_STREAM_AGENT_TOKEN=AGENT_TOKEN,**env_extra)
    p=subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    processes.append(p)
    return p


def ready(port):
    for _ in range(180):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/openapi.json', timeout=.5):
                return
        except Exception:
            time.sleep(.055)
    raise AssertionError(f'port {port} not ready')


with tempfile.TemporaryDirectory() as directory:
    tmp=Path(directory)
    remote_db=tmp/'remote.sqlite'
    front_cfg=tmp/'front.json'
    front_cfg.write_text(json.dumps({'agents':[
        {'id':'http-live','kind':'http_sse','url':f'http://127.0.0.1:{LIVE}/invoke',
         'token_env':'MESH_STREAM_AGENT_TOKEN','timeout_seconds':15},
        {'id':'a2a-live','kind':'a2a_stream','url':f'http://127.0.0.1:{REMOTE}/a2a/uppercase',
         'token_env':'MESH_A2A_REMOTE_TOKEN','timeout_seconds':15},
    ]}))
    try:
        boot([sys.executable,'-m','agentmesh.cli'],MESH_PORT=str(REMOTE),
             MESH_AGENTS_CONFIG=str(ROOT/'agents.example.json'),MESH_DB_PATH=str(remote_db))
        boot([sys.executable,'-m','agentmesh.cli'],MESH_PORT=str(FRONT),
             MESH_AGENTS_CONFIG=str(front_cfg),MESH_DB_PATH=str(tmp/'front.sqlite'))
        boot([sys.executable,'-m','uvicorn','examples.streaming_http_agent:app',
             '--host','127.0.0.1','--port',str(LIVE)])
        for port in (REMOTE,FRONT,LIVE):ready(port)
        from agentmesh.client import MeshClient
        caller=MeshClient(f'http://127.0.0.1:{FRONT}',TOKEN)
        cid=caller.create_conversation('Streaming cross-agent discussion',['http-live','a2a-live'],max_turns=4)['id']
        event_log=[]
        started=threading.Event()
        def listen():
            started.set()
            for event in caller.conversation_stream(cid,after=0,wait_seconds=5.0):
                event_log.append((time.monotonic(),event))
        subscriber=threading.Thread(target=listen,daemon=True)
        subscriber.start()
        started.wait(1)
        caller.start_discussion(cid,'now cooperate',rounds=4,idempotency_key='streaming-live')
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            conv=caller.conversation(cid)
            if len(conv['turns'])==4 and all(t['state']=='completed' for t in conv['turns']):
                break
            time.sleep(.09)
        else:raise AssertionError(f'Cross-platform conversation not completed {conv}')
        subscriber.join(timeout=8)
        assert not subscriber.is_alive(),'SSE subscriber hung'
        messages=caller.conversation_messages(cid)
        assert len(messages)==5,messages
        assert messages[1]['content']=='Streamed: now cooperate',messages
        assert messages[2]['content']=='STREAMED: NOW COOPERATE',messages
        deltas=[(t,e) for t,e in event_log if e['kind']=='delta']
        assert len(deltas)>=6,len(deltas)
        assert len({e['seq'] for _,e in event_log})==len(event_log)
        # First provisional delta must arrive while a turn is still executing.
        first_delta_time=deltas[0][0]
        final_message_time=next(t for t,e in event_log if e['kind']=='message' and e['data']['sender']=='agent:http-live')
        assert first_delta_time < final_message_time,(first_delta_time, final_message_time)
        # Last-Event-ID replay across a fresh connection, same conversation.
        # Cross-language client: Node 22 fetch() receives the same private SSE events.
        node_out=subprocess.check_output(["node",str(ROOT/'examples/node_stream_client.mjs'),cid],
            env=dict(os.environ,MESH_URL=f'http://127.0.0.1:{FRONT}',MESH_TOKEN=TOKEN),
            cwd=ROOT,timeout=10).decode().splitlines()
        assert len(node_out) >= 6,node_out
        assert all('seq' in json.loads(line) for line in node_out)
        from agentmesh.client import MeshClient
        after=event_log[-3][1]['seq']
        replay=list(caller.conversation_stream(cid,after=after,wait_seconds=.3))
        assert [r['seq'] for r in replay]==[e['seq'] for _,e in event_log[-2:]]
        # Direct A2A stream transports intermediate artifacts before terminal status.
        rpc={'jsonrpc':'2.0','id':'direct-live','method':'SendStreamingMessage',
             'params':{'message':{'messageId':'live-rpc-1','role':'ROLE_USER','parts':[{'text':'hi'}]}}}
        with httpx.Client(timeout=20) as http:
            with http.stream('POST',f'http://127.0.0.1:{FRONT}/a2a/http-live',
                             headers={'Authorization':'Bearer '+TOKEN,'A2A-Version':'1.0'},json=rpc) as response:
                assert response.status_code==200,response.status_code
                events=[json.loads(l[5:].strip()) for l in response.iter_lines() if l.startswith('data:')]
        assert 'task' in events[0]['result']
        assert sum('artifactUpdate' in e['result'] and not e['result']['artifactUpdate'].get('lastChunk') for e in events)>=3,events
        assert events[-1]['result']['statusUpdate']['final'] is True
        with sqlite3.connect(remote_db) as db:
            rows=[json.loads(x[0]).get('_a2a_context_id') for x in db.execute('SELECT context_json FROM tasks')]
        assert rows==[cid,cid],rows
        print(json.dumps({'status':'PASS','external_processes':3,'conversation_turns':4,
            'messages':len(messages),'stream_deltas':len(deltas),'real_time_before_completion':True,
            'a2a_stream_events':len(events),'resume':True,'context_propagation':True}))
    finally:
        for p in processes:
            if p.poll() is None:p.terminate()
        for p in processes:
            try:p.wait(timeout=4)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=3)
