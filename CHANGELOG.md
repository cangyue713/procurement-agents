# Changelog

本项目版本记录遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.3.0] - 2026-09-06

### Added

- **P2-A 真 HITL（审批独立审计单元）**：
  - `ApprovalRecord` 升级：`approval_id` 唯一审计主键 + 审批人身份 `approver_id` +
    附件元数据 `attachments`（filename/content_type/size/**sha256**/note）+ 决策来源
    `source`（api/auto/resume）；
  - 审批决策沿 API → service → runner → 审批节点全链路透传身份与附件；
  - approvals 审计行 **append-only 幂等**（按 approval_id 去重，不再整组覆盖）；
  - 旧库（v0.2.0 approvals 无附件列）打开时自动 ALTER 升级并回填 `legacy-*` 主键。
- **P2-B RBAC + 操作审计**：
  - `access.py`：角色枚举（buyer/approver/compliance/admin/auditor）+ 权限矩阵
    （procurement.submit/approve/view、masterdata.change、rules.change、audit.view）；
  - `config/access.yaml` 用户目录；`AuthorizationError`；
  - `audit_logs` 审计表：who/what/when/resource/result，**拒绝(denied)同样留痕**；
  - Web 层强制身份头 `X-Actor-Id`（缺失 401 / 未收录或无权限 403），
    审批人/发起人身份取自已认证主体不可伪造；新增 `GET /audit`。
- **P2-C 供应商主数据升级（结构化行表）**：
  - 价目从 `suppliers.csv` 内联文本迁出 → 结构化行表 `knowledge/data/supplier_items.csv`
    （supplier_item × price，25 行）；`load_suppliers` 聚合生成兼容文本，下游零改动；
  - `validate_supplier_items` schema 校验（列/单价正数/供应商引用/物品不重复）；
  - `masterdata.py` 变更审批工具：propose → pending 变更单（old/new sha256 + 快照）→
    decide（批准：`.bak-<ts>` 备份 + 落盘生效 + 清缓存；拒绝不改文件），全程审计；
  - `store.masterdata_changes` 变更单表。
- **P2-D 规则版本化**：
  - `knowledge/rule_registry.py`：C1~C6 / R1~R6 / ARB-* 规则登记版本元数据 +
    `ruleset_snapshot`（版本+内容 hash）+ `RULESET_CHANGES` 变更登记；
  - 合规每条检查行带 `rule_version`，合规产物/仲裁记录/收官报告烙 `ruleset_version`。
- **P2-E LLM 契约测试**：
  - `llm/recorder.py`：RecordingProvider / ReplayProvider / 夹具（request+response+meta）；
  - `llm/contract_samples.py`：8 条契约样本 + 语义锚点 + 结构契约常量；
  - `tools/record_provider.py`：真实 Provider（DeepSeek/OpenAI 兼容）录制契约夹具；
  - 离线夹具 `tests/fixtures/llm/parse_requirement.json`（CI 无需 API Key）；
  - 契约测试发现并修复两个真实缺陷：mock 千分位金额解析（`300,000` → 300）、
    `openai_compat._normalize` 对畸形 items/缺失键/`None` 描述的清洗与契约补全。
- **P2-F 批量入口 + case 目录化**：
  - `ProcurementService.submit_many` / `list_plan`：计划性/批量采购（plan_id 落库，
    单条失败不阻断整批）；`POST /procurements/batch` + `GET /procurements/plans/{plan_id}`；
  - `runner.save_report` 产物写入 `<output_dir>/<case_id>/` 目录（case 目录化存储）。

### Changed

- Web 业务端点要求身份头 `X-Actor-Id`（RBAC 强制；`GET /health` 公开）。
- approvals 落库语义：整组重建 → append-only 幂等同步（审计不可篡改）。
- `ApproveRequest` 不再接受伪造审批人：审批人/发起人身份由请求主体派生。
- 主数据：价目唯一来源 = `supplier_items.csv`（suppliers.csv 移除 `price_items` 列）。
- 规则产物携带版本号（compliance/arbitration/final_report）。
- 测试 92 → 107 用例，覆盖率 88%；工具版本 ruff 0.16.6 / mypy 2.3.1（与 CI 一致）。

[0.3.0]: https://github.com/cangyue713/procurement-agents/releases/tag/v0.3.0

## [0.2.0] - 2026-09-04

### Added

