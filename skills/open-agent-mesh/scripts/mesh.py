#!/usr/bin/env python3
"""Agent Skills portable CLI: Python standard library only, no project package import needed."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def request(path, method='GET', payload=None):
    url = os.environ.get('MESH_URL', 'http://127.0.0.1:8765').rstrip('/')
    token = os.environ.get('MESH_TOKEN')
    if not token:
        sys.exit('MESH_TOKEN is not set. Obtain an authorized token via your administrator.')
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(url + path, data=body, method=method,
                                 headers={'Authorization': 'Bearer ' + token,
                                          'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        sys.exit(f'Agent Mesh API error: HTTP {e.code} (check server permissions and payload)')
    except urllib.error.URLError as e:
        sys.exit(f'Could not reach Agent Mesh server: {e.reason}')


def segment(value):
    return urllib.parse.quote(value, safe='')


def main():
    parser = argparse.ArgumentParser(description='Open Agent Mesh portable skill CLI')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('agents')
    send = commands.add_parser('send')
    send.add_argument('agent')
    send.add_argument('message')
    send.add_argument('--parent', default=None)
    get = commands.add_parser('get')
    get.add_argument('task_id')
    flow = commands.add_parser('workflow')
    flow.add_argument('spec_file', help='JSON file containing steps and optional project_id')
    flow_get = commands.add_parser('workflow-get')
    flow_get.add_argument('workflow_id')
    flow_cancel = commands.add_parser('workflow-cancel')
    flow_cancel.add_argument('workflow_id')
    facts = commands.add_parser('facts')
    facts.add_argument('project')
    put = commands.add_parser('put-fact')
    put.add_argument('project')
    put.add_argument('key')
    put.add_argument('revision', type=int)
    put.add_argument('json_value', help='JSON value, e.g. a quoted JSON string')
    args = parser.parse_args()
    if args.command == 'agents':
        result = request('/v1/agents')
    elif args.command == 'send':
        result = request('/v1/tasks', 'POST', {'to': args.agent, 'input': args.message,
                                               'parent_task_id': args.parent})
    elif args.command == 'get':
        result = request('/v1/tasks/' + segment(args.task_id))
    elif args.command == 'workflow':
        with open(args.spec_file, encoding='utf-8') as spec:
            result = request('/v1/workflows', 'POST', json.load(spec))
    elif args.command == 'workflow-get':
        result = request('/v1/workflows/' + segment(args.workflow_id))
    elif args.command == 'workflow-cancel':
        result = request('/v1/workflows/' + segment(args.workflow_id) + '/cancel', 'POST')
    elif args.command == 'facts':
        result = request('/v1/projects/' + segment(args.project) + '/facts')
    else:
        try:
            value = json.loads(args.json_value)
        except json.JSONDecodeError:
            sys.exit('json_value must be a valid JSON value')
        result = request('/v1/projects/' + segment(args.project) + '/facts/' + segment(args.key),
                         'PUT', {'value': value, 'expected_revision': args.revision})
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
