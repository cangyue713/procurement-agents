# 供应链/采购流程自动化 · 多 Agent 项目

> 从**分工**到**工业化**：用 7 个各司其职的 Agent + LangGraph 状态机，把「采购需求 → PO/合同草稿」整条链路自动化。
> 价格数据与供应商介绍**放在同一张表**（`suppliers.csv` 库内价目），全流程**无询价/报价环节**，
> 由**仲裁 Agent** 全程监控、留痕、把关。

```
   ┌────────────┐   ┌────────────┐   ┌─────────────────────┐
   │ 需求理解    │──▶│ 采购策略判定 │──▶│ 供应商推荐(库内价目) │
   └────────────┘   └────────────┘   └─────────────────────┘
        ▲                ▲                     ▲
        └────────────────┴──────┬──────────────┘
                         ┌──────────────┐  全程监控/裁决
                         │  仲裁 Agent   │ ◀────────────────────────────┐
                         └──────────────┘                              │
   ┌────────────┐   ┌────────────┐   ┌────────────┐                    │
   │ 合同草稿生成 │◀──│ 合规审查    │◀──│ 比价分析    │◀──┘ (仲裁插桩在每阶段之后) │
   └────────────┘   └────────────┘   └────────────┘
        PO + 合同草稿（金额/条款来自库内价目与成交候选）
```

## 一、为什么这样做（分工的价值）

采购天然是多角色协同：需求方提要求、采购定方式、寻源找供应商、按库内价目比价、
法务/内控审合规、合同岗起草文件、领导把关。本项目用 **7 个 Agent（6 业务 + 1 仲裁）**
还原这套组织分工。与"询价-报价"模式不同，本流程面向**目录式/现货式采购**：
供应商库（`suppliers.csv`）每行既包含供应商介绍（资质/绩效/风险/联系方式），
也包含**库内价目**（对标准物品的单价与交期/质保/付款条款）——价格数据与供应商介绍同源，
需求到达后系统直接按「需求行 × 库内价目」计价比选，不再向供应商临时询价。

| # | Agent | 输入 | 输出（结构化单据） | 关键逻辑 |
|---|-------|------|------------------|---------|
| 1 | 需求理解 | 采购需求文本（txt） | `RequirementArtifact` 需求单 | 提取品类/数量/预算/交期/质量要求，识别缺失字段 |
| 2 | 采购策略判定 | 需求单 | `StrategyArtifact` 策略单 | 预算/紧急度/独家特征 → 直采/询比价/邀请招标/单一来源 |
| 3 | 供应商推荐 | 需求单 | `SupplierShortlistArtifact` 短名单 | 品类匹配 + 绩效/资质/成熟度评分；候选自带库内价目 |
| 4 | 比价分析 | 需求+候选价目 | `ComparisonArtifact` 比价表 | 需求行×价目计价 → 价格 60%+交期 20%+绩效 20% 排名 |
| 5 | 合规审查 | 需求+候选(库内) | `ComplianceArtifact` 合规意见 | 黑名单/风险/交期/预算/价目覆盖/资质 规则矩阵(C1~C6) |
| 6 | PO/合同草稿 | 比价+合规+候选 | `ContractArtifact` | 合规放行 ∩ 比价名次定标，模板渲染 PO 与合同 |
| **仲裁** | **全程监控** | 每阶段产物+上下文 | `ArbitrationRecord[]` | 阶段质检 + **跨阶段一致性复核** + proceed/hold/block 裁决 |

## 二、仲裁 Agent：监控全程意味着什么

仲裁不参与业务计算，只做**监理**，挂在每个业务节点之后（LangGraph 条件路由），典型监控点：

- **阶段质检**：需求明细为空、候选价目缺失、预算超支 → 阻断（block）；
- **跨阶段一致性**（真正的"监控"）：
  - 候选**库内无价目 / 价目未覆盖需求行** → 提示将无法比价与成交；
  - 比价推荐对象是**高风险供应商** → 定标预警（hold）；
  - 比价推荐被合规否决 → 合同阶段发生**定标切换** → 人工知悉（hold）；
  - 候选库内**交期远超需求** → 预判"将在合规阶段被否决"（联动预警）；
  - 未选总价最低者 → 要求理由充分（涉及合规/交期/绩效）；
  - 历史出现过阻断 → 收官阶段禁止标记成功。
- **裁决模型**：`proceed 放行 / hold 挂起(人审点) / block 阻断`，全程写入 `arbitration[]`；
  每次 hold 进入**人工审批节点**：可配置自动放行（留痕 `auto_approved`）、接入人工决策回调、或
  人工拒绝后阻断流程。

