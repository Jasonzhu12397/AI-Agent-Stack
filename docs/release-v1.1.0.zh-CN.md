# AI Agent Stack 1.1.0 — 发布说明（简体中文）

> 英文全文见 `docs/release-v1.1.0.en.md`。

1.1.0 是 1.0.0 之后的第一个功能版本，主题是**无人值守的 etcd 备份**。1.0.0 只在升级和回滚前顺手做一次快照，集群装好之后就没有任何周期性保护；本版本把它变成一个真正的产品能力：合约、执行器、保留策略、调度器、平台只读视图、前端面板，一层不缺。

- 版本号：`1.1.0`，仍由 `pyproject.toml` 单一来源提供，经 `packages/version.py::project_version()` 读取。
- 双语：本文件为中文版，英文版为 `docs/release-v1.1.0.en.md`。
- 变更历史：`CHANGELOG.md`（`[1.1.0]` 条目）。
- 定位：**代码层功能版本**。自动化基线全绿；物理机侧的 etcd 快照验收仍未执行，见 §6。

---

## 1. 本版本解决什么问题

| 1.0.0 的现状 | 1.1.0 的做法 |
| --- | --- |
| 只有升级/回滚前临时快照，装在 `/var/lib/etcd/backups`，无周期、无保留、无上报 | 周期入队 + 校验后发布 + 数量/年龄双维度保留 + 只读上报 |
| 快照失败时可能留下半截文件，目录里看起来像有一份备份 | 先写 `.partial`，`snapshot status` 通过后才 `mv` 成正式名 |
| 备份多了没人清理，磁盘被吃满 | 保留策略由合约决定，永不删最新、永不删 1 小时内新文件、永不删回滚正在引用的快照 |
| 平台不知道备份是否新鲜 | 命令结果 + `GET .../etcd-backup/` + 前端 Backups 列，直接给出最新快照与年龄 |

---

## 2. 合约层：`packages/agent_contracts/etcd_backup.py`

- 命令面：`cluster.kubernetes.backup.run`（写文件，走租约、at-most-once，**不进审批队列**——需要人点头的备份不是备份）、`cluster.kubernetes.backup.status`（只读）。
- 请求归一化 `validate_request`：`cluster_name` 必须是 DNS label；`execution_mode` 取 `simulation` / `execute`，`execute` 必须回显 `confirm_cluster_name`；`directory` 必须落在 `/var/lib/etcd` 下且不含 `..`；`keep_count` 上下限 2–168（默认 2）、`keep_max_age_hours` 上限 8760（默认 8760，即只按数量保留）、`max_age_hours` 默认 26；未知键一律拒绝。
- 命名 `backup_filename` → `<cluster>-<yyyyMMddTHHMMSSZ>.db`，`partial_name` 追加 `.partial`，`parse_backup_name` 反向解析；`snapshot_entries` 只保留本集群、能解析的文件，外来文件不参与保留决策。
- 保留 `select_prunable`：按数量、按年龄两把尺子，叠加 `MIN_AGE_HOURS = 1` 与调用方 `protect` 集合；删除只能由这一个函数决定。
- 判定 `assess`：给出 `count` / `newest` / `newest_age_hours` / `prunable` / `findings`，缺备份报 `ETCD_BACKUP_MISSING`，过旧报 `ETCD_BACKUP_STALE`；`simulation_plan` 是四步（save → verify → publish → retention）。

## 3. 执行层：`apps/target_agent/etcd_backup_exec.py`

- `EtcdBackupExecutor._etcd_pod()` 在 `kube-system` 里挑 Running 的 `etcd-` Pod；`_etcdctl_argv()` 带 `/etc/kubernetes/pki/etcd` 的三份证书；`_list_directory()` 用 `ls -ln` 解析（busybox 兼容），目录不存在视为空。
- `run()` 五步 `prepare / snapshot_save / snapshot_verify / snapshot_publish / retention`；verify 失败会删掉 `.partial` 并**不进入** retention，所以坏备份永远不会导致好备份被删。失败码：`ETCD_BACKUP_PREPARE_FAILED` / `_SAVE_FAILED` / `_VERIFY_FAILED` / `_PUBLISH_FAILED` / `_POD_NOT_FOUND`。
- `status()` 只读，不落盘、不改文件，返回条目、最新年龄与 findings。

