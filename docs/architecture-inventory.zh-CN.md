# AI-Agent-Stack 架构盘点（基于 main 分支真实代码）

> 语言：中文 · [English](./architecture-inventory.en.md)

> 盘点口径：本清单只依据仓库 `Jasonzhu12397/AI-Agent-Stack` main 分支当前代码核对，不把设计目标、文档规划或前端模板冒充成已完成功能。
> 盘点日期：2026-11-19；复核基线 HEAD：`d52e50bdbb8d53f31eb7b5d3f80b42c901dfa5ee`。

## 0. 三态定义（本清单统一口径）

| 标记 | 含义 | 判定标准 |
|---|---|---|
| 【基础】已有代码基础 | 仓库内存在可被导入、被测试覆盖的实现：明确目录、数据模型、视图、执行函数或契约 | 不代表已在真实环境跑通 |
| 【待验证】已实现待验证 | 代码路径完整、有单测或 mock 覆盖，但缺少真实环境（真实 K8s / 裸金属 / 真实模型供应商 / 真实向量库）端到端验收 | 失败/异常路径需在真实链路复核 |
| 【待开发】尚需继续开发 | 仅有设计、枚举值、前端模板、文档规划；运行时执行器、适配器或完整 UI 交互尚未落地 | 不能对外宣称已支持 |

## 1. 基线证据（本次复核）

- 本地基线（2026-11-19 复核）：**975 passed**；`ruff check .` All checks passed；`manage.py check` 0 issues；`makemigrations --check --dry-run` 无模型漂移。
- 测试环境：本地无 PostgreSQL/Redis，测试以内联 `DATABASE_URL=sqlite://` 覆盖运行；CI 使用 PostgreSQL 16 服务容器。
- 规模：`apps/` + `packages/` Python 源码与测试合计约 31.3k 行；`tests/test_*.py` 80 个。
- CI 工作流：`.github/workflows/ci.yml` 三个 job —— `python`（ruff / system check / 迁移漂移 / migrate / pytest）、`web`（npm test / Vite build）、`compose`（两份 compose 配置校验）。
- 历史记录：此前 Python CI 因 `tests/test_worker_tasks.py` 三处 ruff import 排序失败（非测试逻辑失败），已由 commit `5ba685b` 修复；远端 CI 重跑结果本次未再复核。

## 2. 整体架构总览（目标形态 × 真实目录映射）

目标形态：**通用 AI Agent 平台 + 三个基础设施部署 Agent + Metal3/Ironic/CAPI/Omni 执行系统**。AI Agent 负责理解、规划、编排、调用；底层基础设施组件负责真正执行部署。平台本身是 Multi-Agent，外部 AI Agent 通过插件/连接器方式接入被编排调用。

| 分层 | 目标组件 | 真实代码位置 | 现状 |
|---|---|---|---|
| 用户入口 | Web UI / Chat / Workflow / Agent 管理 / 插件市场 / 运维控制台 | `apps/web-studio/src/components/`、`src/services/api.ts` | 【基础】页面与 API 接线已存在；逐交互验收待核对（见 §6） |
| 平台后端 | API Gateway / Auth / Agent Runtime / Workflow Engine / Model Router / Plugin·MCP Gateway / Knowledge·RAG / Token Budget / Audit·RBAC / Task Queue / Realtime Logs / Events | `apps/platform-api/core/`、`apps/worker/worker_app/` | 【基础】大部分模块有实现；任务页实时推送已落地（Channels WebSocket 主通道 + SSE 回退），模型路由降级等待开发（见 §3） |
| 基础设施 Agent 控制面 | Cloud Control / EPH / Target 三 Agent | `apps/cloud_control_agent/`、`apps/eph_agent/`、`apps/target_agent/` | 【基础】三 Agent 独立入口、协议与状态机已存在；真实裸金属链路待验证（见 §4） |
| Agent 协议与传输 | 身份 / 命令 / 租约 / 心跳 / 检查点 / mTLS | `packages/agent_contracts/`、`packages/agent_transport/` | 【基础】契约与传输已实现（见 §7） |
| 执行层 | CAPI / Metal3 / Ironic·BMO / Omni / BMH Inventory / Kubeadm·K8s / Chrony·NTP / SR-IOV·DPDK·Multus | 由三 Agent 内 skill / adapter 模块调用 `kubectl` / `clusterctl` / Ironic HTTP 客户端 | 【待验证】执行适配器以只读观察 + fail-closed 校验为主；真实安装/迁移/数据面转发待验收（见 §5） |
| 物理裸金属 | 服务器 / BMC·IPMI·Redfish / 管理网 / 控制面网 / 数据面网 | `apps/platform-api/core/bmc_credentials.py`、fleet 库存视图；BMC 配置持久化 | 【基础】凭据与库存模型存在；真实 BMC/Ironic 通电验收待开发 |

