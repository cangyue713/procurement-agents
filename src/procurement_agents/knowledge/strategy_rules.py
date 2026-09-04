"""采购策略判定规则（知识层，纯规则、可审计）。"""
from __future__ import annotations

from typing import Any, Dict, List

from procurement_agents.config import RuleConfig
from procurement_agents.domain.enums import ProcurementStrategy, UrgencyLevel

SINGLE_SOURCE_HINTS = ("独家", "专利", "专用", "唯一", "指定品牌", "原厂唯一", "兼容现有", "不可替代", "紧急抢修")


def decide_strategy(requirement: Dict[str, Any], rules: RuleConfig | None = None) -> Dict[str, Any]:
    """依据需求结构化结果判定采购策略。

    规则：
      R1 单一来源特征(专利/独家/唯一供应)          -> 单一来源采购
      R2 预算缺失                                 -> 询比价 + 需人工确认
      R3 预算 <= direct_purchase_max              -> 直接采购
      R4 预算 > tender_threshold                  -> 邀请招标(>=3家)
      R5 预算 > 5 * tender_threshold 或 法定情形    -> 公开招标
      R6 其余                                     -> 询比价
    """
    rules = rules or RuleConfig()
    text = str(requirement.get("notes", "")) + str(requirement.get("title", ""))
    budget = requirement.get("budget_amount")
    urgency = requirement.get("urgency", UrgencyLevel.NORMAL.value)
    rationale: List[str] = []
    applied: List[str] = []
    approval_needed = False

    if any(k in text for k in SINGLE_SOURCE_HINTS):
        applied.append("R1")
        rationale.append("需求含独家/专利/唯一供应特征，依法规可单一来源采购")
        strategy = ProcurementStrategy.SINGLE_SOURCE
    elif budget is None:
        applied.append("R2")
        rationale.append("需求未提供预算，先按询比价组织，需采购经理确认预算")
        strategy = ProcurementStrategy.RFQ
        approval_needed = True
    elif budget <= rules.direct_purchase_max:
        applied.append("R3")
        rationale.append(f"预算 {budget:,.0f} 元 <= 直接采购上限 {rules.direct_purchase_max:,.0f} 元")
        strategy = ProcurementStrategy.DIRECT_PURCHASE
    elif budget > rules.tender_threshold * 5:
        applied.append("R5")
        rationale.append(f"预算 {budget:,.0f} 元 超过公开招标门槛 {rules.tender_threshold * 5:,.0f} 元")
        strategy = ProcurementStrategy.OPEN_TENDER
    elif budget > rules.tender_threshold:
        applied.append("R4")
        rationale.append(
            f"预算 {budget:,.0f} 元 > 招标门槛 {rules.tender_threshold:,.0f} 元，按邀请招标组织"
            f"(至少邀请 {rules.invite_bid_min} 家)"
        )
        strategy = ProcurementStrategy.INVITED_TENDER
    else:
        applied.append("R6")
        rationale.append(f"预算 {budget:,.0f} 元在询比价区间内，走询比价流程")
        strategy = ProcurementStrategy.RFQ

    if urgency in (UrgencyLevel.URGENT.value, UrgencyLevel.CRITICAL.value):
        rationale.append(f"需求紧急度:{urgency}，各环节需提速并压缩交期要求")

    return {
        "strategy": strategy.value,
        "rationale": rationale,
        "rules_applied": applied,
        "approval_needed": approval_needed,
        "remark": "",
    }
