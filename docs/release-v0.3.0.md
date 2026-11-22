# 版本说明 v0.3.0 — 裸金属交付链可部署基线

- 版本号单一来源：`pyproject.toml` 的 `[project] version`，由 `packages/version.py::project_version()` 读取；`/api/v1/` 健康入口、发布清单 `metal3_release_evidence` 均取该值，前端 `package.json` 同步。
- 镜像构建时可用环境变量 `AI_AGENT_STACK_VERSION` 覆盖（元数据被裁剪的镜像仍能报出正确版本）。
- 本版本定位：**代码基线完整、可部署、可自检**。代码层面全部通过自动化验证；物理机验收项单独列出，不并入“已验证”。

## 1. 这一版包含什么

### 1.1 平台层（SaaS）

- 多租户：租户、项目、`admin/editor/viewer` 角色，服务端隔离。
- Agent 管理：定义、版本、发布、运行记录；多 Agent 团队（顺序、协调者、并行扇出、路由四种策略）。
- 工作流引擎：`start` / `llm` / `agent` / `condition` / `knowledge_search` / `tool` / `approval` / `loop` / `end`，并行 any/all join、单审批人恢复、带检查点的有界循环。
- 模型接入：OpenAI 兼容网关（chat 与 embeddings）。
- 工具与集成：HTTP、OpenAPI、MCP streamable-HTTP，工具审核与权限授予。
- 知识库：按 embedding 模型与维度分版本的 Qdrant collection。
- 异步执行：PostgreSQL Worker 领取、租约、心跳、重试、取消；变更类命令 at-most-once。
- 用量治理：按租户 token 预算预留、结算、超支保护。
- 审计：租户与全局链式哈希审计，提供校验接口。
- 控制工作区：安装级项目 + 版本化 skill + 插件引用 + 任务/DAG + 租约式 work。

### 1.2 三个基础设施 Agent

| Agent | 职责 | 关键点 |
| --- | --- | --- |
| Cloud Control Agent | 平台侧编排 | 部署计划、预检汇总、审批、命令派发、状态机对账 |
| EPH Agent | 应急/引导主机 | 裸金属编排（Metal3/Ironic）、就绪检查、Live-OS、本地注册表、控制栈清单 |
| Target Agent | 目标集群内执行 | Kubernetes 升级、回滚、节点网络观测、CAPI worker 供给 |

约束：物理裸金属，**不使用 libvirt/KVM** 作为部署前提；BMC 覆盖 HPE / Dell / Inspur / H3C / Lenovo；交换机 VLAN 与端口不参与校验。

### 1.3 Metal3 交付链

- 网络契约（`packages/agent_contracts/infra_networks.py`）：profile → `networkData`（control_plane / worker / storage 三套）、IPPool、SR-IOV NodePolicy、派生 network spec。
- 本版网络口径：**一条 control 链路（`bond_control`）统管 BMC/vbmc、PXE、IPA、Live-OS、control plane**；**VLAN 70 叠在 `bond_control` 上作为 provisioning 网络**；data 链路为独立 bond（802.3ad），其上叠存储/租户 VLAN。
- 网络规则已收敛到合约层（不再由前端各自推算）：
  - profile 可直接用子接口名（`bond_control.70`）声明 VLAN，归一化后由合约推导 `<base>.<vlan>`，networkData / PXE 端口解析 / 主机链路检查三处一致；子接口不会被当成物理网卡。
  - 只加入 VLAN 的角色（storage、SR-IOV）会把所叠的 bond 及其成员一并渲染，避免 nmstate 在缺失基链路时启动失败。
  - `pxe_boot_ports` 走一跳 VLAN 回到物理成员端口；bond/VLAN 永不作为 `bootMACAddress`。
  - 数据 bond 成员可由 PXE 采集的 `pci_addresses` 派生为 `pci@<addr>`，界面不必手敲网卡名；两者皆空仍拒绝，模板不会渲染无成员 bond。
- PXE 数据回流：`FleetDiscoveryView` 触发全舰队 Redfish 一次性 PXE 引导并采集；库存写入 `hardware_inventory`，用于自动填充模板（boot MAC、SR-IOV PCI、端口核对）。
- Ironic 模板（`packages/agent_contracts/ironic_template.py`）：从 provisioning 网络派生 `interface` / `ip_address` / `dhcp`，只允许预填 EPH 自持地址或子网内确定性首个可用地址；**镜像地址永远留空**，列在 `manual_fields` 由界面手填。
- BMC 网络预检（`packages/agent_contracts/bmc_network.py`）：BMC 必须落在 control 子网；模拟器端口与 vbmc/sushy 实际发布端口比对；结果进入 `preflight_result.network_defaults.bmc_network` 并展示在前端。
- 端口库存核对（`port_inventory_findings`）：profile 声明的物理端口与采集库存逐端口比对，返回 `missing_on` 与告警。
- 验收与发布门禁：`tools/metal3_acceptance.py`（preflight / redfish / verify / rollback，全部只读）、`tools/metal3_release_run.py`（按序跑完全部证据阶段）、`tools/metal3_release_evidence.py`（fail-closed 发布清单，含证据哈希、commit、`product_version`）。

### 1.4 Kubernetes 升级与回滚