## 3. 平台层：通用 AI Agent 能力逐项盘点

| 模块 | 目标职责 | 代码证据 | 三态 | 缺口 / 备注 |
|---|---|---|---|---|
| API Backend | 对外 API、用户、项目、任务管理 | DRF 路由覆盖 auth / tenant / project / task / model-provider / model-config / agent / agent-run / workflow / run / knowledge / connector / installation / connection / cluster-deployment / fleet / agent-node / command / approval / audit / member / token-budget / sriov / handoff / postflight（`core/urls.py`） | 【基础】 | 需逐接口核对错误态、分页与权限边界 |
| Agent Runtime | Agent 生命周期、上下文、工具调用 | `apps/worker/worker_app/tasks.py`：DB 任务记录、原子认领、lease token/过期、心跳续约、有限重试、协作取消；`core/agent_views.py`：Agent 定义 / 版本 / 发布 / 运行记录 | 【基础】 | 多步工具调用的上下文保持、失败恢复待在真实模型链路复核 |
| Workflow Engine | 多步骤编排、状态流转 | `core/workflow_engine.py`：节点类型 `start / llm / condition / knowledge_search / tool / end`；`core/workflow_views.py`：validate / publish / runs | 【基础】 | 【待开发】并行分支、循环、人工审批节点与审批恢复；当前无 `parallel / loop / approval` 节点类型 |
| Model Router | 国内外模型统一接入与路由 | `core/model_gateway.py`：`ProviderType` 仅 `fake` 与 `openai_compatible`；chat `/chat/completions`、embeddings `/embeddings` HTTP 调用；错误按 5xx/429 标记可重试 | 【基础】 | 【待开发】多供应商主备路由、故障降级链、fallback；国内供应商靠 OpenAI-compatible 接口接入，无原生 SDK；FAKE 仅供测试 |
| Plugin / MCP Gateway | 插件、MCP Server、第三方系统集成 | `core/plugin_views.py`：连接器目录、审批、项目安装、显式授权、连接记录；`core/http_connector.py`：`register_connector_tool` 按连接器类型分发（`http` / `openapi` / `mcp`），并保留 `register_http_tool` 兼容别名；`core/mcp_connector.py`：MCP JSON-RPC 客户端，同时支持 streamable HTTP（initialize / tools/list / tools/call、SSE 响应解析、协议版本协商、重定向与 host allowlist 拒绝）与本地 stdio（无 shell 启动、行分隔 JSON-RPC、stderr 排空、超时终止、`MCP_STDIO_ALLOWED_COMMANDS` 可执行白名单、env 仅允许 `env:` 引用）；`core/mcp_tool_sync.py` + `core/mcp_views.py`：`POST /api/v1/plugin-connections/<id>/sync-tools/` 发现并导入远端工具（新工具 `enabled=False` 待审，单连接上限 256） | 【基础】（HTTP / OpenAPI / MCP 三通道） | 【待验证】真实 MCP Server 联调与工具导入后的人工审批闭环；stdio 传输【已实现/已测试】；服务端模式【已实现/已测试】（项目级 JSON-RPC：initialize、tools/list、tools/call；暴露任务查询/创建与连接器能力调用，复用项目成员权限与能力审批门禁）；OAuth 授权码流程【已实现/已测试】（`POST .../agent-plugins/studio/oauth/authorize/` 生成 PKCE 授权 URL 并封装 state；`POST/GET .../oauth/callback/` 用 code + sealed state 换取 token，持久化 refresh token；调用时复用未过期 access token，过期后用 refresh_token 刷新并在行锁内持久化；Plugin Studio 提供“授权码”模式并发起跳转，`/integrations` 回跳后自动换取凭据引用，发布时直接绑定该 `plugin://` 引用） |
| Knowledge / RAG | 文档、向量检索、知识库 | `core/knowledge_models.py`、`core/knowledge_views.py`、`core/rag_runtime.py`、`core/vector_store.py`：知识库/文档/嵌入模型绑定；`QdrantVectorStore`（urllib HTTP，需 `QDRANT_URL` / collection / api-key env 引用）；集合按 embedding 模型和维度版本化（`collection_for_model` / `for_model`，如 `agent_knowledge__bge-m3-1024`）；首次 ingest 幂等建集合（`ensure_collection`）；ingest 结果回写 `vector_collection` / `vector_dimensions`，删除与重试按该集合清理；`core/management/commands/reindex_knowledge.py`：按当前模型重新嵌入（默认跳过已在目标集合的文档，`--force` 重嵌、`--dry-run` 只报告、`--document` 定向重试，单文档失败不中断整批）；`core/management/commands/prune_qdrant_collections.py`：只列举或删除配置命名空间下、未被任何 `KnowledgeDocument.vector_collection` 引用的旧集合，默认 dry-run，必须显式 `--apply` 才删除；本地余弦相似度计算 | 【基础】 | 【已验证】真实 Qdrant v1.20.2 实例通过集合创建/列举/删除、向量写入/检索；`tests/integration/test_qdrant_live.py` 覆盖嵌入服务调用、按模型建集合、ingest、retrieve 全链路（无 `QDRANT_LIVE_URL` 时跳过）；pgvector、S3-compatible 对象存储【待开发】（见 §7.3） |
| Token Budget | 预算预留、用量结算、防超额消耗 | `core/token_budget.py`、`core/token_budget_views.py`：租户级预算预留、结算、幂等、超额保护；含回归测试 | 【待验证】 | 多轮修复后本地测试全绿；需在并发 worker 与真实模型上报用量下复核结算一致性 |
| Task Queue / Scheduler | 缓存、任务队列、异步执行 | `apps/worker/worker_app/main.py` 轮询 `process_once`；任务认领与租约基于 PostgreSQL 记录；依赖含 `redis`，`settings.py` 仅定义 `REDIS_URL` | 【基础】（DB 队列） | 【待开发】Redis 实际作为缓存 / 队列 / 协调通道：代码中未见 `CACHES`、channel_layer 或 redis 客户端调用；队列故障恢复、积压与重放需继续验证 |
| Audit / RBAC | 权限、租户隔离、操作记录 | `core/membership_views.py`：tenant/project 成员与角色（admin/editor/viewer）；视图经 `memberships__user` 限定租户、非管理员按 project membership 二次过滤；`core/audit.py`：租户链与全局链两套链式哈希（`verify_audit_chain` / `verify_global_audit_chain`），`tenant` 可空以承载无租户上下文的认证事件（迁移 `0024_audit_event_nullable_tenant`）；`core/auth_views.py` 登录成功 / 登录失败 / 登出三类事件写入全局链；落库覆盖 agent-node / agent-plugin / cluster-deployment / membership / plugin / tool-approval / model / knowledge / agent / workflow / task / token-budget / tenant / project 写接口 + 认证事件 | 【基础】 | 平台侧写接口与认证事件均已入链；全局链 list / verify 已提供（`GET /api/v1/audit-events/global/` 与 `/verify/`，仅平台管理员只读）；前端 Audit 页已展示全局认证链及校验结果，非平台管理员静默隐藏；更细粒度资源/工具策略闭环待完整覆盖 |
| Realtime Logs / Events | Agent、Workflow、部署过程实时日志 | 后端 `core/realtime.py`：Task / AuditEvent `post_save` 后通过 PostgreSQL `pg_notify` 发布项目事件；SSE `GET /api/v1/tenants/<tenant>/projects/<project>/events/stream/`（`core/realtime_views.py`），PostgreSQL 用 LISTEN 唤醒，SQLite 用有界查询回退；Channels WebSocket `WS /ws/tenants/<tenant>/projects/<project>/events/`（`core/consumers.py`，cookie 或 `session.<token>` 子协议认证、5 秒握手超时、4403 不重试），前端 `subscribeProjectEvents` 以 WebSocket 为主、握手失败或 4403 时回退 SSE；任务列表已改为事件驱动 | 【基础】 | 任务/审计事件已实时；Agent、Workflow、部署过程日志需继续接入同一事件通道，其他页面仍以请求拉取为主。GitHub Codespaces 的 `*.app.github.dev` 代理会缓冲 SSE，故以 WebSocket 为主通道 |
| Multi-Agent 接入 | 外部 AI Agent 以插件方式接入协同工作 | 现有接入形态：第三方能力封装为连接器工具（HTTP / OpenAPI schema / MCP 远端工具），经 tool router 权限/审批后由 Agent/Workflow 调用；MCP 工具可经 `sync-tools` 发现导入后人工启用 | 【基础】（工具级接入，含 MCP 工具导入） | 【待开发】把外部 AI Agent 作为可编排节点、可返回结构化结果并参与状态流转的完整接入协议；MCP Agent 的会话级接入（A2A 类协议）待设计 |

