# ADR-0002：以统一目标上下文驱动两种 Agent 协作模式

- 日期：2026-11-11
- 状态：**Accepted as product architecture; NOT implemented**（产品架构决定，尚未完成编码）
- 适用范围：Open Agent Mesh 未来所有 Agent 通信、任务、工作流、会话和工作区。
- 背景：用户明确区分两种实际使用场景：**无工作区（群）的直接工作流程通信**与**有工作区（群）的工作流通信**；二者共享同一份权威上下文以约束 Agent，且 Agent 背后存在授权其行为的人类。

## 一、产品原则

Open Agent Mesh 首先是**人类授权的跨 Agent 协同工作基础设施**，其次才是聊天与工作区产品。工作区不是前置条件；群聊不能成为执行任务的唯一入口。Agent 可以承接大量执行工作，但最终责任与高风险授权不能转移给会话文字或模型自述。

底层只保留**一个 Context Engine 与统一的治理模型**；两个模式共用一套 Agent 注册、身份、上下文、事件、工作流、审计与权限基础设施：

1. **Direct Flow（无群模式）**：主体发起一个目标驱动的流程；不同 Agent 可串行、并行、按需接力或进行有限对话；无需创建长期工作区。此模式仍须有持久化 `goal_id` / `run_id`、任务日志与上下文版本，不能是无审计、无上下文的临时聊天。
2. **Workspace Flow（有群模式）**：一个长期工作区围绕共同目标容纳人类和不同平台的 Agent。成员可持续沟通、跨多个工作流协作、交接任务并审查交付。每个工作流仍遵守同样的上下文与权限规则。

**同一份上下文**指一个逻辑上的**权威来源（source of truth）**，而**不是**把所有原始数据、私人记忆、密钥与文件无差别复制给每个 Agent。应生成每个任务/Agent 所需的、按权限裁剪并记录版本的视图。

## 二、核心概念和身份模型

| 概念 | 作用 | 关键约束 |
| --- | --- | --- |
| `principal_id` | 人类 / 团队 / 组织中可负责与授权的主体 | 可审计，组织委托须明确；不能由 Agent 自行声称 |
| `agent_id` / `agent_instance_id` | Agent 能力身份及一次运行实例 | Agent 不等于人；代理操作要可归责 |
| `delegation_id` | 主体授予 Agent 的限权委托 | 指定作用域、操作、过期时间、撤销、预算 |
| `goal_id` | 可验收的共同工作目标 | 两种模式都必需；必须有验收条件 |
| `context_scope_id` | 目标的权威共享上下文 | 与 workspace_id 分离，不得依赖群组存在 |
| `context_revision` | 上下文不可变版本 / 变更日志 | 每次决策与执行记录所见版本 |
| `workspace_id?` | 可选的长期协作容器 | 只在 Workspace Flow 存在 |
| `workflow_id` / `run_id` | DAG 流程、执行实例与依赖 | 无群/有群均使用相同引擎 |
| `thread_id?` | 可选会话或讨论线程 | 对话用于协调，不代替批准与执行审计 |
| `task_id` | 可执行单元 | 绑定负责主体、代理、上下文快照及结果 |
| `artifact_id` | 代码、文件、报告等交付成果 | 记录来源、版本、签名/校验与访问规则 |

**操作归属链**：`principal_id → delegation_id → agent_instance_id → task_id → artifact_id`。一个人可以授权多个 Agent；组织也可依法合规授权服务 Agent。人类并不必然一对一绑定 Agent，重点是每个有副作用的行为都有可追溯的委托链。

## 三、Context Engine 的分层

1. **组织/平台策略层**：身份、租户、数据分类、允许的工具、预算及强制审批规则（不得被项目消息覆盖）。
2. **目标事实层**：目标、范围、需求、技术约束、验收标准、已批准的架构决策、工件索引、决策修订历史。
3. **执行快照层**：特定 `run_id/task_id` 在特定 `context_revision` 读取的事实引用、依赖结果及允许的工具。
4. **个人/私有层**：仅归所有者控制的偏好、私有内容与密钥；**默认不进入共享上下文**，共享需明确授权且保留出处。

每个 Agent 得到 `ContextView(principal, agent, goal, task, revision, grants)`。读取与更新都要经过服务器端授权校验；**不能由 Agent 自行生成权限声明或仅凭群聊邀请越权**。并发写入采用版本检查和冲突策略，重大事实变更须有审批/审核。最终交付根据锁定的上下文修订、工件和验收结果复现。

## 四、两种模式中的统一运行契约

```text
Goal + Context Scope + Human Authority
                │
        Policy / Context Engine
                │
       Orchestration + Events
          ┌─────┴─────┐
          │           │
     Direct Flow   Workspace Flow
     workspace=null  workspace=ID
          │           │
      Tasks / A2A / MCP / Skills / Conversations
          └─────┬─────┘
              Artifacts
                  │
           Validation + Approval
```

