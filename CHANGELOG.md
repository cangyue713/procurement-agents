# Changelog

本项目版本记录遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

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