### 3.1 计划支持的插件与模型接入（当前真实状态）

| 类别 | 规划项 | 代码现状 | 三态 |
|---|---|---|---|
| 地图 | 高德、百度地图、Google Maps | 前端 `src/domain/connectorTemplates.ts` 含 amap places/geocoding/routing、baidu places/geocoding、google places 的 HTTP 模板元数据 | 【基础】（模板/HTTP 契约声明）；真实连接、鉴权与后端调用验收待验证 |
| 天气 | 天气 API / 天气服务 | 模板含 open-meteo（HTTP，无需 key） | 【基础】（模板）；真实连接器待验收 |
| 研发协作 | GitHub、Jira | 模板含 `github-readonly`（HTTP）；无 Jira 模板或专用执行器 | GitHub 只读【基础/待验证】；Jira【待开发】 |
| 办公协作 | Microsoft 365、Teams | 模板含 `microsoft-graph-readonly`（HTTP）；无 Teams 专用连接器 | 【基础】（只读模板）；完整 Graph 范围、OAuth 与 Teams 集成【待开发】 |
| 扩展能力 | MCP、第三方 API、自定义插件 | 连接器目录/安装/授权/连接测试框架存在；执行器支持 HTTP / OpenAPI / MCP（streamable HTTP + 工具发现导入） | 框架【基础】；MCP 运行时【基础/待验证】（HTTP、stdio、服务端模式已实现，OAuth【待开发】）；自定义插件隔离执行【待开发】 |
| 模型 | 国内外供应商、OpenAI-compatible、路由与降级 | 网关仅 `openai_compatible` + `fake`；OpenAI-compatible 通道可复用接入各家兼容端点 | 【基础】（兼容通道）；供应商覆盖、显式路由、主备降级【待开发】 |

