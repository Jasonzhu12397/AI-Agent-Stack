# AI Agent Stack 1.0.0 — 发布说明（简体中文）

> 英文全文见 `docs/release-v1.0.0.en.md`。

本版本把「AI Agent SaaS 平台」与「三个 Agent 的物理裸金属交付链」收成**一个可部署、带版本号、可追溯的产物**：一套代码、一个版本号、一份离线镜像清单、一条 fail-closed 发布门禁。

- 版本号：`1.0.0`，单一来源是 `pyproject.toml`，由 `packages/version.py::project_version()` 读取；`/api/v1/`、发布清单、前端 `package.json` 全部一致。
- 许可证：Apache-2.0（见 `LICENSE`）。
- 双语：本文件为中文版，英文版为 `docs/release-v1.0.0.en.md`。
- 变更历史：`CHANGELOG.md`（本版本条目）。
- 本版本定位：**代码层 1.0**。自动化基线全绿；物理机验收项逐条列在 §6，未跑通前不勾选、不声称已验证。

---

## 1. 版本支持矩阵

| 维度 | 本版本声明 |
| --- | --- |
| Python | 3.12 |
| 后端 | Django 5.x + DRF + Channels/Daphne（HTTP/ASGI），PostgreSQL 16 |
| 前端 | Vue 3 + TypeScript + Vite + Element Plus + Pinia |
| 目标 Kubernetes | kubeadm 发行版本，升级/回滚按 `major.minor.patch` 三元组判定 |
| Metal3/Ironic 组件 | 见 `deploy/offline/images.json`（BMO、CAPM3、IPAM、IRSO、Ironic、IPA downloader、CAPI v1.14、Calico、Rook/Ceph、SR-IOV operator、vbmc/sushy） |
| BMC 厂商 | HPE iLO、Dell iDRAC、Inspur、H3C、Lenovo XCC（Redfish `UpdateService`） |
| 部署形态 | 物理裸金属，**不依赖 libvirt/KVM**；单控制面与三控制面均支持 |
| 网络口径 | 一条 `bond_control` 承载 BMC/vbmc、PXE、IPA、Live-OS、control plane；VLAN 70 叠在 `bond_control` 上作 provisioning；data 为独立 bond |
| 许可 | Apache-2.0 |

---

## 2. 架构拓扑

```
                    ┌──────────────────────────────────────────────┐
  浏览器 / API ─────▶│ Platform API (Django/DRF) + Web Studio (Vue) │
                    │ 租户/项目/RBAC · Agent·Workflow·Knowledge     │
                    │ 审计链 · 预算治理 · 审批 · 命令派发与对账      │
                    └───────────────┬──────────────────────────────┘
                                    │ HTTPS + mTLS（Agent 传输）
                    ┌───────────────▼──────────────┐
                    │      Cloud Control Agent     │  平台侧编排
                    └───────────────┬──────────────┘
                                    │
              ┌─────────────────────▼─────────────────────┐
              │                  EPH Agent                │  Live-OS / 引导主机
              │ Metal3·Ironic 编排 · 就绪检查 · live-OS    │
              │ 控制栈清单 · 本地注册表 · PXE 采集回流      │
              └─────────────────────┬─────────────────────┘
                                    │ 目标集群内命令（租约 + at-most-once）
                    ┌───────────────▼──────────────┐
                    │        Target Agent          │  目标集群内执行
                    │ 升级 / 回滚 / 节点网络观测    │
                    └──────────────────────────────┘
```

网络（与现场一致）：

```
bond_control (active-backup: eno1/eno2)
  ├── untagged            → control            172.18.0.0/24
  └── bond_control.70     → provisioning(VLAN70) 172.18.37.0/24  ← PXE / IPA / Live-OS

bond_data (802.3ad: pci@0000:3b:00.0 / .1)
  ├── bond_data.3220      → data (租户)        10.0.10.0/24
  └── bond_data.3200      → storage-internal   10.130.60.0/24
```

bond 与 VLAN 由操作系统拼装，**永远不作为** `bootMACAddress`；固件 PXE 只能从成员物理端口启动，`pxe_boot_ports` 走一跳 VLAN 回到成员端口，`boot_mac_from_inventory` 再从 PXE 采集库存取该端口 MAC。