演示剧本里，候选蓝海云联虽然总价最低，但其库内交期 45 天远超需求 25 天——
合规审查按 C3 规则将其否决，合同自动落定合规放行中综合名次最高的华云数据
（金额 2,126,400 元 < 预算 280 万），仲裁在各阶段为这一结论留下完整证据链。

## 三、数据在哪里（"改哪张表"就是业务变化）

| 数据 | 位置 | 说明 |
|------|------|------|
| **采购需求文本** | `examples/input/request.txt` | 由项目读取（`load_request_text` / `runner.run(request_file=...)`，UTF-8） |
| **供应商介绍 + 价格（同一张表）** | `src/procurement_agents/knowledge/data/suppliers.csv` | 每行一家：介绍列 + `price_items`(价目，`物品=单价` 以 `|` 分隔) + `delivery_days/warranty_months/payment_terms` |
| **禁入名单** | `knowledge/data/blacklist.json` | 合规 C1 规则直接查它 |
| **规则阈值 / 流程参数** | `config/app.yaml` | 直采上限、招标门槛、短名单数量、重试、人审策略等 |
| **LLM 密钥** | 项目根 `.env` | `DEEPSEEK_API_KEY`、`OPENAI_COMPAT_BASE_URL/MODEL` |
| **PO/合同版式** | `templates/po.md.tpl`、`contract.md.tpl` | `$` 占位符由合同 Agent 填充 |
| **运行产物** | `outputs/` | `report_*.md`（可读）+ `trace_*.json`（全量追踪） |

## 四、工业化要素（不只是 Demo）

| 维度 | 落地方式 |
|------|---------|
| **统一任务上下文** | LangGraph `WorkflowState` 全程 JSON 安全，任意节点可恢复 |
| **Agent 协议** | `BaseAgent.invoke(inputs: dict) -> pydantic Artifact`，Agent 与编排解耦 |
| **结构化领域模型** | pydantic v2 单据：需求/策略/短名单(含价目)/比价/合规/合同/裁决/审批 |
| **可插拔模型** | `LLMProvider` 协议；`mock`（中文规则引擎，离线确定）默认，`deepseek`/`openai_compat` 一键切换，Agent 代码零改动 |
| **重试/超时** | 节点护栏：子线程超时 + 失败重试（有界），耗尽后升级问题并置 `failed` |
| **人审点(HITL)** | hold → 审批节点；MemorySaver checkpointer 支撑暂停/恢复语义 |
| **追踪审计** | 每个节点运行记录 + 全程仲裁记录 + 审批记录 + 问题升级 → `outputs/report_*.md` 与 `trace_*.json` |
| **规则可配置** | `config/app.yaml`：直采/招标阈值、重试次数、审批策略等 |
| **数据资产化** | 供应商主数据为 CSV（Excel 可直接维护，价格与介绍同源），黑名单 JSON，评分透明可追溯 |
| **测试体系** | 单测（模型引擎/各 Agent/仲裁规则/护栏）+ 端到端（LangGraph 全链路） |

## 五、快速开始

### 0. 环境
- Python ≥ 3.10（已在 3.12 验证），建议 venv。

### 1. 安装依赖
**常规方式**（可直接写 site-packages 时）：
```bash
pip install -r requirements.txt
```
**受限/离线环境兜底**（pip 无法写入 site-packages 或网络异常时，本项目自带引导）：
```bash
python tools/fetch_wheels.py langgraph   # 通过 PyPI JSON API 抓取依赖 wheel 到 .wheels
python tools/stage_wheels.py             # 解压到工作区内 .pylibs
# 之后运行需设置 PYTHONPATH（PowerShell）：
$env:PYTHONPATH = "$PWD\.pylibs;$PWD\src"
python -X utf8 examples/run_demo.py
```

### 2. 跑端到端演示（默认 Mock 模型，无需任何 API Key）
```bash
python -X utf8 examples/run_demo.py
```
控制台输出：策略判定、候选（库内价目）、比价名次、合规否决、成交对象、**仲裁全程裁决**；
并在 `outputs/` 下生成 `report_*.md`（可读报告）与 `trace_*.json`（全量追踪）。

### 3. 运行测试
```bash
python -X utf8 -m pytest
```

### 4. 切换真实大模型（DeepSeek 或任意 OpenAI 兼容服务）
```bash
cp .env.example .env        # 填入 DEEPSEEK_API_KEY（DeepSeek 开放平台申请）
python -X utf8 examples/run_demo.py --provider deepseek
```
引擎对照：
```bash
python -X utf8 examples/compare_providers.py
```

