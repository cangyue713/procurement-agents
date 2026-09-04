"""Agent-6 PO/合同草稿生成：基于合规放行与比价结论定标，起草采购订单与合同。

说明：成交候选来自供应商主数据（价格/交期/付款/质保都在 suppliers.csv 中），
无询价报价环节 —— 合同行明细 = 需求行 × 成交候选的库内价目。
"""
from __future__ import annotations

import string
from datetime import datetime
from typing import Any, Dict, List

from pydantic import BaseModel

from procurement_agents.agents.base import AgentError, BaseAgent, require_keys
from procurement_agents.config import PROJECT_ROOT
from procurement_agents.domain.enums import PhaseName
from procurement_agents.domain.models import ContractArtifact, QuoteLine
from procurement_agents.knowledge.supplier_lib import catalog_lines

TEMPLATE_DIR = PROJECT_ROOT / "templates"


class ContractDraftAgent(BaseAgent):
    """定标（合规放行 ∩ 比价名次）+ 模板渲染 PO 与合同草稿。"""

    phase = PhaseName.CONTRACT
    display_name = "合同草稿Agent"
    description = "依比价与合规结论从库内候选定标，生成采购订单(PO)与采购合同草稿"

    def __init__(self, buyer: str = "示例制造有限公司 采购中心") -> None:
        self._buyer = buyer

    def invoke(self, inputs: Dict[str, Any]) -> BaseModel:
        require_keys(
            inputs, ["requirement", "comparison", "compliance", "candidates"], agent=self.display_name
        )
        requirement = inputs["requirement"]
        comparison = inputs["comparison"]
        compliance = inputs["compliance"]
        candidates: List[Dict[str, Any]] = inputs["candidates"]
        approved = set(compliance.get("approved_supplier_ids") or [])
        if not approved:
            raise AgentError(f"[{self.display_name}] 无合规放行的供应商，禁止生成合同")

        rows = sorted(comparison.get("rows", []), key=lambda r: int(r.get("rank", 999)))
        winner_row = next((r for r in rows if r["supplier_id"] in approved), None)
        if winner_row is None:
            raise AgentError(f"[{self.display_name}] 合规放行名单与比价行无交集，无法定标")
        winner_cand = next((c for c in candidates if c["supplier_id"] == winner_row["supplier_id"]), None)
        if winner_cand is None:
            raise AgentError(f"[{self.display_name}] 未找到成交供应商 {winner_row['supplier_id']} 的库内数据")

        case_id = str(inputs.get("case_id") or datetime.now().strftime("%Y%m%d-%H%M%S"))
        po_no = f"PO-{case_id}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        budget = requirement.get("budget_amount")
        total = float(winner_row["total_amount"])
        if budget and total > budget:
            raise AgentError(
                f"[{self.display_name}] 定标金额 {total:,.0f} 元超过预算 {budget:,.0f} 元，禁止生成合同"
            )

        # 行明细 = 需求行 × 成交候选价目；价目缺覆盖时兜底按需求行生成占位行
        lines, _total, missing = catalog_lines(str(winner_cand.get("price_items") or ""), requirement.get("items") or [])
        items = [QuoteLine(**l) for l in lines]
        if not items:
            req_lines = requirement.get("items", [])
            total_qty = sum(float(r.get("quantity", 0)) for r in req_lines) or 1.0
            items = [
                QuoteLine(
                    description=it.get("description", ""),
                    quantity=float(it.get("quantity", 0)),
                    unit_price=round(total * float(it.get("quantity", 0)) / total_qty, 2),
                    amount=round(total * float(it.get("quantity", 0)) / total_qty, 2),
                )
                for it in req_lines
            ]

        conditions = [str(c) for c in (compliance.get("conditions") or [])]
        recommended_id = (comparison.get("recommended") or {}).get("supplier_id")
        if recommended_id and recommended_id != winner_row["supplier_id"]:
            conditions.append(
                f"注：比价推荐对象 {recommended_id} 未通过合规审查，本次改由合规放行且比价名次最高的 "
                f"{winner_row['supplier_name']} 成交。"
            )
        if missing:
            conditions.append(f"注意：成交供应商价目未覆盖需求行：{'、'.join(missing[:3])}，该部分需另行议价或补录价目")

        delivery_days = winner_row.get("delivery_days")
        warranty = int(winner_cand.get("warranty_months", 0) or 0)
        payment = str(winner_cand.get("payment_terms") or "货到验收合格后 30 天电汇")

        ctx = {
            "po_no": po_no,
            "buyer": self._buyer,
            "supplier_name": winner_row["supplier_name"],
            "title": requirement.get("title", "采购"),
            "now": now,
            "total": f"{total:,.2f}",
            "delivery_days": delivery_days or requirement.get("delivery_days") or "双方协商",
            "payment": payment,
            "warranty": f"{warranty} 个月" if warranty else "按供应商承诺",
            "currency": "CNY",
            "conditions_text": "；".join(conditions) if conditions else "无",
            "item_lines": "\n".join(
                f"{i + 1}. {li.description} × {li.quantity:g}（单价 {li.unit_price:,.2f} 元，小计 {li.amount:,.2f} 元）"
                for i, li in enumerate(items)
            ),
        }

        po_text = self._render("po.md.tpl", ctx)
        contract_text = self._render("contract.md.tpl", ctx)

        return ContractArtifact(
            po_number=po_no,
            contract_title=f"{requirement.get('title', '')} 采购合同",
            buyer=self._buyer,
            supplier_id=winner_row["supplier_id"],
            supplier_name=winner_row["supplier_name"],
            total_amount=round(total, 2),
            currency="CNY",
            delivery_days=delivery_days,
            payment_terms=payment,
            warranty_months=warranty,
            penalty_rate=0.0005,
            items=items,
            conditions=conditions,
            po_text=po_text,
            contract_text=contract_text,
            based_on=[
                f"比价: {comparison.get('method', '')}",
                f"合规: {compliance.get('conclusion', '')[:100]}",
            ],
        )

    # -- 模板渲染 -----------------------------------------------------------
    @staticmethod
    def _render(tpl_name: str, ctx: Dict[str, Any]) -> str:
        path = TEMPLATE_DIR / tpl_name
        if not path.exists():
            raise AgentError(f"[合同草稿Agent] 模板缺失: {path}")
        template = string.Template(path.read_text(encoding="utf-8"))
        try:
            return template.safe_substitute(ctx)
        except Exception as exc:  # 模板渲染失败统一包装为 AgentError
            raise AgentError(f"[合同草稿Agent] 模板渲染失败: {exc}") from exc
