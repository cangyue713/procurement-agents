"""端到端测试：LangGraph 全链路（6 业务节点 + 仲裁 + 人审）+ 追踪产物。

新流程（无询价/报价）：需求理解 -> 策略判定 -> 供应商推荐(库内价目)
-> 比价分析 -> 合规审查 -> PO/合同草稿。
"""
from __future__ import annotations

import json


def _missing_budget_text() -> str:
    """预算与交期均缺失的需求（触发策略挂起 + 人审）。"""
    return "紧急采购 10 台工业级交换机，具体预算与交期待定。"


def test_end_to_end_demo(demo_data, runner, config):
    result = runner.run(request_text=demo_data["request_text"], case_id=demo_data["case_id"])
    assert result.is_done, result.summary_text
    assert result.status == "done"

    # 剧本断言：候选/比价/合规/成交均来自库内价目数据
    strategy = (result.state.get("strategy") or {}).get("strategy")
    contract = result.contract
    assert strategy == demo_data["expected_strategy"]
    assert contract.get("supplier_name") == demo_data["expected_winner"]
    assert contract.get("total_amount") == 2126400.0
    assert contract.get("po_text") and contract.get("contract_text")
    # 合同行明细按需求 3 行生成
    assert len(contract.get("items", [])) == 3

    # 候选自带价目（价格与供应商介绍同源）
    candidates = (result.state.get("shortlist") or {}).get("candidates", [])
    assert len(candidates) == 3
    assert all(c.get("price_items") for c in candidates)

    # 合规否决：蓝海(库内交期45天) 被 C3 否决；华云/中科放行
    compliance = result.state.get("compliance") or {}
    assert "SUP-1004" in compliance.get("rejected", [])
    assert "SUP-1001" in compliance.get("approved_supplier_ids", [])
    assert "SUP-1002" in compliance.get("approved_supplier_ids", [])

    # 仲裁全程：无阻断；首段为需求理解质量分 100
    verdicts = [r.get("verdict") for r in result.arbitration_records]
    assert "block" not in verdicts
    assert result.arbitration_records[0]["phase"] == "需求理解"
    assert result.arbitration_records[0]["quality_score"] == 100

    # 产物报告与追踪（写到工作区内 outputs 目录，系统临时区不可写）
    md_path = result.save_report()
    assert md_path.exists()
    assert result.trace_path is not None and result.trace_path.exists()
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "done"
    assert "服务器" in trace["requirement"]["title"]


def test_incomplete_demand_auto_approves(demo_data, runner):
    """缺预算 -> 策略挂起需审批；auto_approve 下自动放行留痕并完成。"""
    result = runner.run(request_text=_missing_budget_text(), case_id="PC-T-INCOMPLETE")
    assert result.is_done
    strategy = result.state.get("strategy") or {}
    assert strategy.get("approval_needed") is True
    assert any(a.get("decision") == "auto_approved" for a in result.state.get("approvals", []))


def test_human_reject_blocks(demo_data, runner):
    """接入人工决策器并拒绝策略审批 -> 流程应阻断。"""
    result = runner.run(
        request_text=_missing_budget_text(),
        case_id="PC-T-REJECT",
        auto_approve=False,
        human_decider=lambda phase, summary: False,  # 一律拒绝
    )
    assert result.status == "blocked"
    assert any(i.get("stage") == "approval_node" for i in result.issues)
