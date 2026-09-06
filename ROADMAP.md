# 工业化路线图（ROADMAP）

> 采购多Agent项目（procurement-agents）从「工程底座」走向「可运营平台」的阶段规划。
>
> 状态图例：✅ 已完成　🟡 进行中　⬜ 未开始
>
> 版本语义：`v0.1.0` 工程底座 → `v0.2.0` 运行时生产化(P1) → `v0.3.0` 组织级基建(P2) → `v1.0.0` 平台化(P3)
>
> 相关文档：变更记录见 [CHANGELOG.md](./CHANGELOG.md)；能力与架构说明见 [README.md](./README.md)（尤其「八、扩展指南」）。

---

## 🟢 P0 工程底座 —— 完成 ✅

**目标**：从「个人文件夹」变成「可追溯、可协作、机器强制验证的仓库」。

| 项 | 内容 | 状态 |
|---|---|---|
| 版本控制 | git 仓库、5 个干净 commit、tag `v0.1.0`；`.gitignore` 排除 `.env`/运行产物/上传目录 | ✅ |
| CI 门禁 | `.github/workflows/ci.yml`：`ruff check` + `mypy src` + `coverage run -m pytest`（≥80%）三 job | ✅ |
| 静态检查 | ruff 32 项、mypy 38+1 项全部清零（过程中发现并修复 2 个真实缺陷：未导入的 `BaseAgent`/`List`） | ✅ |
| 测试体系 | 用例 27 → 42，覆盖率 88%；本地用与 CI 同版本工具（ruff 0.16.6 / mypy 2.3.1）实测全绿 | ✅ |
| 工程文档 | `CHANGELOG.md`、`LICENSE`(MIT)、README CI 徽章、`pyproject.toml` 工具配置段 | ✅ |

**简历价值**：CI 徽章 = 「机器每天替你证明质量」；可讲出「ruff 抓到 `Optional` 未导入」级别的工程细节。

---

## ✅ P1 运行时生产化 —— 完成（tag `v0.2.0`）

**目标**：能回答「怎么上线、怎么暂停恢复、金额对不对」——补齐当前最大的叙事空洞。

| # | 任务 | 具体做法 | 状态 |
|---|---|---|---|
| P1.1 | 修复领域硬伤 | ① `knowledge/supplier_lib.py` 硬编码 `2025 - founded_year` → `datetime.now().year`；② 违约金费率收敛为单一常量（`domain/money.py` `PENALTY_DAILY_RATE=0.0005`，消除 0.005 vs 0.0005 十倍不一致）；③ 金额 float → 全链路 Decimal（领域模型 + 计价 + 模板渲染 + 报告，状态内精度无损） | ✅ |
| P1.2 | 服务化 | `web_api.py`（FastAPI）+ `service.py`：`POST /procurements`、`GET /procurements/{case_id}`、`POST /procurements/{case_id}/approve`；「自动放行留痕」改为真挂起等待（`needs_input` + `pending_approval`） | ✅ |
| P1.3 | 持久化断点 | checkpointer 支持 `sqlite`（SqliteSaver + `sqlite_path`），进程重启可恢复 case；`needs_input`/`resume` 语义落地（含跨进程恢复回归测试） | ✅ |
| P1.4 | 并发/隔离验证 | 多 case 并发回归测试；废弃进程级 `HUMAN_DECIDERS` 注册表，审批决策落 DB（`store.py`） | ✅ |
| P1.5 | 版本收口 | 依赖更新（fastapi/uvicorn/httpx + 离线 PINS 补 langgraph-checkpoint-sqlite）、更新 `CHANGELOG`、CI 保持全绿（60 用例 / 88%） | ✅ |

**简历价值**：从「单机原型」升级为「可部署服务 + 状态可恢复」。

---

## ✅ P2 组织级基建 —— 完成（tag `v0.3.0`）

**目标**：审批真实可信、数据资产可治理——达到企业内控/审计口径。