> 以上连接器模板属于产品规划与元数据声明，不代表对应 OAuth 流程、真实供应商联调和生产级凭据托管已经完成。

## 4. 基础设施部署层：三个 Agent 实际分工

### 4.1 代码事实（权威，用于统一文档口径）

| Agent | 目录 | 已实现动作 / 模块 | 三态 | 缺口 |
|---|---|---|---|---|
| Cloud Control Agent | `apps/cloud_control_agent/`（`api.py`、`main.py`、`deployment_controller.py`） | HTTP 控制面：action/role 合法性校验、上报 capabilities 校验、checkpoint actor 迁移校验、证书 CN 身份校验、部署状态汇总与 DB 持久化；结果驱动自动推进在成功且 checkpoint 已推进后规划并幂等下发下一步，按阶段作用域幂等键去重，Agent 注册时补发待推进步骤 | 【基础】 | 【基础】平台 API 已提供运维配置的 OpenStack Nova VM 编排（`core/cloud_vm.py`、`/cloud-vms/`），支持列举、创建、删除、启停/重启并回写状态；阿里云 / 腾讯云 / AWS 适配器【待开发】，OpenStack 真机【待验证】 |
| EPH Agent | `apps/eph_agent/`（`main.py`、`bootstrap_renderer.py`、`control_stack.py`、`control_stack_adapter.py`、`metal3_skill.py`、`capi_renderer.py`、`ntp_policy.py`、`templates/`） | 动作：`eph.preflight`、`eph.source.observe`、`eph.bootstrap.render`（kubeadm init / NoCloud seed / Agent Deployment 纯渲染，返回产物摘要，无集群写入）、`eph.controlstack.apply`（cert-manager→CAPI→BMO→Ironic→CAPM3 幂等 apply + 就绪探针）、`eph.metal3.bmh.apply`/`observe`/`wait_ready`、`eph.capi.cluster.apply`/`observe`/`wait_ready`、`eph.target.agent.bootstrap`（密封 Target Agent bundle 应用、rollout 与目标集群身份校验）；上报的 capabilities 由 `packages/agent_contracts/orchestration.py` 单一来源生成 | 【基础】 | 【待验证】单节点 Bootstrap 集群与目标 Control Plane 引导在真实 CAPI/Metal3/Ironic 版本上验收；EPH 目录下**没有** pivot 适配器（见 §8 冲突清单） |
| Target Agent | `apps/target_agent/`（`main.py`、`capi_pivot.py`、`handoff.py`、`metal3_ownership.py`、`network_plugins.py`、`network_stack_adapter.py`、`cleanup_scope.py`、`eph_cleanup.py`、`worker_provisioning.py`、`postflight.py`、`ntp_attestation.py`、`ntp_policy.py`、`ironic_client.py`、`leader_election.py`、`worker_attestation.py`） | 动作：`target.cluster.observe`、`target.capi.observe`、`target.capi.pivot`（含 dry-run）、`target.metal3.observe`/`verify_ownership`、`target.worker.ntp.validate`、`target.ntp.control_plane.render`/`verify_bootstrap`、`target.ntp.internal.probe`、`target.network.apply`（Calico/Multus/OVS/SR-IOV/DPDK 幂等 apply + 就绪观察）、`target.handoff.evaluate`、`target.eph.cleanup`、`target.worker.provision`；Pivot 适配器、自主接管状态机、BMH 身份集与 ownership 校验、EPH 清理范围、Worker provisioning、NTP 证明、三副本 Leader Election、Ironic HTTP 客户端 | 【基础】 | 【待验证】真实集群就绪后自主接管、CAPI Pivot、BMH 迁移、EPH 清理、物理 Worker 部署与硬件验收；当前多为 fail-closed 预检 + 单测/mock 覆盖，真实 provider 矩阵未跑通 |
| Platform Worker | `apps/worker/worker_app/` | 通用 AI Agent 任务与 Workflow 异步执行（模型调用、工具路由、RAG 检索、工作流运行） | 【基础】 | 定位边界：只承担平台侧通用编排执行，不代替上述三个基础设施 Agent |