---

## 3. 功能清单

### 3.1 SaaS 平台

- 多租户：租户 / 项目 / `admin·editor·viewer`，服务端隔离。
- Agent：定义、版本、发布、运行记录；异步 LLM 运行。
- 多 Agent 团队：顺序、协调者、并行扇出、路由四种策略。
- 工作流：`start·llm·agent·condition·knowledge_search·tool·approval·loop·end`；并行 any/all join、单审批人恢复、带检查点的有界循环。
- 模型：OpenAI 兼容网关（chat + embeddings）。
- 工具与集成：HTTP / OpenAPI / MCP streamable-HTTP；工具审核与权限授予。
- 知识库：按 embedding 模型与维度分版本的 Qdrant collection。
- 异步执行：PostgreSQL 租约队列、心跳、重试、取消；变更类命令 at-most-once。
- 用量治理：租户 token 预算预留/结算/超支保护；链式哈希审计与校验接口。
- 控制工作区：安装级项目、版本化 skill、插件引用、任务/DAG、租约式 work。

### 3.2 基础设施交付

- 部署生命周期状态机：预检 → 计划 → 审批 → 派发 → 对账 → 完成/失败。
- EPH：Metal3/Ironic 裸金属编排、就绪检查、Live-OS 引导、本地注册表、控制栈清单。
- Target：Kubernetes 升级、回滚、节点网络观测、CAPI worker 供给。
- BMC 固件：Redfish `UpdateService` 清点、管理员级 `SimpleUpdate`、幂等任务、task-monitor 轮询、审计。
- PXE 采集回流：一键触发全舰队 Redfish 一次性 PXE 引导并采集库存，回填 `hardware_inventory`。

### 3.3 Metal3 交付链

- 网络契约 `infra_networks`：profile → 三角色 `networkData`、IPPool、SR-IOV NodePolicy、派生 network spec。
- 规则收敛到合约层：VLAN 子接口归一化、只加 VLAN 的角色渲染其所叠 bond、PXE 端口穿透 VLAN、数据 bond 成员由 PCI 派生。
- Ironic 模板 `ironic_template`：从 provisioning 网络派生 `interface`/`ip_address`/`dhcp`；**镜像地址永远留空**并列在 `manual_fields`，由界面手填后推后端。
- BMC 网络预检 `bmc_network`：BMC 必须在 control 子网；模拟器端口与 vbmc/sushy 实际端口比对。
- 端口库存核对 `port_inventory_findings`：声明端口与采集库存逐口比对，给出 `missing_on` 与告警。
- 验收与门禁：`metal3_acceptance.py`（preflight/redfish/verify/rollback 全只读）、`metal3_release_run.py`（一键全阶段）、`metal3_release_evidence.py`（fail-closed 清单，含哈希、commit、`product_version`）。

### 3.4 Kubernetes 升级与回滚

- 升级：控制面滚动（本地 apply + 逐 peer），etcd quorum 过程守卫，`ssh.reachable` + `ssh.sudo` 双预检，完整版本三元组判定。
- 回滚：多控制面逐成员恢复，peer URL↔节点（含 IPv6），快照限定 `/var/lib/etcd`，远端 staging `scp → /tmp → sudo install -m 0444`。
- 失败恢复：统一 `_fail` 换回目录、本地优先重启 kubelet、探 apiserver，结果带 `recovery` 与三选一 `next_step`。

### 3.5 前端（Web Studio）

基础设施页（网络 profile 编辑、库存下拉自动填端口/PCI、端口缺口、BMC fabric、EPH 预检结论与告警、部署详情读 `preflight_result`）、Metal3 车队页（资产/BMC/固件/lab workflow 形态与最近运行，含物理 runner 标签检查）、治理 / 审计 / 集成中心 / Agent 与团队 / 工作流 DAG 等。

### 3.6 离线部署资产

`deploy/offline/images.json`（离线镜像清单）、`deploy/offline/addons/control-stack`（控制栈 vendor 与 kustomize 变量契约，有守卫测试）、`deploy/docker-compose.yml`、`deploy/docker-compose.cloud.yml`、`eph-iso/`（Live-OS 构建入口）。

---

## 4. 部署

### 4.1 开发 / 单机