- **P1.2 服务化**：`web_api.py`（FastAPI）三接口
  `POST /procurements`、`GET /procurements/{case_id}`、`POST /procurements/{case_id}/approve`；
  `service.py` 编排服务（submit/view/resume/list_cases）；`store.py` case 与审批决策 sqlite 存储。
- **P1.3 持久化断点**：checkpointer 支持 `sqlite`（SqliteSaver，`workflow.sqlite_path` 可配），
  人审挂起点经 `interrupt/resume` 真正落地 `needs_input` 语义，进程重启后可查询并续跑。
- **P1.4 决策走 DB 与并发隔离**：废弃进程级 `HUMAN_DECIDERS` 注册表，
  审批决策落库（CaseStore approvals）+ 从断点续跑；多 case 并发回归测试。
- 金额工具模块 `domain/money.py`：`to_decimal` / `fmt_money` / 违约金费率单一常量
  `PENALTY_DAILY_RATE`（0.05%/日）。

### Changed

- **P1.1 领域硬伤修复**：
  - 供应商成熟度评分不再硬编码年份，改用 `datetime.now().year` 计算成立年限；
  - 违约金费率收敛为单一常量 `PENALTY_DAILY_RATE`（此前模型默认 0.005 与合同实际写入
    0.0005 不一致，相差 10 倍）；合同模板费率文本改为常量驱动占位；
  - 金额 float → 全链路 `Decimal`：领域模型字段、价目解析/计价（supplier_lib）、
    比价/合规/合同/仲裁金额比较、模板渲染与报告展示均改为 Decimal（JSON 状态中为
    精度无损字符串，读回用 `to_decimal`，展示用 `fmt_money`）。
- `runner.py`：`run()` 支持 `auto_approve=False` 挂起语义；新增 `resume()` / `get_state()`；
  `ProcurementRun` 增加 `needs_input` / `pending_approval` 视图。
- `graph.py`：审批节点改为 LangGraph `interrupt()` 真挂起（替代进程级决策器回调）。
- 依赖新增 `fastapi` / `uvicorn`（服务化）与 `httpx`（测试）；
  离线引导 `fetch_wheels.py` 增补对应 PINS（含 langgraph-checkpoint-sqlite / aiosqlite）。
- 测试 42 → 60 用例（新增 money / service / web 回归组），覆盖率维持 ≥ 88%。

### Fixed

- **CI 安装缺 SqliteSaver 发行包**：`langgraph.checkpoint.sqlite` 属于独立发行包
  `langgraph-checkpoint-sqlite`，此前仅存在于离线 `.pylibs`，CI 的 `pip install -e ".[dev]"`
  未安装导致 service/web 共 12 个用例 `ModuleNotFoundError`。
  已将 langgraph 0.6 世代依赖（langgraph/langchain-core/langgraph-checkpoint/
  langgraph-checkpoint-sqlite/langgraph-sdk/langgraph-prebuilt/langsmith）在
  `pyproject.toml` 与 `requirements.txt` 中**精确钉扎**，与 `tools/fetch_wheels.py`
  的 PINS 一致，保证 CI 与本地行为可复现。

[0.2.0]: https://github.com/cangyue713/procurement-agents/releases/tag/v0.2.0

## [0.1.0] - 2026-09-04

### Added

- 初始版本：供应链/采购流程自动化多 Agent 项目。
  - **6 业务 Agent**：需求理解 → 采购策略判定 → 供应商推荐(库内价目) → 比价分析 → 合规审查 → PO/合同草稿；
  - **仲裁 Agent**：阶段质检 + 跨阶段一致性复核，输出 proceed / hold / block 三态裁决并全程留痕；
  - **LangGraph 编排**：条件路由 + MemorySaver checkpointer（支撑人审暂停/恢复语义）+ 节点级超时/重试护栏；
  - **可插拔 LLM Provider**：mock（离线确定性规则引擎）/ deepseek / openai_compat 零改动切换，价格与合规走可审计规则与主数据；
  - **领域分层**：domain / knowledge / llm / agents / pipeline / runner；
  - **审计产物**：Markdown 运行报告 + 全量 JSON trace（阶段记录/仲裁/审批/升级问题）；
  - **工程基建**：git 仓库、GitHub Actions CI（ruff + mypy + pytest + coverage ≥80%）、CHANGELOG。
  - 测试：27 个用例（规则引擎 / Agent 单元 / 仲裁规则 / 节点护栏 / 端到端）全部通过。

[0.1.0]: https://github.com/cangyue713/procurement-agents/releases/tag/v0.1.0
