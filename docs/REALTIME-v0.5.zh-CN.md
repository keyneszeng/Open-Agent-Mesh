# Open Agent Mesh v0.5 实时跨平台 Agent 对话

**版本：** 0.5.0｜**性质：** 可测试的开放式原型，不是 ChatGPT、Claude、Cursor 专有客户端的官方集成，也未通过 A2A/MCP 官方一致性认证。

## 1. 功能与架构

- **私有 Mesh 事件流：** `GET /v1/conversations/{conversation_id}/stream`，`Authorization: Bearer ...`。提交消息仍通过独立 `POST /v1/conversations/{conversation_id}/messages`。Web 客户端需要 `fetch()` streaming；原生 `EventSource` 无法设置 Authorization header。
- **持久事件顺序：** 每个会话独立严格递增 `seq`；SSE 的 `id:` 即该序号。支持 `Last-Event-ID` 优先、`?after=` 兜底，重连可补发对应会话的后续记录。订阅默认 20 秒、可设 0.1~30 秒然后重连，心跳保活。
- **消息类型：** `state` 是会话/任务进度，`message` 是持久化正式消息，`delta` 是 **provisional=true 的文本片段**。`delta` 有 `attempt` 与 `task_id`；租约失效或 SIGKILL 重新执行可能出现前一次尝试的临时文本，消费者不得将其当作最终答案。
- **外部 Agent 适配器：** 管理员在 agent config 中注册 `http_sse` 或 `a2a_stream`（只接受固定的配置端点，不让任务调用方传任意 URL），通过独立 `token_env` 读取外部 Bearer token。`http_json` / Python callable 不会凭空生成 token 级片段，但依旧发送任务状态与最终结果。
- **A2A JSON-RPC SSE 子集：** 支持 `A2A-Version: 1.0` 下的 `SendStreamingMessage` 和 `SubscribeToTask`，使用 `text/event-stream` 发送初始 Task、状态变化与分片 artifactUpdate，最终状态 `final=true`。不支持已有 A2A Task 的原位多轮续接、文件附件、推送通知。对外的 Agent Card 仅对可运行的 SSE 子集宣告 `streaming: true`。
- **资源边界：** 每次任务最多 256 个 4KB 文本片段、累计约 64KB；单会话依照既有最大轮数限制。SQLite 为研发用存储，不保证分布式多活、完整高可用或多租户数据隔离。

## 2. 最小 HTTP SSE Agent 接入协议

配置由管理员维护，不得将 Token 硬编码到 GitHub：

```json
{"id":"my-live-agent","kind":"http_sse","url":"http://127.0.0.1:19913/invoke","token_env":"MY_LIVE_AGENT_TOKEN","timeout_seconds":20}
```

Mesh 发起 `POST /invoke`：

```json
{"task_id":"uuid","input":"请分析","context":{"_mesh_history":[]},"parent_task_id":null}
```

必须返回 `Content-Type: text/event-stream`：

```text
event: delta
data: {"text":"第一段"}

event: delta
data: {"text":"第二段"}

event: done
data: {"output":"第一段第二段"}

```

缺少 `done`、错误 JSON、非 SSE Content-Type、过大的输出均会失败关闭。`Idempotency-Key` HTTP header 为稳定 task ID，接收方必须自行做好幂等去重。可运行的演示 Agent 在 `examples/streaming_http_agent.py`。

## 3. Python 与 Agent Skill 使用

```python
from agentmesh.client import MeshClient
m = MeshClient("http://127.0.0.1:8765", "AUTHORIZED_TOKEN")
chat = m.create_conversation("审查系统架构", ["my-live-agent", "remote-reviewer"])
# 在另一线程、进程或终端开始监听：
for event in m.conversation_stream(chat["id"], after=0, wait_seconds=20):
    print(event["seq"], event["kind"], event["data"])
# 另一个授权客户端使用 m.start_discussion(... rounds=4) 或 send_conversation_message(...) 发送内容
```

CLI：`python skills/open-agent-mesh/scripts/mesh.py chat-stream CONVERSATION_ID --after 0 --wait-seconds 20`。

## 4. 反复测试

```bash
pip install -e '.[dev]'
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -W error::ResourceWarning -m pytest -q
PYTHONPATH=src python tests/smoke_network.py
PYTHONPATH=src python tests/smoke_conversations_network.py
PYTHONPATH=src python tests/smoke_streaming_v05.py
PYTHONPATH=src python tests/chaos_streaming_v05.py
PYTHONPATH=src python tests/stress_streaming_v05.py
```

测试覆盖：单元、兼容回归、SSE 事件恢复、越权拒绝、A2A JSON-RPC 订阅、外部 SSE 协议故障、过大报文、旧任务租约拒写、多进程 token-first 验证、SIGKILL 重启、并发会话负载。GitHub Actions 使用 Python 3.10 / 3.12 / 3.13 交叉验证。

## 5. 特别注意

1. **不是完整 MCP Streamable HTTP：** v0.5 的 `GET /v1/conversations/.../stream` 属于项目自有 REST 协议，不能称为 MCP 2026-07-28 中的 `subscriptions/listen`，后者采用独立 POST SSE 请求。参考：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/transports/streamable-http.mdx>。
2. **A2A 只实现一部分：** 本项目实现两项 JSON-RPC SSE 方法，但尚未覆盖 A2A 1.0 全量生命周期、流断连重续、PushNotification 和官方 SDK 一致性测试。参考：<https://github.com/a2aproject/A2A/blob/main/docs/specification.md>。
3. **并非所有 Agent 天生支持流式输出：** 依赖对方公开并允许访问的 HTTP/A2A API，或由开发者加装可授权适配器。仅安装 Skill 不能操控托管版 ChatGPT、Claude、Cursor 的内部会话。
4. **不允许把临时输出用于高风险自动操作：** 部署、付款、删除等仍必须经现有审批关卡。实时片段是未验证的远端内容，要防提示注入与泄露。生产版还需要 HTTPS、服务端密钥管理、最小权限、动态限速、审计保留策略、OIDC/mTLS 和隔离租户资源。
5. **故障恢复语义：** 已收到的 SSE 事件可补收；不能保证外部副作用 exactly-once。任务发生重试，旧 attempt 的片段仍可被读取，但最终消息会由当前有效租约一次性确认。
