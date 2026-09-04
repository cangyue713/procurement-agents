"""Agent-5 合规审查：对候选供应商的【库内信息】执行规则矩阵（无询价报价环节）。

规则编号（供追溯）：
  C1 禁入名单检查        fail：黑名单/经营异常
  C2 高风险供应商        fail：高风险；中风险给 warn
  C3 交期满足性          fail：库内交期 > 需求交期*1.3；warn：>需求交期
  C4 预算约束            fail：按价目×需求数量计出的总价 > 预算
  C5 价目有效性          warn：库内无价目或未覆盖需求行(无法成交/需补录)
  C6 资质匹配            warn：需求要求的认证供应商缺失
金额精度：C4 预算比较全 Decimal（P1.1）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from pydantic import BaseModel

from procurement_agents.agents.base import BaseAgent, require_keys
from procurement_agents.config import RuleConfig
from procurement_agents.domain.enums import CheckLevel, PhaseName, SupplierRiskLevel
from procurement_agents.domain.models import ComplianceArtifact, ComplianceCheck
from procurement_agents.domain.money import fmt_money, to_decimal
from procurement_agents.knowledge.supplier_lib import blacklist_ids, blacklist_names, catalog_lines


def _add_check(checks: List[Dict[str, Any]], rule: str, level: CheckLevel, message: str) -> None:
    """追加一条合规检查记录（参数化，避免内嵌函数捕获循环变量）。"""
    checks.append({"rule": rule, "level": level.value, "message": message})


class ComplianceAgent(BaseAgent):
    """对候选执行 黑名单/风险/交期/预算/价目/资质 规则矩阵。"""

    phase = PhaseName.COMPLIANCE
    display_name = "合规审查Agent"
    description = "内控规则矩阵审查候选供应商（库内数据），输出可成交名单与附带条件"

    def __init__(self, rules: RuleConfig | None = None) -> None:
        self._rules = rules or RuleConfig()

    def invoke(self, inputs: Dict[str, Any]) -> BaseModel:
        require_keys(inputs, ["requirement", "candidates"], agent=self.display_name)
        requirement = inputs["requirement"]
        candidates: List[Dict[str, Any]] = inputs["candidates"]
        req_items = requirement.get("items") or []

        all_checks: List[ComplianceCheck] = []
        approved: List[str] = []
        rejected: List[str] = []
        warn_notes: List[str] = []
        rejected_reasons: Dict[str, str] = {}

        for cand in candidates:
            sid = str(cand.get("supplier_id"))
            name = str(cand.get("name") or sid)
            checks: List[Dict[str, Any]] = []
            eligible = True

            # C1 禁入名单
            if sid in blacklist_ids() or name in blacklist_names():
                _add_check(checks, "C1", CheckLevel.FAIL, "供应商在禁入名单内，禁止成交")
                eligible = False
            # C2 风险等级
            risk = str(cand.get("risk_level") or SupplierRiskLevel.LOW.value)
            if risk == SupplierRiskLevel.HIGH.value:
                _add_check(checks, "C2", CheckLevel.FAIL, "高风险供应商，无专项审批不得成交")
                eligible = False
            elif risk == SupplierRiskLevel.MEDIUM.value:
                flags = cand.get("risk_flags") or []
                _add_check(checks, "C2", CheckLevel.WARN, f"中风险供应商({';'.join(flags)})，需评估后定标")
            # C3 交期
            req_days = requirement.get("delivery_days")
            q_days = cand.get("delivery_days")
            if req_days and q_days:
                if q_days > float(req_days) * 1.3:
                    _add_check(checks, "C3", CheckLevel.FAIL,
                               f"库内交期 {q_days} 天，超出需求 {req_days} 天 30% 以上，无法满足上线计划")
                    eligible = False
                elif q_days > float(req_days):
                    _add_check(checks, "C3", CheckLevel.WARN,
                               f"库内交期 {q_days} 天，超出需求交期 {req_days} 天，需采购经理确认")
            elif q_days is None:
                _add_check(checks, "C3", CheckLevel.WARN, "库内未登记交期")
            # C4 预算（按价目×需求数量）
            _, total, missing = catalog_lines(str(cand.get("price_items") or ""), req_items)
            budget = to_decimal(requirement.get("budget_amount"))
            if budget is not None and total > budget:
                _add_check(checks, "C4", CheckLevel.FAIL,
                           f"价目计得总价 {fmt_money(total)} 元超出预算 {fmt_money(budget)} 元")
                eligible = False
            # C5 价目有效性
            if not cand.get("price_items"):
                _add_check(checks, "C5", CheckLevel.WARN, "库内无价目，无法成交，需补录价目")
            elif missing:
                _add_check(checks, "C5", CheckLevel.WARN,
                           f"价目未覆盖需求行:{'、'.join(missing[:3])}，覆盖外部分无法计价")
            # C6 资质匹配需求
            have = {c.replace(" ", "").upper() for c in (cand.get("certifications") or [])}
            for req_line in requirement.get("quality_requirements") or []:
                m = re.search(r"ISO\s?[-]?\s?\d{2,5}", str(req_line))
                if m and m.group(0).replace(" ", "").upper() not in have:
                    _add_check(checks, "C6", CheckLevel.WARN, f"需求要求[{m.group(0)}]，供应商资质清单未见对应认证")

            for c in checks:
                all_checks.append(ComplianceCheck(rule=c["rule"], subject=name, level=c["level"], message=c["message"]))
            if eligible:
                approved.append(sid)
                warns = [c["message"] for c in checks if c["level"] == CheckLevel.WARN.value]
                if warns:
                    warn_notes.append(f"{name}: {';'.join(warns)}")
            else:
                rejected.append(sid)
                fails = [f"{c['rule']}: {c['message']}" for c in checks if c["level"] == CheckLevel.FAIL.value]
                rejected_reasons[sid] = "；".join(fails) or "未通过合规检查"

        names = {str(c.get("supplier_id")): str(c.get("name")) for c in candidates}
        if not approved:
            conclusion = "全部候选未通过合规审查，本次采购应予中止或扩充候选后重审"
            eligible_flag = False
        else:
            eligible_flag = True
            conclusion = (
                f"合规放行 {len(approved)} 家：{', '.join(names.get(s, s) for s in approved)}。"
                + (f"否决 {len(rejected)} 家：{', '.join(names.get(s, s) + '(' + rejected_reasons[s] + ')' for s in rejected)}。" if rejected else "")
            )

        conditions: List[str] = []
        if warn_notes:
            conditions.append("成交对象存在提示事项，须在 PO/合同中落实：" + "；".join(warn_notes[:5]))
        if not requirement.get("delivery_days"):
            conditions.append("需求未明确交付期，签约前请补充交付里程碑")
        if requirement.get("urgency") in ("紧急", "特急"):
            conditions.append("紧急需求：建议在合同中约定提前交付激励机制并加严进度跟踪")

        return ComplianceArtifact(
            eligible=eligible_flag,
            checks=all_checks,
            rejected=rejected,
            approved_supplier_ids=approved,
            conditions=conditions,
            conclusion=conclusion,
        )
