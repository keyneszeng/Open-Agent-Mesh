# Open Agent Mesh v0.4：跨 Agent 多轮对话引擎

**状态：实现并通过本地自动化与跨进程演练的研究原型，不是全量 A2A/MCP 一致性认证，也不直接连接封闭聊天产品。**

## 产品定位

将通信层（A2A 1.0 子集、HTTP JSON、MCP 工具子集、Agent Skills）与会话层分离：

- **Conversation**：有所有者、可选 `project_id`、最多 8 个预先授权 Agent 参与者、最多 40 次 Agent 调用；历史与审计留在 SQLite。
- **Message**：用户或被授权的 Agent 可投递到指定参与者；服务器根据 Bearer 身份确定真实 `sender`，不接受客户端伪造来源。
- **Turn**：由调度器可靠地委派给既有任务队列；每轮携带历史的有界窗口（最多 12 条，每条不超过 1600 字符）。
- **Discussion**：**人类显式启动**，至少 2 个参与者，2–20 次轮流执行；每位 Agent 收到上一位发言及历史，失败自动停止，不允许无限递归互聊。
- **Ownership**：只有 Owner / Admin 或已被邀请、且具有项目读取权限的专属 Agent Principal 能读取会话。Agent 不能自动创建群聊、启动自动讨论或关闭会话。
- **Approval Gate**：被配置为 `requires_approval` 的 Agent 完全不能通过对话渠道调用；仍须通过 v0.3 人工审批工作流。

**提醒：这里的 Agent “群聊”是有序、限轮、持久化的会话模型，不是 WebSocket 实时聊天室。示例 Agent 只是确定性函数，不会替代真正的 LLM。**

## 使用方法

需要先配置 `MESH_TOKENS` 和 `agents.example.json`，按 README 启动服务。下面示例必须先把变量 `TOKEN` 设置成自己有权限的 Bearer Token；`PORT=8765`：

```bash
curl -s -X POST http://127.0.0.1:8765/v1/conversations \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"架构评审","participants":["uppercase","echo"],"max_turns":6}'
```

从响应取得 `id`（下例称为 `$CID`），发起人类启动的有限轮讨论：

```bash
curl -s -X POST "http://127.0.0.1:8765/v1/conversations/$CID/discussions" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt":"讨论 Agent 间共享记忆的原则","rounds":4,"idempotency_key":"arch-review-1"}'

curl -s "http://127.0.0.1:8765/v1/conversations/$CID/messages" \
  -H "Authorization: Bearer $TOKEN"
```

第二种方式：手动发送多轮消息，每轮等上轮完成后向另一位 Agent 投递：

```bash
curl -s -X POST "http://127.0.0.1:8765/v1/conversations/$CID/messages" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"to":"echo","text":"请评审上一位的结论","idempotency_key":"review-2"}'
```

**主要 API**：`POST /v1/conversations`、`GET /v1/conversations`、`GET /v1/conversations/{id}`、`POST /{id}/messages`、`POST /{id}/discussions`、`GET /{id}/messages?after=N&limit=100`、`GET /{id}/events`、`POST /{id}/close`（上述省略路径的均在 `/v1/conversations` 下）。

**Python SDK**：`MeshClient.create_conversation()`、`send_conversation_message()`、`start_discussion()`、`conversation_messages()`。

**Agent Skills CLI**：`chat-new`、`chat-show`、`chat-send`、`chat-discuss`、`chat-messages`、`chat-close`。

**MCP 工具子集**：`mesh_create_conversation`、`mesh_get_conversation`、`mesh_conversation_messages`、`mesh_send_conversation_message`、`mesh_start_discussion`。兼容性声明仅限本仓库受测 JSON 响应子集；2026-07-28 MCP 完整状态机、SDK 一致性和安全功能尚未完成。<https://modelcontextprotocol.io/specification/2026-07-28>

## A2A 上下文连续性

A2A 1.0 定义 `contextId` 标识逻辑会话。v0.4 `SendMessage` 支持**使用相同 `contextId` 创建新任务**，将同一所有者任务最近最多 12 条输出/输入片段作为 `_a2a_history` 传给本地执行者；远端 A2A 适配器也透传该 `contextId`。`messageId` 用于幂等去重，修改原 `messageId` 的内容或上下文会拒绝。

仍未实现对已有 *input-required* 任务直接追加 `taskId` 消息、SSE 双向流、推送通知与官方 TCK 一致性。官方规范：<https://a2a-protocol.org/v1.0.0/specification/>。

## 故障恢复语义

1. 消息、轮次请求在同一个 SQLite 事务中创建。
2. 后台调度器将队列轮次绑定至既有持久化任务，使用确定性 `conversation:{id}:{number}` 去重键；失败或重启后重复调度不应生成额外任务。
3. 已完成任务的输出在事务中归档到消息表，下一轮任务仅在前一轮成功后创建；重复轮询不应增加回复。
4. 关闭会话阻止后续轮次并取消关联任务。**已经发送给外部 Agent 的调用可能无法撤回，仍存在至少一次执行与远程副作用重复风险**。
5. 日志只记录时间、状态、目标与审计信息，不记录消息正文；消息本体和任务数据在 SQLite 本地明文持久化。

## 安全与商业版差距

- 没有端到端加密、细粒度成员邀请审核、OIDC、限流、配额、分布式数据库与多租户隔离；不宜直接对公网开放。
- Agent 发言视为**不可信内容**，不会被 Mesh 当作 shell 指令或自动调用任意工具；但接入的第三方 Agent 仍须自己防护提示注入和工具越权。
- 对话历史只发送有界窗口；仍需在生产前添加敏感信息脱敏、数据保留/删除和审计策略。
- `max_turns` 限制消息调用次数，不等于调用费用上限；Token、费用、运行时间及网络流量的商业级预算策略尚未实现。
- 测试中通过 A2A/HTTP 的是自建模拟 Agent；没有直接连接或认证 ChatGPT、Cursor、Claude 等封闭平台。

## 重复验证命令

```bash
python -m pytest -q
python -W error::ResourceWarning -m pytest -q
python tests/smoke_network.py
python tests/smoke_conversations_network.py
python tests/chaos_conversations.py
python tests/stress_conversations_http.py
python tests/chaos_network.py
python tests/stress_http.py
```

以上脚本全部是本机回环地址上的离线集成验证；不能代替互联网部署前的第三方协议 TCK、安全渗透、备份恢复及真实 LLM 预算测试。
