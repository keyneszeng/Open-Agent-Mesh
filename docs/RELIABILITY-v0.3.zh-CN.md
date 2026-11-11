# v0.3 可靠性与兼容性：设计、操作和测试证据

> 版本定位：**可运行的研发预览版**，不是已认证的 A2A/MCP 完整实现，更不是互联网生产就绪服务。

## 核心可靠性机制

- `tasks.lease_token` 与 `tasks.lease_expires_at`：SQLite `BEGIN IMMEDIATE` 原子抢占；抢占后返回唯一 token。
- 每个执行进程持续心跳续租；`finish()` 校验租约 token 与有效期，过期或被另一 Worker 接管的旧结果不能覆盖新的任务状态。
- 超时租约在下一次 `claim()` 被回收，事件日志记录 `submitted -> working -> submitted -> working`。
- v0.1/v0.2 历史表在首次启动时补列。旧版未带租约的 `working` 任务，在下一次抢占时进入回收流程。
- `MESH_LEASE_SECONDS` 默认 30 秒，可配置为 2~3600 秒。短租约只建议故障注入测试使用。
- **只提供至少一次执行语义**：一旦任务对外部系统产生副作用，数据库租约无法实现全局 exactly-once，接收侧仍必须实现幂等键/事务和重复消息去重。
- 当前协调器依然是单进程 SQLite DAG：**不得用多个 Uvicorn Worker/实例并行执行同一工作流 DB**。租约只改善了底层任务抢占，并不意味着分布式工作流协调已完成。

## 人工审批门禁

配置 `agents.json` 某个 Agent 的 `requires_approval: true`，此配置属于服务管理员，调用方不能关闭该门禁。

- 直接调用 `/v1/tasks` / A2A `SendMessage` / MCP `mesh_submit_task` 返回拒绝；必须以工作流步骤运行。
- 工作流遇到该步骤后进入 `awaiting_approval`，在批准前不创建底层任务（`attempts = 0`）。
- 审核 API：`POST /v1/workflows/{id}/steps/{step}/approve` 或 `/reject`；使用不同于提交者的 principal，且必须是管理员或带 `can_approve: true` 且具有对应项目写权限的用户。
- 拒绝后该步骤失败，依赖步骤被跳过；取消或重复审核返回冲突。审核人、时间、决定记录在 SQLite 与工作流审计日志中。
- **重要信任边界**：`can_approve` 是静态 Token 声明，并不能技术上证明调用者是人类。生产部署必须接入交互式身份验证、身份二次确认/签名和必要的操作审计。

### 示例

```json
{
  "project_id": "demo-project",
  "steps": [
    {"id":"analysis","to":"uppercase","input":"inspect request"},
    {"id":"controlled","to":"gated-demo","input":{"$from":"analysis"},"depends_on":["analysis"]}
  ]
}
```

进入 `awaiting_approval` 后，可使用不同主体的批准 Token 调用：

```bash
curl -X POST "http://127.0.0.1:8765/v1/workflows/$WORKFLOW_ID/steps/controlled/approve" \
  -H "Authorization: Bearer $REVIEWER_TOKEN"
```

不要把审核 Token 暴露给 Agent，或允许它作为调用方可控制的上下文/环境变量。

## MCP 2026-07-28 JSON Streamable HTTP 子集

新端点 `POST /mcp`，支持静态 Bearer ACL、`initialize`、`ping`、`notifications/initialized`、`tools/list`、`tools/call`。实现了以下工具：`mesh_list_agents`、`mesh_submit_task`、`mesh_get_task`、`mesh_create_workflow`、`mesh_get_workflow`。

MCP 要求 `MCP-Protocol-Version: 2026-07-28` 和 `Mcp-Method`；`tools/call` 还需 `Mcp-Name`。工具调用不能绕过已有 Agent/项目/任务权限。**暂不支持** SSE、Server-side Sampling、资源/提示词协议、MRTR、多版本兼容或完整 SDK conformance；这些缺失将继续由后续 issue 跟踪。

```bash
curl http://127.0.0.1:8765/mcp \
 -H "Authorization: Bearer $MESH_TOKEN" \
 -H 'Content-Type: application/json' \
 -H 'MCP-Protocol-Version: 2026-07-28' \
 -H 'Mcp-Method: tools/list' \
 -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

## 多轮测试命令（全部可在离线本机执行）

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -W error::ResourceWarning -m pytest -q
python -m compileall -q src examples
python tests/smoke_network.py
python tests/chaos_network.py
python tests/stress_http.py
```

测试包含：基础功能回归、确定性 DAG 随机化、MCP 不合法输入/头部、鉴权与审批拒绝、SQLite 迁移、跨线程原子抢占、租约续租与失效防护、进程 SIGKILL 恢复、独立双网关 A2A、外部 HTTP Agent、Node 客户端/Skill CLI、12 并发提交者和 10 组 DAG 工作流。

**限制**：开发测试与 GitHub CI 的成功不等于成熟生产环境验证。仍需真实多副本+PostgreSQL、网络分区、压力容量曲线、OIDC/OAuth、TLS/mTLS、第三方 A2A/MCP SDK 一致性测试、成本/速率配额、数据加密与备份演练。
