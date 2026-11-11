"""Concurrent live HTTP SSE discussions with shared SQLite, no lost final messages."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
PORT,UPSTREAM=19916,19917
TOKEN='mesh-stress-stream-123456'
SECRET='mesh-stress-upstream-123456'
N=12


def wait_ready(port):
    for _ in range(150):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/openapi.json',timeout=.5):return
        except Exception:time.sleep(.07)
    raise RuntimeError('server not ready')

with tempfile.TemporaryDirectory() as directory:
    cfg=Path(directory)/'config.json'
    cfg.write_text(json.dumps({'agents':[{'id':'live','kind':'http_sse','url':f'http://127.0.0.1:{UPSTREAM}/invoke',
        'token_env':'MESH_STREAM_AGENT_TOKEN','timeout_seconds':12},
        {'id':'upper','kind':'python','handler':'agentmesh.builtins:uppercase'}]}))
    env=dict(os.environ,PYTHONPATH=str(ROOT/'src'),MESH_PORT=str(PORT),
        MESH_DB_PATH=str(Path(directory)/'mesh.sqlite'),MESH_AGENTS_CONFIG=str(cfg),
        MESH_TOKENS=json.dumps({TOKEN:{'name':'load','role':'admin','allowed_agents':['*']}}),
        MESH_STREAM_AGENT_TOKEN=SECRET)
    p=subprocess.Popen([sys.executable,'-m','agentmesh.cli'],cwd=ROOT,env=env,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    q=subprocess.Popen([sys.executable,'-m','uvicorn','examples.streaming_http_agent:app',
         '--host','127.0.0.1','--port',str(UPSTREAM)],cwd=ROOT,env=env,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        wait_ready(PORT);wait_ready(UPSTREAM)
        from agentmesh.client import MeshClient
        client=MeshClient(f'http://127.0.0.1:{PORT}',TOKEN)
        def create(i):
            cid=client.create_conversation(f'Load #{i}',['live','upper'],max_turns=2)['id']
            first=client.start_discussion(cid,f'hello {i}',rounds=2,idempotency_key=f'load-{i}')
            assert first['replayed'] is False
            return cid
        start=time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids=list(pool.map(create,range(N)))
        deadline=time.monotonic()+28
        while time.monotonic()<deadline:
            conversations=[client.conversation(cid) for cid in ids]
            if all(len(c['turns'])==2 and all(t['state']=='completed' for t in c['turns']) for c in conversations):break
            time.sleep(.1)
        else:raise AssertionError('Load failed: '+str([c['turns'] for c in conversations]))
        for i,cid in enumerate(ids):
            msg=client.conversation_messages(cid)
            assert len(msg)==3,msg
            assert msg[1]['content']==f'Streamed: hello {i}',msg
            assert msg[2]['content']==f'STREAMED: HELLO {i}',msg
            feed=list(client.conversation_stream(cid,wait_seconds=.15))
            assert len([e for e in feed if e['kind']=='delta'])==3
            assert len(set(e['seq'] for e in feed))==len(feed)
        print(json.dumps({'result':'PASS','conversations':N,'turns':2*N,
            'delta_events':3*N,'elapsed_seconds':round(time.monotonic()-start,2)}))
    finally:
        for x in (p,q):
            if x.poll() is None:x.terminate()
        for x in (p,q):
            try:x.wait(timeout=4)
            except subprocess.TimeoutExpired:x.kill();x.wait(timeout=3)