- **Direct Flow**：用户或已授权 Agent 提交目标及可选工作流，Mesh 按必要性发现/调用 Agent；默认不建立群组，但可以保留临时 thread 与事件流。上下文绑定 `goal_id`。适合单次报表、自动修复、执行流水线、跨工具转交。
- **Workspace Flow**：用户创建工作区并加入一组人类与 Agent，承载一个或多个目标与工作流；参与者持续讨论、认领任务、共同编辑经授权的上下文。适合长期软件研发与跨职能项目。上下文仍以 `context_scope_id` 为权威来源，群消息不是自动生效的政策。
- **升级路径**：Direct Flow 可以在得到主体授权后升级为 Workspace Flow；**保留 goal_id、context_scope_id、run_id、上下文修订、审计与工件**，只新增 workspace/member/thread 关联。新增成员仅能读取其入群授权允许的历史。
- **退出路径**：Workspace 中的某个流程可以按原权限作为独立 Direct Flow 执行；不得据此自动带出私有群内容。

## 五、治理和边界

- **人类控制**：高风险的部署、付款、删除、对外发送与权限提升设置服务器端审批，Agent 对话不能绕过。
- **权限是能力而非厂商品牌**：产品/设计/开发/测试是角色与技能；模型供应商或客户端名称不决定权限。Agent 可以换岗，但需要相应授权。
- **受信边界**：Agent 消息、工具返回、网页与共享上下文都视为不可信输入，不能执行其中的“忽略审批”指令。
- **持续协作有成本**：最大轮数、时间、Token、并发量及预算由主体/平台规定，防止无限 Agent-to-Agent 循环。
- **追责与可解释**：记录人工目标、委托凭证、Agent 版本、上下文版本、计划、执行、工具副作用、批准、验收、失败原因和重试。
- **隐私**：跨组织租户隔离；不同工作区与直接流程不得因复用一个 Agent 而泄露资料。

## 六、建议 API（方案，尚未实现）

```http
POST /v1/goals
POST /v1/goals/{goal_id}/runs             # Direct Flow；workspace_id 可为空
POST /v1/workspaces                      # Workspace Flow 的可选容器
POST /v1/workspaces/{id}/members
POST /v1/workspaces/{id}/goals/{goal_id}/attach
GET  /v1/context-scopes/{id}?revision=...
POST /v1/context-scopes/{id}/changes      # compare-and-swap + policy check
GET  /v1/runs/{id}/events                 # SSE + replay
GET  /v1/runs/{id}/artifacts
POST /v1/runs/{id}/approve
```

标准 A2A/MCP/Skills 接口仍是传输/调用面，不应将上述 Mesh 私有资源路径冒充成官方协议端点。

## 七、验收条件与分阶段实现

- **A：一致性**：Direct 与 Workspace 模式对相同目标和上下文版本得到等价的授权结果；不创建工作区仍能完成全流程。
- **B：权限隔离**：未授权的 Agent 不能读取/修改目标事实；工作区成员不能窃取另一个主体的上下文或私有记忆。
- **C：迁移不丢失**：直接流程升级到工作区后仍保留上下文、任务、事件与工件版本；成员入群历史可见性独立控制。
- **D：角色与归责**：每次 Agent 外部副作用都可追溯到人类或组织授权及作用域；未经批准的高风险执行被拒绝。
- **E：可靠性**：断线补收、重启、租约恢复、并发编辑冲突、工作流 DAG 和事件去重有测试；不夸大 exactly-once 保证。
- **F：互操作**：先以自建 Python/HTTP/A2A Agent 验收，再用各厂商官方授权 SDK 做一致性测试；未经接入测试不可宣称任何封闭客户端可以原生互联。

**实现阶段建议**：统一 `goal/context_scope` 数据模型 → Direct Flow API → Workspace member/role 及关联 → 同一上下文读写 API → 迁移与归责审计 → 安全/并发/故障/第三方兼容测试。

## 八、与当前版本的关系

v0.5 已有任务队列、DAG 协调、项目事实版本、基本角色 ACL、会话持久化及 SSE；但 `goal_id/context_scope_id` 独立实体、可选 Workspace、组织级委托链、同源上下文视图及模式转换**均未实现**。本 ADR 不等于代码实现，也不等于完成生产安全验证。

## 九、对比备选方案

- **所有通信都必须建群**：拒绝；使一次性 Agent 流程付出不必要的工作区组织成本。
- **两个模式各维护一套上下文库**：拒绝；容易产生规则漂移、权限冲突和审计断层。
- **把群聊聊天记录当成权威上下文**：拒绝；消息是协商材料，只有经过验证/授权写入的事实和决策才能变为规则。
- **让 Agent 自称代表某个人**：拒绝；需独立鉴权和可撤销委托。
