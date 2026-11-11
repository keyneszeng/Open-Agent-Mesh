import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENVS = dict(os.environ)
ENVS['PYTHONPATH'] = str(ROOT/'src')
TOKENS = {"an-actual-test-token-123456789": {"name": "integration", "role": "admin", "allowed_agents": ["*"]}}
ENVS['MESH_TOKENS'] = json.dumps(TOKENS)
ENVS['MESH_TOKEN'] = 'an-actual-test-token-123456789'
ENVS['REMOTE_BEARER_TOKEN'] = 'an-actual-test-token-123456789'
ENVS['MESH_HTTP_AGENT_TOKEN'] = 'http-agent-demo-authentication-token'
PORTS=[19841,19842,19843]
ps=[]

def start(cmd, **settings):
    env=dict(ENVS, **settings)
    ps.append(subprocess.Popen(cmd, env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))

def get(url):
    with urllib.request.urlopen(url, timeout=1) as f:
        return json.loads(f.read())

def await_ready(url, timeout=12):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        try:
            get(url)
            return
        except Exception:
            time.sleep(.08)
    raise RuntimeError(f'not ready {url}')

try:
    with tempfile.TemporaryDirectory() as td:
        other_config = {"agents": [{"id":"remote-uppercase","kind":"a2a_jsonrpc", "token_env":"REMOTE_BEARER_TOKEN", "url":f"http://127.0.0.1:{PORTS[0]}/a2a/uppercase", "name":"Remote", "description":"A2A remote agent", "skills":["transform"]},
                                    {"id":"webhook", "kind":"http_json", "token_env":"MESH_HTTP_AGENT_TOKEN", "url":f"http://127.0.0.1:{PORTS[2]}/invoke", "name":"Webhook", "description":"Webhook test", "skills":["webhook"]}]}
        remote_conf=ROOT/'agents.example.json'
        front_conf=pathlib.Path(td)/'front.json'
        front_conf.write_text(json.dumps(other_config))
        start([sys.executable,'-m','agentmesh.cli'], MESH_AGENTS_CONFIG=str(remote_conf),MESH_PORT=str(PORTS[0]),MESH_PUBLIC_URL=f'http://127.0.0.1:{PORTS[0]}',MESH_DB_PATH=str(pathlib.Path(td)/'remote.sqlite'))
        start([sys.executable,'-m','agentmesh.cli'], MESH_AGENTS_CONFIG=str(front_conf),MESH_PORT=str(PORTS[1]),MESH_PUBLIC_URL=f'http://127.0.0.1:{PORTS[1]}',MESH_DB_PATH=str(pathlib.Path(td)/'front.sqlite'))
        start([sys.executable,'-m','uvicorn','examples.http_agent:app','--host','127.0.0.1','--port',str(PORTS[2])])
        await_ready(f'http://127.0.0.1:{PORTS[0]}/health')
        await_ready(f'http://127.0.0.1:{PORTS[1]}/health')
        await_ready(f'http://127.0.0.1:{PORTS[2]}/openapi.json')
        from agentmesh.client import MeshClient
        client=MeshClient(f'http://127.0.0.1:{PORTS[1]}', 'an-actual-test-token-123456789')
        a = client.run('remote-uppercase', 'live A2A transfer',seconds=15)
        b = client.run('webhook', 'live HTTP agent',seconds=15)
        assert a=='LIVE A2A TRANSFER',a
        assert b=='processed by external agent: live HTTP agent',b
        import shutil
        node_result = 'SKIPPED (node not installed)'
        if shutil.which('node'):
            cmd = subprocess.run(['node',str(ROOT/'examples'/'node_client.mjs')], env=dict(ENVS, MESH_URL=f'http://127.0.0.1:{PORTS[0]}'), cwd=str(ROOT), capture_output=True, text=True, timeout=12)
            assert cmd.returncode==0, cmd.stderr
            assert 'HELLO FROM JS' in cmd.stdout, cmd.stdout
            node_result = 'PASS' 
        skill = subprocess.run([sys.executable,str(ROOT/'skills'/'open-agent-mesh'/'scripts'/'mesh.py'), 'agents'], env=dict(ENVS, MESH_URL=f'http://127.0.0.1:{PORTS[1]}'), cwd=str(ROOT), capture_output=True, text=True, timeout=12)
        assert skill.returncode==0, skill.stderr
        assert 'remote-uppercase' in skill.stdout
        coordinator = subprocess.run([sys.executable, str(ROOT/'examples'/'coordinator.py')],
            env=dict(ENVS, MESH_URL=f'http://127.0.0.1:{PORTS[0]}'), cwd=str(ROOT), capture_output=True, text=True, timeout=12)
        assert coordinator.returncode == 0, coordinator.stderr
        assert 'Coordinator completed two dependent agent tasks' in coordinator.stdout
        print(json.dumps({'coordinator_project_memory':'PASS', 'a2a_proxy_to_second_process':a,'http_json_to_third_process':b,'node_client':node_result, 'agent_skill_cli':'PASS','result':'PASS'},ensure_ascii=False))
finally:
    for p in ps:
        p.terminate()
    for p in ps:
        try:p.communicate(timeout=4)
        except subprocess.TimeoutExpired:p.kill();p.communicate()