## 4. 调度与运维接线

- `python apps/platform-api/manage.py queue_etcd_backups`：遍历所有 `succeeded` 的 `ClusterDeployment`，每个部署按小时桶生成幂等键 `etcd-backup:<deployment_id>:<YYYYMMDDTHH>`，用部署的 `requested_by` 作为发起人，输出 JSON 报告 `{bucket, queued, skipped, dry_run, at}`。`--dry-run` / `--directory` / `--keep-count` / `--keep-max-age-hours` 可覆盖；`spec.kubernetes.etcd_backup: false` 或没有 Target Agent 的部署给出跳过原因而不排队。
- `deploy/docker-compose.yml` 与 `deploy/docker-compose.cloud.yml` 新增 `etcd-backup-scheduler`：每 `ETCD_BACKUP_INTERVAL_SECONDS`（默认 3600）跑一次该命令，`restart: unless-stopped`、`no-new-privileges`。外部 cron 调同一个命令也是安全的，小时桶让重复执行变成幂等。
- `GET /api/v1/cluster-deployments/<id>/etcd-backup/`（只读）：本部署声明的策略（`kubernetes.etcd_backup` / `etcd_backup_keep_count` / `etcd_backup_keep_max_age_hours` / `etcd_backup_max_age_hours` / `etcd_backup_directory`，全部过合约校验）、最新快照与年龄、是否过期、可清理集合、findings；策略非法（例如 `keep_count: 1`）在配置期就报出合约错误码，而不是凌晨三点才发现。
- 前端 Target Cluster Delivery 表新增 **Backups** 列：`fresh` / `stale` 标签、最新文件名与年龄、失败原因。

## 5. 升级与兼容

- 平台侧无需数据迁移：备份状态来自既有的 `AgentCommand.result`，调度器只写 `AgentCommand`。
- Target Agent 需要升级到本版本才会识别 `cluster.kubernetes.backup.run` / `.status`；旧 Agent 会以未知命令拒绝，调度器按普通失败记录，不影响交付链。
- 已有的 `/var/lib/etcd/backups` 里升级/回滚留下的快照会被 `snapshot_entries` 识别并纳入保留决策；如果回滚仍引用某份快照，请在恢复完成后再让它参与清理（合约支持 `protect`）。
- 默认策略是最保守的一档：保留 2 份、只按数量保留。要保留更多请显式设置 `kubernetes.etcd_backup_keep_count`。

## 6. 自动化基线

- 单元/集成测试：`pytest` 全绿（1.0.0 基线 1666 项 + 本版本 33 项），`ruff` 全绿，前端 `npm test`（36 项）与 `npm run build` 通过。
- CI：`ci.yml` 全绿（Python 分片 4/4 + 前端 + Compose 冒烟 + kind/Metal3 CRD 集成）。
- 本版本新增测试覆盖：请求校验与目录边界、按数量/按年龄/最小年龄/外来文件四类保留、save→verify→publish→prune 顺序、save 失败不进 retention、verify 失败删 `.partial` 且不 prune、无 etcd Pod、status 只读、调度器每小时一条/幂等/跳过原因、compose 服务守卫、平台只读视图的 fresh/stale/非法策略/陌生人不可见。

## 7. 仍需物理机的项（本版本**未**声称已验证）

1. 真实 `etcdctl snapshot save` 与 `snapshot status` 在三控制面集群上的耗时与卷占用。
2. 从本版本产出的 `.db` 真正恢复一个被驱逐 member（回滚链路的物理验收）。
3. 调度器与集群升级并发时的行为（升级期间的快照一致性）。
4. 备份目录所在磁盘写满时的失败路径与告警送达。
5. 其余 1.0.0 §6 的 GA 门禁项保持不变：真实 Redfish/固件升级、真实 PXE 引导、物理 bond + VLAN70、多控制面免密 sudo、Ceph/SR-IOV、`release-manifest.json`。

## 8. 发布产物

- 源代码：本 tag 的 GitHub 归档（`tar.gz` / `zip`）。
- 离线镜像清单：`deploy/offline/images.json`（本版本未新增镜像）。
- 文档：`docs/agent-command-api.md`（新增备份一节 + 只读视图）、`CHANGELOG.md`、本文件与英文版。
- 许可证：Apache-2.0。