### 5. 在代码中使用
```python
from procurement_agents.runner import ProcurementRunner

runner = ProcurementRunner()                     # 读 config/app.yaml，默认 mock
result = runner.run(
    request_file="examples/input/request.txt",   # 需求从 txt 读取（也可直接传 request_text）
    case_id="PC-2025xxxx-001",
)
print(result.summary_text)                       # 采购结论（策略/比价/合规/成交）
print(result.markdown_report())                  # 完整报告（含仲裁裁决与合同草稿）
```
换业务只需改 `suppliers.csv`（增删供应商及其价目）与需求 `txt`，Agent 代码零改动。

## 六、目录结构

```
agent/
├── config/app.yaml            # 业务/工作流/模型配置
├── tools/                     # 环境引导：fetch_wheels(离线抓包) / stage_wheels(就地解压)
├── src/procurement_agents/
│   ├── config.py              # 配置装载(.env + yaml)
│   ├── domain/                # 领域层：枚举 + pydantic 单据模型
│   ├── llm/                   # 模型层：Provider 协议 + Mock 规则引擎 + OpenAI 兼容实现
│   ├── knowledge/             # 知识层：suppliers.csv(介绍+价目) / 黑名单 / 策略与价目工具
│   ├── agents/                # Agent 层：6 业务 Agent + 仲裁 Agent
│   ├── pipeline/              # 编排层：State / PhaseNode 护栏 / LangGraph 图 / 追踪
│   └── runner.py              # 高层 API
├── templates/                 # PO 与合同草稿模板
├── examples/
│   ├── input/request.txt      # 演示采购需求（由项目读取）
│   ├── demo_case.py / run_demo.py / compare_providers.py
├── tests/                     # 单测 + 端到端测试
└── outputs/                   # 运行产物（报告/追踪/合同草稿）
```

## 七、配置速查（config/app.yaml）

| 键 | 默认 | 说明 |
|----|------|------|
| `llm.provider` | `mock` | `mock` / `deepseek` / `openai_compat` |
| `llm.api_key_env` | `DEEPSEEK_API_KEY` | 密钥来源环境变量 |
| `workflow.max_agent_retries` | `2` | 节点失败重试上限 |
| `workflow.auto_approve_holds` | `true` | hold 自动放行留痕；`false` 走人审 |
| `workflow.checkpointer` | `memory` | 内存断点（人审恢复语义） |
| `workflow.shortlist_size` | `3` | 供应商短名单候选数 |
| `rules.direct_purchase_max` | `50000` | ≤ 此金额直接采购 |
| `rules.tender_threshold` | `1000000` | 超此金额进招标区间 |
| `rules.invite_bid_min` | `3` | 邀请招标最少邀请家数 |

## 八、扩展指南（走向更大规模）

- **维护供应商与价格**：直接编辑 `suppliers.csv`。多值字段（`aliases/categories/certifications/risk_flags`）用 `|`
  分隔；价目列 `price_items` 写 `物品描述=单价|物品描述=单价`；`delivery_days` 为该供应商履约交期。
  新供应商加入即自动参与推荐/比价/合规/签约（Agent 代码零改动）。
- **价目匹配规则**：`knowledge/supplier_lib.py` 的 `match_price` 支持 全等/包含/公共汉字子串 三级匹配，
  兼容"48口万兆交换机"与"工业级交换机"这类描述差异；匹配不到的行会被比价排除、合规与仲裁给出提示。
- **接真实供应商主数据**：把 `supplier_lib.load_suppliers` 换成查询 SRM/ERP 接口即可（保持返回结构一致）；
  或把 CSV 换成语料/目录服务后扩展 `load_suppliers` 一个实现。
- **需求文本来源**：Runner 支持 `request_text`（字符串）与 `request_file`（txt 路径）两种入口，兼容 API/文件输入。
- **更细的合规引擎**：在 `agents/compliance.py` 的 C 编号规则中追加（C7、C8…），仲裁与合同自动消费其结论。
- **持久化与服务化**：checkpointer 换 `SqliteSaver`/`PostgresSaver`；Runner 包一层 REST；
  hold 挂起对接审批工作流（返回 `needs_input`，审批后从断点续跑）。

## 九、已知边界（诚实说明）

- **Mock 引擎是确定性启发式**：面向一句一物的中文需求文本；更口语化/复杂写法请切换真实模型——
  代码路径与协议完全一致（价格数据不经过 LLM，只做需求解析）。
- **价目匹配是规则匹配**：`match_price` 面向结构化价目；跨品类/别名严重不一致时需人工维护价目描述，
  仲裁会以"价目覆盖"提示兜底。
- **金额使用 float**：演示/原型足够；生产建议迁移 Decimal 并接入财务系统。
- **目录式采购定位**：本流程面向库内有价目的现货/目录采购；招投标法定程序、单件定制询价等场景
  建议沿用带询价环节的流程变体。
- 所有供应商、企业、人名均为**虚构演示数据**，不代表任何真实主体。