### 4.2 责任边界唯一标准（锁定口径，代码以此为准）

| 角色 | 最终职责 | 不承担 |
|---|---|---|
| Cloud Control Agent | 接收任务、全局编排、授权、预算、派发、状态汇总、失败协调 | 不审批 Target takeover；不直接执行裸金属变更 |
| EPH Agent | 单节点 Bootstrap、源集群预检查、引导目标 Control Plane | 不执行 CAPI Pivot、不做 Target 接管 |
| Target Agent | 目标集群就绪后自主接管、CAPI Pivot、BMH 迁移、EPH 清理、Worker 部署与最终验证 | 不需要 Cloud Control 审批接管动作 |
| Platform Worker | 通用 AI Agent 与 Workflow 异步执行 | 不代替基础设施 Agent 操作集群/裸金属 |

## 5. 部署生命周期状态机与网络 / NTP 现状

### 5.1 生命周期七步（核心业务状态机）

实现位置：平台通过 `core/cloud_control_bridge.py` 创建并派发 Cloud Control 部署；相位、命令、重试和检查点绑定位于 `apps/cloud_control_agent` 与 `packages/agent_contracts/orchestration.py`。旧的 `core/cluster_deployment_services.py` 链路及其 Target handoff/checkpoint API 已移除。

| 步骤 | 目标流程 | 代码现状 | 三态 |
|---|---|---|---|
| 1. 任务创建与资源检查 | Cloud Control 接收请求，检查物理机、BMC、网络、镜像、参数 | 部署单创建、节点/BMC/参数校验、兼容性判定 | 【基础】 |
| 2. 部署单节点 EPH K8s | 唯一 Bootstrap 集群，非每台物理机一套 EPH | EPH preflight / source observe / CAPI cluster apply | 【待验证】 |
| 3. EPH 引导目标 CP | 用 CAPI/Metal3/Ironic 部署目标 K8s Master | EPH CAPI cluster observe、模板渲染；provider 版本兼容性预检 | 【待验证】 |
| 4. Target Agent 接管 | CP Ready 后自主接管，无需 Cloud 审批 | Target handoff 评估、集群身份/controller 就绪校验、checkpoint CAS | 【待验证】 |
| 5. 清理 EPH 并迁移 BMH 管理权 | 清理 Bootstrap，处理 BMH 清单、ownership、资源归属 | `metal3_ownership.py`、`cleanup_scope.py`、`eph_cleanup.py`：身份集与范围校验 | 【待验证】 |
| 6. 部署目标 Worker | Target 驱动 Worker 部署并验证节点状态 | `worker_provisioning.py`、`worker_attestation.py`、`postflight.py` | 【待验证】 |
| 7. 最终验收 | 节点 Ready、CNI、SR-IOV、DPDK、NTP、Ironic、集群网络 | postflight 就绪判定与证据收集；DPDK/数据面转发未见真实验收 | 【待验证】；数据面部分【待开发】 |

### 5.2 网络分层

| 面 | 目标组件 | 代码现状 | 三态 |
|---|---|---|---|
| Control Plane（普通 NIC） | API Server / etcd / 管理接口 / Ironic 内部服务 | 部署规划与 NTP 源策略区分控制面/数据面 | 【基础】 |
| Data Plane（SR-IOV/DPDK） | Pod 数据流量、VF、多网卡 | SR-IOV Device Plugin 配置校验契约（`packages/agent_contracts/sriov_device_plugin.py`）、视图持久化、postflight 资源名核对；Calico/Multus/DPDK 以兼容性判定字段出现（只读校验） | SR-IOV 配置【基础/待验证】；OVS/Calico/Multus/DPDK 真实安装、升级、回滚与真实数据面流量验证【待开发】 |

### 5.3 NTP 规则

- Control Plane：使用外部 NTP。
- Worker：chronyd 必须使用目标 K8s Control Plane 的 Ironic 内部 IP/VIP 作为时间同步源，禁止外部 IP 池与回退。
- 代码现状：`eph_agent/ntp_policy.py`、`target_agent/ntp_policy.py`、`target_agent/ntp_attestation.py` 实现策略校验与内部源探测；生成 chrony 主机命令 / 模板。
- 三态：策略校验与命令生成【基础】；生成的主机命令尚未注入 CAPI bootstrap 模板、尚未在物理主机执行，真实 NTP 同步验收【待验证/待开发】。

## 6. 前端 GUI 页面盘点（`apps/web-studio/src/components/`）

验收标准：每个按钮连接真实 API、能持久化、执行操作并正确处理失败态；仅有静态展示不算完成。