| 任务 | 内容 | 状态 |
|---|---|---|
| 真 HITL | 审批记录独立落库（`approval_id` + 审批人身份/时间/意见/附件含 sha256/决策来源）；决策走服务端接口，append-only 审计不可篡改；旧库自动迁移 | ✅ |
| 权限与审计 | RBAC（`access.py` 角色×权限矩阵 + `config/access.yaml` 用户目录；谁可触发采购/审批/改主数据）；`audit_logs` 操作审计 who/what/when，拒绝同样留痕；Web 强制 `X-Actor-Id`（401/403）+ `GET /audit` | ✅ |
| 供应商主数据升级 | 价目 → 结构化行表 `supplier_items.csv`（supplier_item × price）；`validate_supplier_items` schema 校验；`MasterDataService` 导入/变更审批（备份回滚 + sha256 + 全程审计） | ✅ |
| 规则版本化 | `rule_registry.py`：合规 C1~C6/策略 R1~R6/仲裁 ARB-* 规则登记版本；产物（合规检查行/仲裁记录/收官报告）烙 `ruleset_version`，变更登记 `RULESET_CHANGES` 可追溯回滚 | ✅ |
| LLM 契约测试 | `llm/recorder.py` 录制/回放 + `tools/record_provider.py` 真实 Provider 录制夹具 + 离线契约测试（结构契约/语义锚点/归一化稳定性）；修复千分位金额与 normalize 缺陷 | ✅ |
| 批量入口 | `service.submit_many/list_plan`（计划性/批量采购，plan_id 落库）+ `POST /procurements/batch`；`runner.save_report` case 目录化存储 `<out>/<case_id>/` | ✅ |

**完成标志**：107 用例全绿 / coverage 88% / ruff 0.16.6 + mypy 2.3.1 零错误。
**简历价值**：从「演示系统」迈向「内控系统」，可写「对接审批流 / RBAC / 主数据治理」。

---

## 🔴 P3 平台化与可观测性 —— 目标 `v1.0.0`

**目标**：能运营、能量化、能扩容。

| 任务 | 内容 |
|---|---|
| 可观测性 | OpenTelemetry spans + 每节点耗时/成功率/质量分指标；**LLM 成本台账**（每次调用 token/费用——采购审计刚需） |
| 异步任务 | 任务队列（异步 + 幂等 + 可重试），长流程不阻塞 API |
| 多租户/隔离 | case 级隔离、并发压测（千行价目 × 多品类 × 并行 case）出性能基线 |
| 系统集成 | `load_suppliers` 换真实 SRM/ERP 查询实现（接口已抽象，见 README 扩展指南）；PO/合同推送接口；金额走财务系统 Decimal |
| 运维治理 | 告警、看板、数据备份、密钥管理（Vault）、灰度（mock ↔ 真实模型比例切换） |

**工作量数周~数月。简历价值**：达到可谈「架构设计 + 生产运营」的程度。

---

## ⚪ P4+ 演进方向（可选加分项）

- **框架化沉淀**：把「6+1 采购域」抽象为通用「业务 Agent + 监理 Agent」编排框架，供审批/报销/招投标等流程低代码接入；
- **评估集（evals）**：golden case 集 + LLM-as-judge，保证需求解析/归一化提示词变更不回归；
- **模型路由**：按任务难易/成本路由 mock/小模型/大模型并自动降级；
- **安全加固**：提示注入防护、供应商数据脱敏分级。

---

## 建议执行顺序

按「性价比 + 消除面试把柄」排序：

```
已完成 → P1（v0.2.0：领域硬伤 / FastAPI 服务化 / SqliteSaver / 并发隔离 / 收口）
已完成 → P2（v0.3.0：真 HITL 审计 / RBAC+操作审计 / 主数据行表化+变更审批 /
              规则版本化 / LLM 契约测试 / 批量入口+case 目录化）
现在   → P3（可观测性 + 系统集成；也可先做 P4 加分项：evals / 模型路由）
```

## 每阶段固定动作

1. 改代码；
2. 本地验证：`ruff check .` + `mypy src` + `coverage run -m pytest`（工具已就位于本地环境）；
3. push 并确认 GitHub Actions 三 job 全绿；
4. 更新 `CHANGELOG.md` 并打 tag；
5. 简历对应补充一行可验证的成果。

---

*本路线图随项目演进持续维护。*
