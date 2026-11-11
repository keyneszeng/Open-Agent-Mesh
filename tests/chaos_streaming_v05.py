"""Kill a streaming worker mid-token; replay provisional deltas and recover one final turn."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
PORT, LIVE=19914,19915
TOKEN='mesh-stream-chaos-token-123456'
SECRET='mesh-stream-chaos-upstream-123456'
URL=f'http://127.0.0.1:{PORT}'


def get(path, body=None):
    req=urllib.request.Request(URL+path,method='GET' if body is None else 'POST',
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=6) as response:
        return json.load(response)


def online():
    for _ in range(150):
        try:
            if get('/health')['ok']:return
        except Exception:time.sleep(.075)
    raise AssertionError('Gateway not ready')

with tempfile.TemporaryDirectory() as folder:
    tmp=Path(folder)
    config=tmp/'agents.json'
    config.write_text(json.dumps({'agents':[{'id':'live','kind':'http_sse',
        'url':f'http://127.0.0.1:{LIVE}/invoke','token_env':'MESH_STREAM_AGENT_TOKEN','timeout_seconds':10}]}))
    db_path=tmp/'mesh.sqlite'
    env=dict(os.environ,PYTHONPATH=str(ROOT/'src'),MESH_PORT=str(PORT),
        MESH_AGENTS_CONFIG=str(config),MESH_DB_PATH=str(db_path),MESH_LEASE_SECONDS='2',
        MESH_TOKENS=json.dumps({TOKEN:{'name':'tester','role':'admin','allowed_agents':['*']}}),
        MESH_STREAM_AGENT_TOKEN=SECRET)
    upstream=subprocess.Popen([sys.executable,'-m','uvicorn','examples.streaming_http_agent:app',
        '--host','127.0.0.1','--port',str(LIVE)],cwd=ROOT,env=env,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    gateway=None
    def start_gateway():
        return subprocess.Popen([sys.executable,'-m','agentmesh.cli'],cwd=ROOT,env=env,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        gateway=start_gateway()
        online()
        cid=get('/v1/conversations',{'title':'Crash stream','participants':['live'],'max_turns':2})['id']
        get(f'/v1/conversations/{cid}/messages',{'to':'live','text':'recover tokens'})
        end=time.monotonic()+8
        while time.monotonic()<end:
            with sqlite3.connect(db_path) as db:
                deltas=db.execute('SELECT COUNT(*) FROM conversation_stream WHERE conversation_id=? AND kind=?',(cid,'delta')).fetchone()[0]
                working=db.execute('SELECT state FROM conversation_turns WHERE conversation_id=?',(cid,)).fetchone()[0]
            if deltas>=1 and working=='submitted':break
            time.sleep(.03)
        else:raise AssertionError('No token delta before killing worker')
        gateway.kill();gateway.wait(timeout=3)
        gateway=start_gateway();online()
        deadline=time.monotonic()+14
        while time.monotonic()<deadline:
            conversation=get(f'/v1/conversations/{cid}')
            if conversation['turns'][0]['state']=='completed':break
            time.sleep(.11)
        else:raise AssertionError('Streaming task did not recover')
        tid=conversation['turns'][0]['task_id']
        task=get(f'/v1/tasks/{tid}')
        messages=get(f'/v1/conversations/{cid}/messages')
        assert task['attempts']>=2,task
        assert len(messages)==2,messages
        assert messages[1]['content']=='Streamed: recover tokens',messages
        with sqlite3.connect(db_path) as db:
            seen=[json.loads(r[0])['attempt'] for r in db.execute(
                'SELECT payload_json FROM conversation_stream WHERE conversation_id=? AND kind=? ORDER BY seq',(cid,'delta'))]
        assert 1 in seen and 2 in seen,seen
        print(json.dumps({'status':'PASS','sigkill_recovery':True,'attempts':task['attempts'],
                          'delta_attempts':seen,'final_replies':len(messages)-1}))
    finally:
        for p in (gateway,upstream):
            if p and p.poll() is None:p.terminate()
        for p in (gateway,upstream):
            if p:
                try:p.wait(timeout=4)
                except subprocess.TimeoutExpired:p.kill();p.wait(timeout=3)
