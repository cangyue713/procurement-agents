"""各业务 Agent 单元测试（不启动 LangGraph，纯领域逻辑；价格来自库内价目）。"""
from __future__ import annotations

import pytest

from procurement_agents.agents.base import AgentError
from procurement_agents.agents.comparison import ComparisonAgent
from procurement_agents.agents.compliance import ComplianceAgent
from procurement_agents.agents.contract import ContractDraftAgent
from procurement_agents.agents.requirement import RequirementAgent
from procurement_agents.agents.strategy import StrategyAgent
from procurement_agents.agents.supplier import SupplierAgent
from procurement_agents.config import RuleConfig
from procurement_agents.knowledge.supplier_lib import load_suppliers, recommend_suppliers


@pytest.fixture()
def demo_requirement(provider):
    agent = RequirementAgent(provider)
    art = agent.invoke({"request_text": (
        "数字化产线升级项目急需采购 40 台2U机架式服务器(Intel Xeon 双路 128GB 内存)；"
        "采购 2 台48口万兆交换机。预算 280 万元，含税含运到厂。要求 25 天内交付。"
    )})
    return art.model_dump(mode="json")


@pytest.fixture()
def it_candidates() -> list[dict]:
    """IT 品类候选（含库内价目）。"""
    art = recommend_suppliers("IT设备", size=3)
    return [c.model_dump(mode="json") for c in art.candidates]


def test_requirement_agent(demo_requirement):
    assert demo_requirement["category"] == "IT设备"
    assert len(demo_requirement["items"]) == 2


def test_strategy_agent_routes():
    sa = StrategyAgent(RuleConfig())
    big = sa.invoke({"requirement": {"budget_amount": 2_800_000, "notes": "常规采购"}})
    assert big.strategy.value == "邀请招标"
    small = sa.invoke({"requirement": {"budget_amount": 20_000, "notes": ""}})
    assert small.strategy.value == "直接采购"
    src = sa.invoke({"requirement": {"budget_amount": 600_000, "notes": "该型号为专利独家产品"}})
    assert src.strategy.value == "单一来源采购"
    nb = sa.invoke({"requirement": {"budget_amount": None, "notes": ""}})
    assert nb.strategy.value == "询比价" and nb.approval_needed


def test_supplier_agent_excludes_high_risk(demo_requirement, config):
    art = SupplierAgent(config.workflow).invoke({"requirement": demo_requirement})
    ids = [c.supplier_id for c in art.candidates]
    assert len(ids) >= 2
    assert "SUP-1008" not in ids          # 高风险天擎不在候选
    assert "SUP-1001" in ids
    # 候选应携带库内价目（价格与供应商介绍同源）
    for c in art.candidates:
        assert isinstance(c.price_items, str)
    assert any("高风险" in ex.reason or "禁入" in ex.reason for ex in art.excluded)


def test_comparison_agent_ranks_by_catalog(demo_requirement, it_candidates):
    """比价基于库内价目×需求行：候选 华云/中科/蓝海。"""
    art = ComparisonAgent().invoke({
        "requirement": demo_requirement,
        "candidates": it_candidates,
    })
    assert len(art.rows) == 3
    by_id = {r.supplier_id: r for r in art.rows}
    # 蓝海总价最低(197.9万)但交期 45 天拉低综合分；华云综合第一
    assert art.lowest_bidder_id == "SUP-1004"
    assert art.rows[0].supplier_id == "SUP-1001"      # 华云
    assert art.rows[0].rank == 1
    assert by_id["SUP-1001"].total_amount == 2112000.0   # 40*51500 + 2*26000（2 行需求）
    assert art.recommended.supplier_id == "SUP-1001"
    assert art.recommended.price_gap_pct and art.recommended.price_gap_pct > 0


def test_compliance_agent_intercepts_c3_and_c2(demo_requirement, it_candidates):
    """蓝海(交期45>需求25*1.3) 被 C3 否决；附加高风险候选(天擎)被 C2 否决。"""
    from procurement_agents.domain.enums import CheckLevel

    cands = list(it_candidates)
    tianqing = next(s for s in load_suppliers() if s["id"] == "SUP-1008")
    cands.append({
        "supplier_id": tianqing["id"], "name": tianqing["name"],
        "risk_level": tianqing["risk_level"], "certifications": tianqing["certifications"],
        "performance_rating": tianqing["performance_rating"],
        "price_items": tianqing["price_items"], "delivery_days": tianqing["delivery_days"],
        "warranty_months": tianqing["warranty_months"], "payment_terms": tianqing["payment_terms"],
    })
    art = ComplianceAgent(RuleConfig()).invoke({
        "requirement": demo_requirement,
        "candidates": cands,
    })
    assert "SUP-1004" in art.rejected        # C3 交期严重超期
    assert "SUP-1008" in art.rejected        # C2 高风险
    assert "SUP-1001" in art.approved_supplier_ids
    assert "SUP-1002" in art.approved_supplier_ids
    assert art.eligible
    assert any(c.rule == "C2" and c.level == CheckLevel.FAIL for c in art.checks)
    assert any(c.rule == "C3" and c.level == CheckLevel.FAIL for c in art.checks)


def test_contract_agent_fallback_when_recommended_rejected(demo_requirement, it_candidates):
    """比价推荐(华云)被合规否决 -> 合同应切换到合规放行且比价名次最高的中科。"""
    comparison = ComparisonAgent().invoke({
        "requirement": demo_requirement, "candidates": it_candidates,
    })
    # 人为构造合规：否决华云，放行 中科/蓝海
    compliance = {
        "eligible": True,
        "approved_supplier_ids": ["SUP-1002", "SUP-1004"],
        "rejected": ["SUP-1001"],
        "conditions": ["原比价推荐对象 SUP-1001 未通过合规审查"],
    }
    art = ContractDraftAgent().invoke({
        "requirement": demo_requirement,
        "comparison": comparison.model_dump(mode="json"),
        "compliance": compliance,
        "candidates": it_candidates,
        "case_id": "PC-T-CONTRACT",
    })
    assert art.supplier_id == "SUP-1002"
    assert art.supplier_name == "中科智算网络科技有限公司"
    assert art.total_amount == 2192000.0      # 40*53500 + 2*26000（2 行需求）
    assert art.po_number.startswith("PO-")
    assert "中科智算网络科技有限公司" in art.contract_text
    assert len(art.items) == len(demo_requirement["items"])
    assert any("未通过合规审查" in c for c in art.conditions)


def test_contract_agent_blocks_without_approved(demo_requirement, it_candidates):
    from procurement_agents.domain.models import ComparisonArtifact

    comparison = ComparisonArtifact(
        rows=[], lowest_bidder_id=None, recommended=None, method="x",
    ).model_dump(mode="json")
    compliance = {
        "eligible": False,
        "approved_supplier_ids": [],
        "rejected": ["SUP-1001"],
        "conditions": [],
    }
    with pytest.raises(AgentError):
        ContractDraftAgent().invoke({
            "requirement": demo_requirement,
            "comparison": comparison,
            "compliance": compliance,
            "candidates": it_candidates,
            "case_id": "PC-T-BLOCK",
        })
