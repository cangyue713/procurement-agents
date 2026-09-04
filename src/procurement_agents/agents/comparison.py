"""Agent-4 比价分析：把候选供应商的【库内价目】与需求行结合，横向评分排名。

注：本流程不含询价/报价环节 —— 价格来自供应商主数据(price_items)，
按需求行的数量自动计出各家总价，再做 价格60%+交期20%+绩效20% 综合评分。
"""
from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel

from procurement_agents.agents.base import AgentError, BaseAgent, require_keys
from procurement_agents.domain.enums import PhaseName
from procurement_agents.domain.models import (
    ComparisonArtifact,
    ComparisonRow,
    Recommendation,
)
from procurement_agents.knowledge.supplier_lib import catalog_lines

# 评分权重（可配置；演示采用 价格60 交期20 绩效20）
W_PRICE, W_DELIVERY, W_PERF = 0.6, 0.2, 0.2


class ComparisonAgent(BaseAgent):
    """按 总价/交期/绩效 三维评分 → 比价表 + 推荐（含未选最低价的理由要求）。"""

    phase = PhaseName.COMPARISON
    display_name = "比价分析Agent"
    description = "基于供应商库内价目×需求数量自动比价：价格/交期/历史绩效加权评分"

    def invoke(self, inputs: Dict[str, Any]) -> BaseModel:
        require_keys(inputs, ["requirement", "candidates"], agent=self.display_name)
        requirement = inputs["requirement"]
        candidates: List[Dict[str, Any]] = inputs["candidates"]
        req_items = requirement.get("items") or []
        priced = []
        for cand in candidates:
            sid = str(cand.get("supplier_id"))
            lines, total, missing = catalog_lines(str(cand.get("price_items") or ""), req_items)
            priced.append({
                "supplier_id": sid,
                "supplier_name": str(cand.get("name") or sid),
                "lines": lines,
                "total": total,
                "missing": missing,
                "delivery_days": cand.get("delivery_days"),
                "performance_rating": float(cand.get("performance_rating", 0) or 0),
            })
        valid = [p for p in priced if p["total"] > 0 and not p["missing"]]
        if len(valid) < 1:
            raise AgentError(
                f"[{self.display_name}] 没有任何候选供应商的价目能覆盖需求行，请先补录 suppliers.csv 价目"
            )
        if len(valid) < 2:
            raise AgentError(
                f"[{self.display_name}] 仅 {len(valid)} 家候选价目覆盖需求，无法形成有效比价，请扩充供应商价目"
            )

        min_total = min(p["total"] for p in valid)
        req_days = requirement.get("delivery_days")

        rows: List[ComparisonRow] = []
        for p in sorted(valid, key=lambda x: x["supplier_id"]):
            total = p["total"]
            price_score = (min_total / total * 100) if total > 0 else 0.0
            delivery_score = 0.0
            q_days = p["delivery_days"]
            if q_days and req_days:
                delivery_score = min(100.0, float(req_days) / float(q_days) * 100)
            perf = p["performance_rating"] / 5.0 * 100
            total_score = W_PRICE * price_score + W_DELIVERY * delivery_score + W_PERF * perf
            note = ""
            if not q_days:
                note += "库内未登记交期；"
            if p["missing"]:
                note += f"价目未覆盖行:{'、'.join(p['missing'][:2])}；"
            rows.append(ComparisonRow(
                supplier_id=p["supplier_id"],
                supplier_name=p["supplier_name"],
                total_amount=total,
                delivery_days=q_days,
                price_score=round(price_score, 1),
                delivery_score=round(delivery_score, 1),
                perf_score=round(perf, 1),
                total_score=round(total_score, 1),
                rank=0,
                notes=note,
            ))

        rows.sort(key=lambda r: (-r.total_score, r.total_amount))
        for i, r in enumerate(rows):
            r.rank = i + 1

        lowest = min(rows, key=lambda r: r.total_amount)
        winner = rows[0]
        gap = round((winner.total_amount - lowest.total_amount) / lowest.total_amount * 100, 2) if lowest.total_amount else 0.0

        reasons: List[str] = []
        risks: List[str] = []
        if winner.supplier_id != lowest.supplier_id:
            reasons.append(
                f"未选总价最低的 {lowest.supplier_name}({lowest.total_amount:,.0f} 元)，"
                f"因综合评分更高(交期/历史绩效)，溢价 {gap}%"
            )
            risks.append(
                f"推荐对象非总价最低：{winner.supplier_name} 比最低价 {lowest.supplier_name} 高 {gap}%，"
                "定标前请确认理由充分（如交期/绩效/合规）"
            )
        else:
            reasons.append(f"{winner.supplier_name} 总价最低且综合评分第一，推荐成交")
        reasons.append(f"评分方法: 价格{W_PRICE:.0%}+交期{W_DELIVERY:.0%}+历史绩效{W_PERF:.0%}")

        return ComparisonArtifact(
            method=f"价格{W_PRICE:.0%} + 交期{W_DELIVERY:.0%} + 历史绩效{W_PERF:.0%}（价目×需求数量自动计价）",
            rows=[r.model_dump(mode="json") for r in rows],
            lowest_bidder_id=lowest.supplier_id,
            recommended=Recommendation(
                supplier_id=winner.supplier_id,
                supplier_name=winner.supplier_name,
                reason="；".join(reasons),
                price_gap_pct=gap if winner.supplier_id != lowest.supplier_id else 0.0,
            ),
            risks=risks,
        )
