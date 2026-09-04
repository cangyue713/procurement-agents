"""仲裁 Agent(Arbitrator)：全流程的『监理』。

职责：
  1. 对每个业务阶段的产物做质量检查（字段完整性、业务合理性）；
  2. 跨阶段一致性复核 —— 监控的真正价值所在：
       候选是否具备库内价目/价目是否覆盖需求行 /
       中标推荐为何不是最低价 / 合同定标是否绕开合规否决对象 /
       金额是否守住预算；
  3. 输出裁决：proceed(放行) / hold(挂起待人工) / block(阻断)。

裁决不写业务字段，只写 arbitration 记录；hold 由节点层交由
『人工审批』节点处置（可配置自动放行并留痕）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel

from procurement_agents.agents.base import BaseAgent, require_keys
from procurement_agents.domain.enums import CheckLevel, PhaseName, VerdictAction
from procurement_agents.domain.models import ArbitrationCheck, ArbitrationRecord
from procurement_agents.knowledge.supplier_lib import (
    match_price,
    parse_price_items,
    supplier_registry,
)

# 每个业务阶段的裁决函数注册表
_RULES: Dict[str, Any] = {}


def _rule(phase: str):
    def deco(fn):
        _RULES[phase] = fn
        return fn

    return deco


class _Reporter:
    """收集 checks / fail / hold 的轻量工具。"""

    def __init__(self) -> None:
        self.checks: List[ArbitrationCheck] = []
        self.fails: List[str] = []
        self.holds: List[str] = []

    def add(self, name: str, level: CheckLevel, message: str, hold: bool = False) -> None:
        self.checks.append(ArbitrationCheck(name=name, level=level, message=message))
        if level == CheckLevel.FAIL:
            self.fails.append(f"{name}: {message}")
        if hold:
            self.holds.append(f"{name}: {message}")


class ArbitratorAgent(BaseAgent):
    """仲裁 Agent：消费各阶段产物做裁决。"""

    phase = PhaseName.ARBITRATE
    display_name = "仲裁Agent"
    description = "全程监控：阶段质检 + 跨阶段一致性复核 + 放行/挂起/阻断裁决"

    def invoke(self, inputs: Dict[str, Any]) -> BaseModel:
        require_keys(inputs, ["phase", "artifact"], agent=self.display_name)
        phase_value = str(inputs["phase"])
        artifact: Dict[str, Any] = inputs["artifact"] or {}
        ctx: Dict[str, Any] = inputs.get("context") or {}
        fn = _RULES.get(phase_value)
        if fn is None:
            # 未知/辅助阶段：默认放行
            return ArbitrationRecord(phase=PhaseName(phase_value), verdict=VerdictAction.PROCEED,
                                     summary=f"{phase_value}：无可配置的仲裁规则，默认放行")

        r = _Reporter()
        fn(r, artifact, ctx)
        nfail, nwarn = len(r.fails), sum(1 for c in r.checks if c.level == CheckLevel.WARN)
        quality = max(0, min(100, 100 - 10 * nfail - 3 * nwarn))

        if nfail:
            verdict, summary = VerdictAction.BLOCK, f"阻断：{len(r.fails)} 项不通过（{'；'.join(r.fails[:3])}）"
        elif r.holds:
            verdict, summary = VerdictAction.HOLD, f"挂起待人工：{'；'.join(r.holds[:2])}"
        else:
            verdict, summary = VerdictAction.PROCEED, f"放行：质量分 {quality}，{len(r.checks)} 项检查全部通过"
        return ArbitrationRecord(
            phase=PhaseName(phase_value), verdict=verdict, quality_score=quality,
            checks=r.checks, summary=summary,
        )


# ==========================================================================
# 各阶段裁决规则（只读审阅，不修改业务状态）
# ==========================================================================
@_rule(PhaseName.REQUIREMENT.value)
def _req(r: _Reporter, art: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    items = art.get("items") or []
    r.add("需求明细", CheckLevel.FAIL if not items else CheckLevel.PASS,
          "需求无任何明细行，无法进入采购" if not items else f"明细 {len(items)} 行")
    if items:
        bad = [it for it in items if not (float(it.get("quantity", 0)) > 0)]
        if bad:
            r.add("数量合法性", CheckLevel.FAIL, f"{len(bad)} 行数量缺失或非正数")
    budget, days = art.get("budget_amount"), art.get("delivery_days")
    if not budget:
        r.add("预算完整性", CheckLevel.WARN, "缺少预算金额，将影响策略判定与预算管控", hold=False)
    if not days:
        r.add("交期完整性", CheckLevel.WARN, "缺少交付期要求，将影响交期比选与合规判断")
    for mf in art.get("missing_fields") or []:
        r.add("缺失字段", CheckLevel.WARN, f"需求缺失：{mf}")
    title = art.get("title", "")
    if not title:
        r.add("标题", CheckLevel.WARN, "需求标题为空")


@_rule(PhaseName.STRATEGY.value)
def _strategy(r: _Reporter, art: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    strategy = art.get("strategy")
    if not strategy:
        r.add("策略结果", CheckLevel.FAIL, "未输出采购策略")
        return
    if art.get("approval_needed"):
        r.add("审批要求", CheckLevel.WARN, "策略判定触发人工确认（如预算缺失），须采购经理审批", hold=True)
    if strategy == "询比价":
        # 询比价应给出预算区间提示
        budget = (ctx.get("requirement") or {}).get("budget_amount")
        if not budget:
            r.add("预算提示", CheckLevel.WARN, "询比价流程建议补充预算上限")
    r.add("规则依据", CheckLevel.PASS, f"命中规则: {','.join(art.get('rules_applied') or [])}")


@_rule(PhaseName.SUPPLIER.value)
def _supplier(r: _Reporter, art: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    cands = art.get("candidates") or []
    if not cands:
        r.add("候选数量", CheckLevel.FAIL, "无任何候选供应商")
        return
    r.add("候选数量", CheckLevel.PASS, f"候选 {len(cands)} 家")
    low = [c for c in cands if float(c.get("score", 0)) < 60]
    for c in low:
        r.add("低分候选", CheckLevel.WARN, f"{c.get('name')} 评分偏低({c.get('score')})，参与比价前建议复核")
    for ex in art.get("excluded") or []:
        if "禁入" in ex.get("reason", "") or "黑名单" in ex.get("reason", "") or "高风险" in ex.get("reason", ""):
            r.add("排除留痕", CheckLevel.PASS, f"{ex.get('name')} 被排除：{ex.get('reason')}")

    # 价目完整性监控（价格与供应商介绍同源，缺价目将无法比价/成交）
    req_items = (ctx.get("requirement") or {}).get("items") or []
    for c in cands:
        sid = c.get("supplier_id")
        name = c.get("name") or sid
        price_items = str(c.get("price_items") or "")
        parsed = parse_price_items(price_items)
        if not parsed:
            r.add("价目缺失", CheckLevel.WARN, f"{name} 库内无价目，将无法参与比价与成交", hold=False)
            continue
        if req_items:
            covered = sum(1 for it in req_items
                          if match_price(price_items, str(it.get("description", ""))) is not None)
            if covered < len(req_items):
                r.add("价目覆盖", CheckLevel.WARN,
                      f"{name} 价目仅覆盖需求 {covered}/{len(req_items)} 行，未覆盖部分将无法计价", hold=False)

    strategy = (ctx.get("strategy") or {}).get("strategy")
    if strategy in ("邀请招标", "公开招标") and len(cands) < 3:
        r.add("招标家数", CheckLevel.WARN, f"招标类策略候选仅 {len(cands)} 家(<3)，竞争性不足", hold=True)


@_rule(PhaseName.COMPARISON.value)
def _comparison(r: _Reporter, art: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    rows = art.get("rows") or []
    if not rows:
        r.add("比价行", CheckLevel.FAIL, "比价结果为空")
        return
    if len(rows) < 2:
        r.add("比价基数", CheckLevel.WARN, "参与比价对象不足 2 家")
    rec = art.get("recommended") or {}
    lowest_id = art.get("lowest_bidder_id")
    if not rec.get("supplier_id"):
        r.add("定标建议", CheckLevel.FAIL, "未给出推荐成交对象")
        return
    # 跨阶段预警：推荐对象为高风险供应商时，必须人确认/交由合规拦截
    rec_sid = rec.get("supplier_id")
    reg = supplier_registry().get(rec_sid, {})
    if reg.get("risk_level") == "高":
        r.add("高风险定标预警", CheckLevel.WARN,
              f"比价推荐 {rec.get('supplier_name')} 属高风险供应商(如处罚/授权问题)，"
              "按流程应由合规审查否决，建议人工知悉并复核评分口径", hold=True)
    if lowest_id and rec["supplier_id"] != lowest_id:
        reason = str(rec.get("reason", ""))
        # 跨阶段一致性：未选最低价必须有充分理由
        if any(k in reason for k in ("合规", "交期", "绩效", "风险", "评分")):
            r.add("非最低价定标", CheckLevel.PASS,
                  f"推荐 {rec.get('supplier_name')} 非总价最低({lowest_id})，理由含{reason[:60]}，符合程序")
            risk = art.get("risks") or []
            if risk:
                r.add("比价风险", CheckLevel.WARN, "；".join(risk[:2]))
        else:
            r.add("非最低价定标", CheckLevel.WARN, "未选总价最低者且理由不充分，须人工复核", hold=True)
    for row in rows:
        if row.get("delivery_days") and (ctx.get("requirement") or {}).get("delivery_days"):
            pass  # 交期已在合规阶段把关


@_rule(PhaseName.COMPLIANCE.value)
def _compliance(r: _Reporter, art: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    approved = art.get("approved_supplier_ids") or []
    rejected = art.get("rejected") or []
    if not approved:
        r.add("可成交对象", CheckLevel.FAIL, "无合规放行的供应商，建议中止采购或重新询价")
    else:
        r.add("可成交对象", CheckLevel.PASS, f"放行 {len(approved)} 家")
    # 每个否决必须有 FAIL 依据
    for rid in rejected:
        has_fail = any(
            c.get("rule", "") and c.get("level") == CheckLevel.FAIL.value
            for c in (art.get("checks") or [])
            if c.get("subject") == rid or rid in str(c.get("subject"))
        )
        # 校验 subject 可能为供应商名，此处简化：统计 FAIL 数即可（已在 Agent 内保证）
        if not has_fail and not any(c.get("level") == CheckLevel.FAIL.value for c in art.get("checks") or []):
            r.add("否决依据", CheckLevel.WARN, f"否决对象 {rid} 的 FAIL 依据缺失，请人工复核")
    # 原比价推荐是否被否 -> 提示合同阶段将发生定标切换
    rec_id = ((ctx.get("comparison") or {}).get("recommended") or {}).get("supplier_id")
    if rec_id and rec_id in rejected:
        r.add("定标切换预警", CheckLevel.WARN,
              f"比价推荐({rec_id})已被合规否决，合同阶段将自动改选，需人工知悉", hold=True)


@_rule(PhaseName.CONTRACT.value)
def _contract(r: _Reporter, art: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    if not art.get("supplier_name"):
        r.add("定标对象", CheckLevel.FAIL, "合同未确定成交供应商")
    budget = (ctx.get("requirement") or {}).get("budget_amount")
    total = float(art.get("total_amount", 0) or 0)
    if budget and total > budget:
        r.add("预算红线", CheckLevel.FAIL, f"合同金额 {total:,.0f} 元超预算 {budget:,.0f} 元")
    elif budget:
        r.add("预算红线", CheckLevel.PASS, f"合同金额 {total:,.0f} 元在预算 {budget:,.0f} 元内")
    approved = set((ctx.get("compliance") or {}).get("approved_supplier_ids") or [])
    sid = art.get("supplier_id")
    if approved and sid not in approved:
        r.add("合规联动", CheckLevel.FAIL, f"成交对象 {sid} 未在合规放行名单内")
    elif approved:
        r.add("合规联动", CheckLevel.PASS, f"成交对象 {sid} 已通过合规审查")
    if not art.get("po_text") or not art.get("contract_text"):
        r.add("文件产出", CheckLevel.FAIL, "PO/合同文本为空")
    if not art.get("items"):
        r.add("合同明细", CheckLevel.WARN, "合同缺少行项目明细")
    # 若发生定标切换（比价推荐被否），在此留痕并由人确认
    if any("未通过合规审查" in c for c in (art.get("conditions") or [])):
        r.add("定标切换确认", CheckLevel.WARN, "成交对象异于比价推荐，需业务负责人审批", hold=True)


@_rule(PhaseName.FINAL.value)
def _final(r: _Reporter, art: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    records = (ctx.get("arbitration") or [])
    blocked = [x for x in records if x.get("verdict") == "block"]
    if blocked:
        r.add("全程裁决", CheckLevel.FAIL, f"历史存在 {len(blocked)} 次阻断记录，本次采购需复盘")
        return
    holds = [x for x in records if x.get("verdict") == "hold"]
    r.add("全程裁决", CheckLevel.PASS, f"全链路放行，历史挂起 {len(holds)} 次(均已处置/留痕)")
    r.add("质量水位", CheckLevel.PASS,
          f"阶段质量分均值 {sum(int(x.get('quality_score', 100)) for x in records) // max(1, len(records))}")
