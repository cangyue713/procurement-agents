"""仲裁 Agent 全程监控规则测试（无询价/报价环节，含价目完整性监控）。"""
from __future__ import annotations

import pytest

from procurement_agents.agents.arbitrator import ArbitratorAgent
from procurement_agents.domain.enums import PhaseName, VerdictAction


def _verdict(phase: PhaseName, artifact: dict, ctx: dict | None = None):
    rec = ArbitratorAgent().invoke({
        "phase": phase.value,
        "artifact": artifact,
        "context": ctx or {},
    })
    return rec


def test_requirement_block_when_no_items():
    rec = _verdict(PhaseName.REQUIREMENT, {"title": "无明细需求", "items": []})
    assert rec.verdict == VerdictAction.BLOCK


def test_requirement_warn_missing_budget_and_delivery():
    rec = _verdict(PhaseName.REQUIREMENT, {
        "title": "需求", "items": [{"description": "a", "quantity": 1, "uom": "台"}],
        "budget_amount": None, "delivery_days": None,
    })
    assert rec.verdict == VerdictAction.PROCEED
    names = [c.name for c in rec.checks]
    assert "预算完整性" in names and "交期完整性" in names
    assert rec.quality_score < 100


def test_strategy_approval_hold():
    rec = _verdict(PhaseName.STRATEGY, {
        "strategy": "询比价", "approval_needed": True, "rules_applied": ["R2"], "rationale": []
    })
    assert rec.verdict == VerdictAction.HOLD


def test_supplier_ok_and_blocked():
    ok = _verdict(PhaseName.SUPPLIER, {
        "candidates": [{"supplier_id": "S1", "name": "x", "score": 90, "price_items": "a=1"}],
        "excluded": [],
    })
    assert ok.verdict == VerdictAction.PROCEED
    bad = _verdict(PhaseName.SUPPLIER, {"candidates": [], "excluded": []})
    assert bad.verdict == VerdictAction.BLOCK


def test_supplier_price_items_monitoring():
    """候选库内无价目/价目未覆盖需求行 -> 仲裁提示（不阻断）。"""
    ctx = {"requirement": {"items": [
        {"description": "2U机架式服务器", "quantity": 1, "uom": "台"},
        {"description": "48口万兆交换机", "quantity": 1, "uom": "台"},
    ]}}
    rec = _verdict(PhaseName.SUPPLIER, {
        "candidates": [
            {"supplier_id": "S1", "name": "只覆盖一行", "score": 90,
             "price_items": "2U机架式服务器=100"},
            {"supplier_id": "S2", "name": "无价目", "score": 80, "price_items": ""},
        ],
        "excluded": [],
    }, ctx)
    assert rec.verdict == VerdictAction.PROCEED
    assert any(c.name == "价目缺失" for c in rec.checks)
    assert any(c.name == "价目覆盖" for c in rec.checks)


def test_high_risk_recommendation_hold():
    """比价推荐对象为高风险供应商 -> 定标预警(hold)。"""
    rec = _verdict(PhaseName.COMPARISON, {
        "rows": [{"supplier_id": "SUP-1008", "total_amount": 100, "rank": 1}],
        "lowest_bidder_id": "SUP-1008",
        "recommended": {"supplier_id": "SUP-1008", "supplier_name": "天擎科技", "reason": "价格最低"},
    })
    assert rec.verdict == VerdictAction.HOLD
    assert any(c.name == "高风险定标预警" for c in rec.checks)


def test_non_lowest_recommendation_needs_reason():
    """未选总价最低者但理由充分 -> 放行并留痕。"""
    rec = _verdict(PhaseName.COMPARISON, {
        "rows": [
            {"supplier_id": "A", "total_amount": 90, "rank": 2},
            {"supplier_id": "B", "total_amount": 100, "rank": 1},
        ],
        "lowest_bidder_id": "A",
        "recommended": {"supplier_id": "B", "supplier_name": "b",
                        "reason": "因交期更优、历史绩效更高，故综合评分第一"},
    })
    assert rec.verdict == VerdictAction.PROCEED
    assert any(c.name == "非最低价定标" for c in rec.checks)


def test_compliance_recommended_rejected_hold():
    ctx = {"comparison": {"recommended": {"supplier_id": "SUP-1008"}}}
    rec = _verdict(PhaseName.COMPLIANCE, {
        "eligible": True,
        "approved_supplier_ids": ["SUP-1001"],
        "rejected": ["SUP-1008"],
        "checks": [{"rule": "C2", "level": "不通过", "message": "高风险"}],
    }, ctx)
    assert rec.verdict == VerdictAction.HOLD          # 定标切换预警


def test_contract_budget_and_compliance_link():
    ctx = {
        "requirement": {"budget_amount": 500000},
        "compliance": {"approved_supplier_ids": ["S1"]},
    }
    rec = _verdict(PhaseName.CONTRACT, {
        "supplier_id": "S1", "supplier_name": "x", "total_amount": 600000,
        "po_text": "PO", "contract_text": "C", "items": [{"description": "a"}], "conditions": [],
    }, ctx)
    assert rec.verdict == VerdictAction.BLOCK          # 预算红线

    ok = _verdict(PhaseName.CONTRACT, {
        "supplier_id": "S1", "supplier_name": "x", "total_amount": 400000,
        "po_text": "PO", "contract_text": "C", "items": [{"description": "a"}], "conditions": [],
    }, ctx)
    assert ok.verdict == VerdictAction.PROCEED


def test_final_ok():
    ctx = {
        "arbitration": [
            {"phase": "需求理解", "verdict": "proceed", "quality_score": 100},
            {"phase": "比价分析", "verdict": "hold", "quality_score": 90},
        ]
    }
    rec = _verdict(PhaseName.FINAL, {}, ctx)
    assert rec.verdict == VerdictAction.PROCEED