| 页面 | 组件 | 代码情况 | 三态 | 缺口 |
|---|---|---|---|---|
| Login | `LoginView.vue` | 接 `/api/v1/auth/csrf\|login\|logout\|me` | 【基础】 | 完整认证体验、错误态与会话恢复待核对 |
| Dashboard | `DashboardView.vue` | 拉取真实 tasks / nodes 分页，computed 派生活跃/成功/在线指标 | 【基础】 | 指标来自任务/节点表，非独立监控指标；趋势与实时刷新待补 |
| Agent Studio | `AgentStudioView.vue` | 接 agents / publish / runs / 模型测试 | 【基础】 | 多步工具执行过程展示与失败恢复待完善 |
| Workflow Studio | `WorkflowStudioView.vue` | 可视化 DAG 画布：分层布局、节点拖动、节点检查器、连线编辑与 JSON 兜底；节点右侧端口可拖拽连线（条件节点自动分配 true/false 分支），节点坐标写入 `draft_definition.ui.node_positions`；支持审批决策 | 【已实现】（可视化编辑/校验/审批/循环/拖拽连线/坐标持久化） | 【待验证】大规模图的连线交互与坐标冲突合并 |
| Model Manager | `ModelManagerView.vue` | 接 model-providers / models / model-configs test | 【基础】 | 供应商覆盖、路由与降级策略 UI 待补 |
| Integration Hub | `IntegrationHubView.vue` | 接 connectors / installations / connections / test / status | 【基础】 | 真实连接器、OAuth 授权流与连接健康闭环待补 |
| Knowledge Base | `KnowledgeBaseView.vue` | 接 knowledge-bases / documents / retry | 【基础】 | 完整 RAG 链路（嵌入/检索/召回质量）待核对 |
| Infrastructure Agents | `InfrastructureAgentsView.vue` | 接 agent-nodes / commands / approve | 【基础】 | 真实部署闭环、阶段证据与失败态待验证 |
| Cloud VMs | `CloudVmsView.vue` | 接 cloud-vms/providers、列表刷新、创建、启停/重启、删除 | 【已实现】（operator-configured OpenStack Nova） | OpenStack 真机验收依赖真实云账号 |
| Metal3 Fleet | `Metal3FleetView.vue` | 接 fleet machines / BMC 配置 / lifecycle-review | 【基础】 | 物理设备操作与硬件验收待验证 |
| Governance | `GovernanceView.vue` | 接 tool-approvals / approve / reject | 【基础】 | 完整策略闭环（审批规则、越权拦截）待补 |
| Audit | `AuditView.vue` | 接 audit-events / verify 与全局 audit-events/global | 【基础】 | 平台侧写接口与认证事件均已入链；租户链与全局认证链均在前端展示并通过校验；全局链仅平台管理员可见，403 时静默隐藏 |
| Organization | `OrganizationView.vue` | 接 tenants / projects / members | 【基础】 | 邀请流程、角色变更通知等仍需完善 |
| Docs & Matrix | `DocsAndMatrixView.vue` | 静态阶段矩阵 + 外链 verification-matrix，不调 API | 【基础】（文档展示） | 矩阵状态必须由真实验收证据驱动，需持续更新 |
| SR-IOV Editor | `SriovDevicePluginEditor.vue` | 子组件，被部署视图引用；不直接 import api.ts | 【基础】 | 配置与后端持久化的端到端校验待核对 |
| Workspace | `WorkspaceView.vue` | 工作区容器/导航 | 【基础】 | — |

> 共 15 个 `.vue` 组件；其中 13 个直接引用 `services/api.ts`，`DocsAndMatrixView` 为静态矩阵页，`SriovDevicePluginEditor` 为父组件复用的编辑子组件。

## 7. 后端服务与数据架构

### 7.1 服务目录

| 服务 | 目录 | 作用 | 三态 |
|---|---|---|---|
| Platform API | `apps/platform-api` | Django REST API、认证、权限、数据管理 | 【基础】 |
| Worker | `apps/worker/worker_app` | 异步任务、通用 Agent 执行、Workflow 执行（`tasks.py` / `main.py`） | 【基础】 |
| Cloud Control Agent | `apps/cloud_control_agent` | 云端部署控制面（协议校验/状态汇总） | 【基础】；真实云编排【待开发】 |
| EPH Agent | `apps/eph_agent` | Bootstrap 集群操作与源侧观察 | 【基础/待验证】 |
| Target Agent | `apps/target_agent` | 目标集群接管、Pivot、清理、Worker 部署 | 【基础/待验证】 |
| Agent Contracts | `packages/agent_contracts` | Agent 身份/命令/租约/心跳/checkpoint 契约；SR-IOV 配置校验契约 | 【基础】 |
| Agent Transport | `packages/agent_transport` | mTLS 通信、租约续约、传输客户端 | 【基础】 |

