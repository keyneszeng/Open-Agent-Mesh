"""A simple human-authored coordinator: demonstrates rule versioning and dependent task delegation."""
import os
import urllib.error
from agentmesh.client import MeshClient

mesh = MeshClient(os.environ.get('MESH_URL', 'http://127.0.0.1:8765'), os.environ['MESH_TOKEN'])
project = 'demo-project'
key = 'expense-approval-rule'
rule = {'decision': '核销通过后再请付款', 'source': 'human-approved project decision'}

try:
    current = mesh.fact(project, key)
    revision = current['revision']
except urllib.error.HTTPError as exc:
    if exc.code != 404:
        raise
    revision = 0

if revision == 0 or current['value'] != rule:
    updated = mesh.put_fact(project, key, rule, expected_revision=revision)
    print('Shared project knowledge updated to revision', updated['revision'])

print('Shared decisions:', mesh.facts(project))
first = mesh.submit('uppercase', 'coordinate a task across agents')
first_result = mesh.wait(first['id'])
assert first_result['state'] == 'completed', first_result
second = mesh.submit('echo', {'first_agent_output': first_result['output'], 'rule': rule},
                     parent_task_id=first['id'], context={'project_id': project})
second_result = mesh.wait(second['id'])
assert second_result['state'] == 'completed', second_result
print('Coordinator completed two dependent agent tasks:', second_result['output'])
