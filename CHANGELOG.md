# Changelog

本项目版本记录遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

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