注：`worker_app/capi_pivot.py` 为未被 `tasks.py` / `main.py` / 测试引用的预留残留模块，当前不参与执行链路；正式 Pivot 适配器位于 `apps/target_agent/capi_pivot.py`。

### 7.2 部署清单

- `deploy/agents/`：三 Agent 的 K8s manifest（cloud-control / eph / target-cluster-agent）、namespace、构建与部署脚本。
- `deploy/cloud-control-plane/agent-mtls/`：haproxy / nginx mTLS 配置与部署文档。
- `deploy/docker-compose.yml`、`deploy/docker-compose.cloud.yml`：本地与云端 compose 配置（CI 仅做 `config` 校验）。

### 7.3 数据层现状

| 存储 | 定位 | 代码现状 | 三态 |
|---|---|---|---|
| PostgreSQL | 业务数据、任务、Agent 状态、部署记录、检查点 | `DATABASES` 经 `dj-database-url` 配置；业务模型与 checkpoint 持久化落地 | 【基础】 |
| Redis | 缓存与协调，不应作为唯一持久化源 | `settings.py` 仅定义 `REDIS_URL`；业务代码未见缓存/队列/通道实际调用 | 【待开发】（接入并验证故障恢复） |
| pgvector | 计划中的向量检索存储 | 代码未使用 pgvector；当前向量对接 Qdrant HTTP 接口 | 【待开发】（规划项，勿记为已接入） |
| S3-compatible | 计划中的文档/对象存储 | 代码未见 boto3 / S3 客户端 | 【待开发】（规划项） |
| Secret Manager / Vault | 凭据托管、轮换、授权解析 | 凭据引用解析（env 引用 / bearer）、BMC 凭据、Agent 证书申请与轮换接口；`BMC_VAULT_TIMEOUT_SECONDS` 正数校验 | 【基础】；完整生产级密钥托管、自动轮换、细粒度授权解析【待完善】 |

## 8. 文档与代码职责冲突清单（需统一到 §4.2 口径）

**代码事实**：CAPI Pivot 适配器位于 `apps/target_agent/capi_pivot.py`，由 `apps/target_agent/main.py` 第 25 行 `from .capi_pivot import execute_capi_pivot` 接线；Cloud Control 派发的动作是 `target.capi.pivot.dry_run` / `target.capi.pivot`；`apps/eph_agent/` 目录下不存在 `capi_pivot.py`。

> **状态（2026-11-18 对齐）**：下表所列旧口径已在本次文档修订中统一到 §4.2 责任边界；"冲突表述"列仅保留修订前措辞以便追溯，现行口径以"应改为"列为准。

以下文档此前按旧口径写成「EPH 执行 source-side Pivot」，与代码不符，现已统一修订：

| 文件 | 位置 | 冲突表述 | 应改为 |
|---|---|---|---|
| `README.md` | "Authoritative three-Agent" 第 2 条 | EPH Agent owns source-side CAPI Pivot operation | EPH 仅做 Bootstrap / 源侧预检查 / 引导目标 CP；Pivot 归 Target Agent |
| `README.md` | "Not yet production-complete" 段 | "a new EPH CAPI Pivot adapter" | Pivot 适配器在 Target Agent；EPH 侧无 Pivot 适配器 |
| `docs/adr/0004-separate-cloud-and-node-agent-runtimes.md` | 第 17 行 | EPH owns source-side CAPI Pivot operation | 同上，统一为 Target 归属 |
| `docs/adr/0005-three-agent-fresh-runtime.md` | 第 17 行 | EPH owns … source-side CAPI Pivot operation | 同上；注意同文件第 36、40 行已是 Target 口径，文档内部自相矛盾，需对齐 |
| `docs/capi-pivot-contract.md` | 标题、第 3、7、8 行 | "EPH CAPI Pivot contract"；称由 `apps/eph_agent/capi_pivot.py` 实现，动作 `eph.capi.pivot.dry_run` / `eph.capi.pivot` | 适配器路径改为 `apps/target_agent/capi_pivot.py`；动作改为 `target.capi.pivot(.dry_run)` |
| `docs/dual-agent-infrastructure-architecture.md` | 第 8 行 | EPH performs … approved source-side CAPI Pivot operation | 同上，统一为 Target 归属 |
| `docs/eph-node-lifecycle.md` | 第 42、44、68 行 | EPH Pivot adapter 执行 `clusterctl move`；引用 `apps/eph_agent/capi_pivot.py` | Pivot 步骤归属 Target Agent；EPH 仅保留 Bootstrap / 清理前观察 |
| `docs/agent-command-api.md` | 第 25 行 | "The Node Agent runs `clusterctl move --dry-run`"（Node Agent 已退役） | dry-run 模拟与只读检查归 Target Agent；Node Agent 表述移除 |

> 修订原则：代码与新文档一律以 §4.2 责任边界为准；历史 ADR 如保留旧决策，应标注 superseded，避免新旧口径并存。