- 升级：控制面滚动（本地 `kubeadm upgrade apply` + 逐 peer 包/kubeadm/node/kubelet），etcd quorum 在过程中持续守卫，`ssh.reachable` 与 `ssh.sudo` 双预检。
- **版本判定用完整三元组**：`v1.34.0` 不再被当作满足 `v1.34.3`，补丁版本不会被跳过却报成功。
- 回滚：多控制面逐成员恢复（peer URL ↔ 节点匹配，含 IPv6 括号形式）；快照只允许位于 etcd Pod 可见的 `/var/lib/etcd` 下；远端 staging 为 `scp → /tmp` + `sudo install -m 0444` + 可读探测。
- **失败不再把集群留在停摆状态**：统一 `_fail` 先换回已替换目录，再本地优先重启所有被停 kubelet，最后探测 apiserver；结果带 `recovery{...}` 与三选一的 `next_step`。
- 审批：变更类操作（升级/回滚）需要平台审批；EPH/Target 执行前不额外审批。

### 1.5 前端（`apps/web-studio`）

- 基础设施页：网络 profile 编辑（bond 成员与 SR-IOV PCI 从采集库存下拉自动填充）、端口库存缺口展示、BMC fabric 展示、EPH preflight 结论与告警、部署详情页读取 `preflight_result`。
- Metal3 车队页：资产、BMC、固件、lab workflow 形态与最近一次运行状态（含物理 runner 标签检查）。
- 契约层统一后，前端不再自行计算 profile 派生值。

### 1.6 离线部署资产

- `deploy/offline/images.json`：离线镜像清单（Metal3/Ironic、CAPI、Kubernetes、Calico、Ceph/Rook、SR-IOV、vbmc/sushy 等）。
- `deploy/offline/addons/control-stack`：控制栈 vendor 清单与 kustomize 变量契约（有守卫测试）。
- `deploy/docker-compose.yml`（开发）与 `deploy/docker-compose.cloud.yml`（云端控制面）。
- `eph-iso/`：EPH Live-OS 构建入口。

## 2. 部署

### 2.1 开发 / 单机

```bash
cp .env.example .env
make dev        # docker compose up --build
make verify     # ruff + pytest + 前端测试 + 前端构建
make version    # 打印当前版本
```

### 2.2 离线金属机房

1. 在可联网机器上导出镜像：按 `deploy/offline/images.json` 拉齐并推送至内网 registry（`registry_endpoint` 改为现场地址）。
2. 现场按 `deploy/offline/README.md` 导入镜像、启动 compose 或云端控制面。
3. 网络按本文 1.3 口径在界面配置：`bond_control` 承载 control（untagged）与 VLAN 70（provisioning）；data bond 上叠存储/租户 VLAN；镜像地址在界面手填。
4. 触发舰队 PXE 采集，确认 `hardware_inventory` 回填后，预检里的端口/PCI/BMC 告警为空。

### 2.3 实验室验收（物理机，可后置）

```bash
tools/metal3_acceptance.py preflight --out evidence/preflight.json
tools/metal3_acceptance.py redfish   --out evidence/redfish.json
tools/metal3_acceptance.py verify    --out evidence/acceptance.json
tools/metal3_acceptance.py rollback --snapshot /var/lib/etcd/backups/<pre>.db \
    --out evidence/rollback.json
tools/metal3_release_run.py          # 一键跑完全部阶段并封发布清单
```

或在 Web 前端 Metal3 车队页手动 dispatch `.github/workflows/metal3-lab-e2e.yml`（runner 标签 `self-hosted, metal3-lab, physical`）。

## 3. 本版自动化验证结果

| 项目 | 结果 |
| --- | --- |
| pytest 全量 | 1666 passed / 15 skipped |
| ruff 全库 | All checks passed |
| 前端 `npm test` | 36 pass / 0 fail |
| 前端 `npm run build` | 成功（仅有既有 chunk 体积告警） |
| CI（GitHub Actions，含 Compose 全栈冒烟、kind + Metal3 CRD 集成） | success |

## 4. 待物理机验证清单（本版**未**声称已验证）

- Redfish 真实 BMC 应答与固件 `SimpleUpdate`（HPE / Dell / Inspur / H3C / Lenovo）。
- 真实固件 PXE 引导：`bootMACAddress` 取自 bond 成员端口是否能成功引导。
- 物理 `bond_control` + VLAN 70 的实际拼装与 Live-OS / IPA 取址。
- 真实 etcd 静态 Pod 快照的 `snapshot status` 与逐成员恢复。
- 多控制面回滚的 `ssh` 免密 + `NOPASSWD sudo` 通道。
- Rook-Ceph 与 SR-IOV 在真实网卡上的落地。
- 发布清单 `release-manifest.json` 的物理证据产出（需上述跑通后生成）。

## 5. 版本纪律

- 版本只改 `pyproject.toml` 一处（外加 `apps/web-studio/package.json` 保持同步）；`tests/test_health.py` 断言取自 `project_version()`，不再写死字面量。
- 发布清单记录 `product_version` + `commit` + 证据哈希，可追溯到具体提交。
- 物理验证结果进入 `evidence/` 与本文件 §4 的对应条目，未跑通前 §4 保持未勾选。
