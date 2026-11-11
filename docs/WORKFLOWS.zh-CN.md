# Open Agent Mesh v0.2 工作流协调器（DAG）

v0.2 新增明确编排的**持久化多 Agent 协调**，不是让语言模型自由决定可执行工具。客户端必须在提交前选择已注册、已授权的 Agent。

## 运行演示

先根据 `README.zh-CN.md` 启动网关并配置 `MESH_TOKEN`、`MESH_URL`，再执行：

```bash
python skills/open-agent-mesh/scripts/mesh.py workflow examples/workflow.json
python skills/open-agent-mesh/scripts/mesh.py workflow-get <返回的工作流ID>
```

也可以用 Python SDK：

```python
from agentmesh.client import MeshClient
mesh = MeshClient("http://127.0.0.1:8765", "YOUR_TOKEN_FROM_SECURE_CONFIG")
workflow = mesh.submit_workflow([
    {"id": "draft", "to": "uppercase", "input": "hello"},
    {"id": "review", "to": "echo", "depends_on": ["draft"],
     "input": {"draft": {"$from": "draft"}}},
])
result = mesh.wait_workflow(workflow["id"])
print(result["state"], result["steps"])
```

## HTTP API

- `POST /v1/workflows`：创建工作流，返回 202。参数 `steps`（1-24 个）、可选 `project_id` 和 `idempotency_key`。
- `GET /v1/workflows`：列出自己提交的工作流（管理员可列出全部）。
- `GET /v1/workflows/{id}`：查看执行状态、每步输出、重试次数；只能自己或管理员读取。
- `GET /v1/workflows/{id}/events`：读取不含内容负载的状态审计。
- `POST /v1/workflows/{id}/cancel`：取消尚未完成的步骤（无法撤销远端副作用）。

每步字段：`id`、`to`、`input`、`depends_on`、可选 `fact_keys`、`max_attempts`（1-3，默认 1）。

`{"$from":"draft"}` 只能引用 `depends_on` 列出的步骤输出。允许嵌套在对象和数组中；工作流拒绝缺失依赖、环、重复步骤和非法引用。不同分支可并行执行。任一步最终失败后依赖它的步骤会标为 `skipped`；未受影响的分支继续执行。

## 共享项目记忆与权限

设置 `project_id` 后，可以在步骤中指定 `fact_keys`；系统在**创建工作流时**固定被选中事实的 `value` 和 `revision`，作为 `context.mesh_workflow.facts` 传入执行 Agent。默认不发送任何项目事实。请仅向可信 Agent 分享已审核的非敏感内容。

在 `MESH_TOKENS` 对应主体下可写：

```json
{"name":"project_reader","allowed_agents":["echo"],"project_roles":{"project-a":"reader"}}
```

- `reader`：可读取事实与历史，但不能更新。
- `editor`：可读写事实（写入需要 `expected_revision`）。
- 旧配置 `allowed_projects` 仍按读写权限处理，便于迁移；管理员拥有全部权限。

## 恢复与限制

工作流、步骤、重试次数和快照保存在 SQLite。网关重启后会重新检查正在执行的工作流；原有任务队列也会恢复 `working` 状态任务。重试每步最多三次，退避约 1、2 秒（未来可配置）。同一提交可通过 `idempotency_key` 去重。

**仍属单进程演示级实现：** 没有跨实例排他锁/租约、分布式队列、远端副作用回滚、人工审批、全面 A2A 协议认证或加密多租户存储；生产环境必须增加这些能力，不应把机密写进任务或项目事实。并发的多进程调度尚未受支持。
