# Open Agent Mesh — 通用 Agent 协作网关 v0.1

本项目旨在提炼 Cursor Projects 类似的跨 Agent 协作思想，用开放标准实现一个**与具体大模型厂商无关的通用协作层**。

> 当前交付物是已通过基础测试的 MVP（概念验证版），不是 Cursor 源码，不属于 Cursor 产品，也**不是经过官方一致性认证的完整 A2A 服务**。封闭的 ChatGPT/Cursor/Claude 产品只有在对应客户端提供接口、技能加载器或工具权限时才能接入；安装此包不会自动获得这些权限。

## 一、为什么采用 A2A + Agent Skills + 共享知识

| 组件 | 作用 | 本版状态 |
| --- | --- | --- |
| A2A 1.0 JSON-RPC（开放通信标准） | 消息与任务交互 | 核心子集实现 |
| 普通 HTTP JSON | 兼容各语言和自研框架 | 已实现 |
| Agent Skills `SKILL.md` | 让支持技能标准的 Agent 学会调用网关 | 已实现可携带的 Skill |
| 项目知识库 | 不同 Agent 对齐业务规则，记录修改版本 | SQLite + 版本冲突检查 |
| 任务队列 | 提交、完成、失败、取消、记录事件 | 已实现 |
| 权限治理 | Token、Agent 调用范围、项目可访问范围 | 适用本地 MVP |
| 主动智能协调 Agent | 自动规划、分工与解决冲突 | **尚未实现**，目前可由用户自定义协调器 |
| MCP、SSE、审批、分布式执行 | 完整 Agent 生态协作 | 后续研发 |

## 二、五分钟本地运行

需要 Python 3.10+。在解压目录内执行：

```bash
cd open-agent-mesh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
export MESH_TOKENS='{"local-demo-admin-token-change-me":{"name":"admin","role":"admin","allowed_agents":["*"]},"local-demo-client-token-change-me":{"name":"alice","allowed_agents":["echo","uppercase"],"allowed_projects":["demo-project"]}}'
export MESH_TOKEN=local-demo-client-token-change-me
export MESH_DB_PATH=./mesh.sqlite3
agentmesh
```

在另一个终端配置相同 `MESH_TOKEN` 后运行：

```bash
python examples/python_client.py
node examples/node_client.mjs
python examples/coordinator.py
```

Swagger API 文档：`http://127.0.0.1:8765/docs`。

> 上述口令**仅用于本机演示**，不能原样放到互联网服务器。生产环境应使用高熵随机口令、HTTPS、完善的权限和访问限制。

## 三、不同 Agent 如何加入

1. **原生 A2A 1.0 Agent**：把已验证的 JSON-RPC 端点、鉴权环境变量写进 `agents.example.json`，由 Mesh 调用。
2. **Python / LangGraph / CrewAI 等**：将 Agent 包装为一个 Python 函数，或实现简单的 HTTP POST `/invoke`，返回 `{"output":...}`。
3. **Node.js 等非 Python 应用**：使用 `/v1/tasks`、`/v1/agents` 等 HTTP 接口。示例见 `examples/node_client.mjs`。
4. **Codex / Cursor / Claude Code 等支持 Agent Skills 的编码工具**：将 `skills/open-agent-mesh/` 放到产品支持的 skills 目录，配置服务地址和授权 Token，通过技能脚本调用。
5. **ChatGPT 等托管聊天产品**：仅当其托管环境提供对该 API 的工具连接能力时可调用；本项目目前没有自动接入托管 ChatGPT 的原生插件。

## 四、共享知识机制

项目规则以 `project_id + key` 定位，写入时携带 `expected_revision`。例如规则「核销通过后再请付款」，第一次写入使用版本 0；第二位 Agent 修改必须携带当前版本 1。如果已有其他 Agent 抢先更新，将收到 HTTP 409 而非静默覆盖。

任务不会自动得到整个项目知识库的全部内容，只有项目授权主体能够读取，且应自行选择要传给下游 Agent 的信息。这一点有助于减少上下文污染和不必要的信息外泄。

## 五、实际测试

- 本地自动化测试：`PYTHONPATH=src pytest -q`（24 项通过）。
- 跨进程联调：`PYTHONPATH=src python tests/smoke_network.py`。此测试在不同端口启动两个网关和一个 HTTP Agent，验证 A2A JSON-RPC 转发、HTTP 转发、Node 客户端和 Skill CLI。

## 六、仍需解决的工程问题

当前不支持 A2A 1.0 的全部功能，例如流式 SSE、推送通知、文件类型、会话中断后继续、全面的多租户和完整协议信息校验。执行采用单机 SQLite 与内存调度；崩溃后重新入队的远端任务存在重复执行风险，取消并不撤销远端副作用。需要在生产之前完成持久化工作租约、幂等执行、访问审计、限流、OIDC/mTLS、Agent Card 校验、人工审批和第三方一致性测试。

完整英文使用说明和配置示例请见 `README.md`，架构与安全清单请见 `docs/ARCHITECTURE.md`，后续研发路线请见 `docs/ROADMAP.md`。
