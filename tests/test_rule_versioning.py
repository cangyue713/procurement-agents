"""P2-D 规则版本化回归测试。

验证点（对照 ROADMAP P2「规则版本化」）：
  1. 规则注册表登记 C1~C6（合规）/ R1~R6（策略）/ ARB-*（仲裁），版本与变更登记齐备；
  2. 运行产物烙版本：合规产物/仲裁记录/收官报告均带 ruleset_version；
  3. 每条合规检查行可追溯命中规则版本；快照 hash 稳定可比对（跨版本 diff 依据）。
"""
from __future__ import annotations

import pytest

from procurement_agents.config import AppConfig
from procurement_agents.knowledge import rule_registry as rr
from procurement_agents.knowledge.supplier_lib import SUPPLIERS_CSV, load_suppliers
from procurement_agents.runner import ProcurementRunner

DEMO = ("紧急采购 10 台工业级交换机与 2 台服务器，预算 280 万，"
        "25 天内交付，需通过 ISO9001 认证。")


@pytest.fixture()
def runner():
    cfg = AppConfig.load()
    cfg.llm.provider = "mock"
    cfg.workflow.auto_approve_holds = True
    return ProcurementRunner(cfg)


def test_registry_covers_all_named_rules():
    """规则注册表完整登记 C1~C6 / R1~R6 / 仲裁监测点。"""
    ids = {spec["rule_id"] for spec in rr.ALL_RULES.values()}
    assert {"C1", "C2", "C3", "C4", "C5", "C6"} <= ids
    assert {"R1", "R2", "R3", "R4", "R5", "R6"} <= ids
    assert {"ARB-REQ", "ARB-STR", "ARB-SUP", "ARB-CMP", "ARB-CL", "ARB-CT", "ARB-FIN"} <= ids
    assert all(spec.get("version") == rr.RULESET_VERSION for spec in rr.ALL_RULES.values())
    assert rr.RULESET_CHANGES and rr.RULESET_CHANGES[0]["version"] == rr.RULESET_VERSION


def test_ruleset_snapshot_stable_hash():
    """同一版本快照 hash 稳定（规则变更会改变 hash -> 可用于版本 diff）。"""
    a = rr.ruleset_snapshot()
    b = rr.ruleset_snapshot()
    assert a["version"] == b["version"] and a["hash"] == b["hash"]
    assert a["rule_count"] == len(rr.ALL_RULES)


def test_demo_run_artifacts_carry_ruleset_version(runner):
    """端到端产物烙规则集版本：compliance / arbitration / final_report。"""
    result = runner.run(DEMO, case_id="PC-RV-1", auto_approve=True)
    assert result.is_done

    compliance = result.state.get("compliance") or {}
    assert compliance.get("ruleset_version") == rr.RULESET_VERSION

    arb = result.state.get("arbitration") or []
    assert arb and all(r.get("ruleset_version") == rr.RULESET_VERSION for r in arb)

    report = result.state.get("final_report") or {}
    assert report.get("ruleset_version") == rr.RULESET_VERSION


def test_compliance_check_rows_trace_rule_version(runner):
    """每条合规检查行带命中规则版本（可追溯到具体规则 + 版本）。"""
    result = runner.run(DEMO, case_id="PC-RV-2", auto_approve=True)
    checks = (result.state.get("compliance") or {}).get("checks") or []
    assert checks
    for c in checks:
        rid = c["rule"]
        spec = rr.rule_spec(rid)
        assert c["rule_version"] == spec["version"] == rr.RULESET_VERSION
        assert spec["label"]                       # 规则说明齐备（报告/审计可读）


def test_ruleset_version_survives_service_view():
    """服务视图产物含版本（审计可读）。"""
    import shutil
    import uuid
    from pathlib import Path

    from procurement_agents.service import ProcurementService

    root = Path(__file__).resolve().parents[1] / ".tmp" / "rv"
    root.mkdir(parents=True, exist_ok=True)
    d = root / f"ws-{uuid.uuid4().hex[:6]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        cfg = AppConfig.load()
        cfg.llm.provider = "mock"
        cfg.workflow.auto_approve_holds = False
        cfg.workflow.output_dir = str(d / "out")
        with ProcurementService(config=cfg, case_db=str(d / "c.sqlite")) as svc:
            svc.submit("紧急采购 10 台工业级交换机，具体预算与交期待定。", case_id="PC-RV-3",
                       actor="zhangsan")
            view = svc.resume("PC-RV-3", approved=True, actor="wangjl", comment="ok")
            comp = view["artifacts"]["compliance"] or {}
            assert comp.get("ruleset_version") == rr.RULESET_VERSION
            report = view["artifacts"]["final_report"] or {}
            assert report.get("ruleset_version") == rr.RULESET_VERSION
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_repo_data_unaffected_by_versioning():
    """规则版本化不动主数据文件（行表仍是结构化数据源）。"""
    assert load_suppliers()
    assert SUPPLIERS_CSV.exists()