```bash
cp .env.example .env
make dev        # docker compose up --build
make verify     # ruff + pytest + 前端测试 + 前端构建
make version    # 1.0.0
```

### 4.2 生产（云端控制面）

`deploy/docker-compose.cloud.yml`，机密由 `deploy/vault/` 与 `.env` 提供；生产必须设置非默认 `SECRET_KEY`、关闭 `DEBUG`，健康检查 `/health/live` 与 `/health/ready`。

### 4.3 离线金属机房

1. 联网机器按 `deploy/offline/images.json` 拉齐镜像并推送内网 registry（改 `registry_endpoint`）。
2. 现场导入镜像，启动 compose 或云端控制面。
3. 界面配置网络：`bond_control` 承载 control 与 VLAN 70 provisioning；data bond 叠存储/租户 VLAN；镜像地址手填。
4. 触发舰队 PXE 采集，确认 `hardware_inventory` 回填、预检端口/PCI/BMC 告警为空。
5. 提交部署计划 → 预检 → 审批 → 派发。

### 4.4 实验室验收（物理机）

```bash
tools/metal3_acceptance.py preflight --out evidence/preflight.json
tools/metal3_acceptance.py redfish   --out evidence/redfish.json
tools/metal3_acceptance.py verify    --out evidence/acceptance.json
tools/metal3_acceptance.py rollback --snapshot /var/lib/etcd/backups/<pre>.db \
    --out evidence/rollback.json
tools/metal3_release_run.py     # 全阶段 + 封发布清单
```

或在 Web 前端 Metal3 车队页 dispatch `.github/workflows/metal3-lab-e2e.yml`（runner 标签 `self-hosted, metal3-lab, physical`）。

---

## 5. 本版本自动化基线

| 项目 | 结果 |
| --- | --- |
| pytest 全量 | 1666 passed / 15 skipped |
| ruff 全库 | All checks passed |
| 前端 `npm test` | 36 pass / 0 fail |
| 前端 `npm run build` | 成功（仅既有 chunk 体积告警） |
| CI | Compose 全栈冒烟、kind + Metal3 CRD 集成、PostgreSQL/mTLS 集成、四个分片单测、镜像构建全部通过 |
| 版本一致性 | `/api/v1/`、`pyproject.toml`、`package.json`、发布清单均为 `1.0.0`（由 `tests/test_health.py` 动态断言） |

证据指向具体 run 与 commit，见 `docs/verification-matrix.md`。

---

## 6. GA 门禁：仍需物理机的项（本版本**未**声称已验证）

1. Redfish 真实 BMC 应答与固件 `SimpleUpdate`（HPE / Dell / Inspur / H3C / Lenovo 各一台）。
2. 真实固件 PXE 引导：`bootMACAddress` 取自 bond 成员端口能否成功进入 IPA。
3. 物理 `bond_control` + VLAN 70 拼装，Live-OS / IPA 取址正确。
4. 真实 etcd 静态 Pod 的 `snapshot status` 与逐成员恢复。
5. 多控制面回滚的 `ssh` 免密 + `NOPASSWD sudo` 通道。
6. Rook-Ceph 与 SR-IOV 在真实网卡上的落地。
7. `metal3_release_run.py` 产出 `release-manifest.json`（`status: pass`）——这是把 1.0.0 升级为「物理已验证」的唯一凭据。

任一项未过，发布说明与 §5 不得改写为已验证；跑通后按条勾选并附 run/commit。

---

## 7. 已知限制（有意保留的边界）

- 包降级不在范围：kubeadm 无受支持降级路径，回滚只恢复控制面状态。
- 交换机 VLAN / 端口不参与校验（现场负责）。
- WebSocket consumer 未实现，实时能力走 HTTP/轮询。
- OIDC/OAuth2、邀请邮件、密码重置、服务身份与细粒度资源/工具策略未实现。
- 真实 Qdrant 链路与预算并发压测待真实验证。
- `ironic` 镜像地址、凭证名、版本号属于人工字段，永不自动填充。

---

## 8. 发布产物

- Git tag：`v1.0.0`
- GitHub Release：本说明全文
- 源码包与校验：GitHub 自动生成的 `tarball`/`zipball`
- 许可证：`LICENSE`（Apache-2.0）
- 变更历史：`CHANGELOG.md`