## 9. 已具备基础能力 vs 未达生产完成清单

### 9.1 已有代码实现（含单测/mock 覆盖，不等于真实环境验收通过）

- 三 Agent 独立运行时、独立 Dockerfile 与部署清单。
- Agent 身份、命令、租约、心跳、检查点、结果协议（contracts + transport）。
- Agent mTLS 通信，证书身份绑定注册的 Agent UUID。
- Target Agent 多副本部署、Pod 反亲和、PDB、Kubernetes Lease Leader Election。
- CAPI Pivot 的前置校验与安全门禁（集群身份、controller 就绪、版本兼容、BMH 身份集、审批摘要绑定）。
- BMH Inventory、ownership 与 EPH 清理范围校验。
- Worker provisioning 与内部 Ironic NTP 地址校验 / 证明。
- SR-IOV Device Plugin 配置校验与资源匹配检查。
- Token 预算预留、结算、幂等与超额保护。
- Agent、Workflow、插件、知识库相关 API 与前端页面基础。
- 租户/项目成员隔离、角色校验、链式哈希审计记录与 verify（租户链 + 全局认证链）。
- MCP 客户端运行时（streamable HTTP 与 stdio JSON-RPC、工具映射、SSE 响应、超时与 allowlist 门禁）、项目级 MCP Server（`/api/v1/tenants/<tenant>/projects/<project>/mcp/`）与工具发现导入接口。
- Qdrant 集合自举（幂等建集）、readiness 探针、按 embedding 模型 / 维度版本化的集合绑定与记录。
- `manage.py reindex_knowledge`：按当前 embedding 模型重新嵌入历史文档并回写集合与维度，支持 dry-run、定向重试与逐文档失败隔离。
- 全局审计链（认证事件）的只读对外接口：`GET /api/v1/audit-events/global/`（分页列表）与 `GET /api/v1/audit-events/global/verify/`（链校验），仅平台管理员（`is_staff` / `is_superuser`）可读。
- 项目级实时事件：Task / AuditEvent 提交后 `pg_notify`，Channels WebSocket 与 SSE 端点均消费同一事件源；前端优先 WebSocket、SSE 回退；SQLite 下为有界轮询，PostgreSQL 下为 LISTEN 唤醒。

### 9.2 仍未达到生产完成（需继续开发或真实环境验收）

- 与真实 MCP Server 的双向联调（客户端与服务端实现均已落地，尚未接入外部真实 Server 做端到端验收）。
- 完整第三方连接器与 OAuth 凭据授权、token 刷新流程。
- 更细粒度的资源级/工具级授权策略闭环。
- Workflow 拖拽画布、并行、循环、人工审批恢复等高级节点。
- 插件隔离执行、SSRF 防护的全量供应商集成验收。
- 完整 GUI 与参考仓库逐页面、逐交互一致（按钮接真实 API、持久化、失败态）。
- 网络插件（Multus / SR-IOV CNI·Device Plugin / OVS / Calico / DPDK）完整安装、升级与回滚。
- CAPI / Metal3 / Ironic 在真实环境的完整部署、Pivot 与 BMH 接管。
- EPH 实际清理、物理 Worker 部署与硬件验收。
- SR-IOV / DPDK 真实数据面流量验证。
- 已实现的升级、回滚、恢复与工作节点扩缩容在真实 CAPI / Metal3 集群上的验收；Day-2 修复编排仍未完成。
- Redis 缓存/队列/协调通道落地与故障恢复；pgvector、S3 对象存储接入。
- 模型多供应商路由、主备降级与国内外供应商原生适配。

## 10. 建议的后续清理与补齐顺序

1. **先统一口径**：按 §8 修订冲突文档与 README，把 Pivot 责任固定到 Target Agent；`docs/verification-matrix.md` 与前端 `DocsAndMatrixView` 的状态只填有测试/环境证据的项。
2. **GUI 功能映射核对**：逐页面、逐按钮对照 §6 清单，确认 API 接线、持久化与失败态；把静态展示与 JSON 编辑区域标记为未完成。
3. **后端补齐**：Redis 通道接入与故障恢复、全局审计链的前端展示、旧集合清理策略（reindex 命令已落地）、模型路由/降级、连接器 OAuth。
4. **Agent 真实链路验收**：在选定的 CAPI / Metal3 / Ironic / OS 版本矩阵上，按 §5.1 七步状态机逐阶段补端到端证据；fail-closed 的预检逐步替换为真实执行适配器。
5. **数据面与 Day-2**：Multus/SR-IOV/OVS/Calico/DPDK 安装回滚、真实 VF 与流量验证；扩容/缩容/升级/修复运维闭环。

---

*本盘点为代码现状梳理，不构成生产可用性声明；任何"已支持"的对外表述都应附带具体环境、操作与可观察到的后置条件证据。*
