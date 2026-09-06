"""P2-F 批量/计划性采购入口 + case 目录化存储回归测试。

验证点（对照 ROADMAP P2「批量入口」）：
  1. submit_many：多条需求批量提交 -> 各自独立 case，计划汇总（含挂起/失败不阻断）；
  2. list_plan：按计划汇总所有 case 当前状态；plan_id 落库可查询；
  3. runner.save_report：产物写入 <output_dir>/<case_id>/ 目录（case 目录化存储）；
  4. RBAC：批量提交需 procurement.submit；审计留痕 procurement.plan.submit；
  5. HTTP 端点：POST /procurements/batch + GET /procurements/plans/{plan_id}。
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from procurement_agents.access import AuthorizationError
from procurement_agents.config import AppConfig
from procurement_agents.runner import ProcurementRunner
from procurement_agents.service import ProcurementService

_REQ_A = "紧急采购 10 台工业级交换机，具体预算与交期待定。"          # 挂起（缺预算）
_REQ_B = "采购 12 台 48口万兆交换机，预算 40 万元，30 天内交付。"      # IT 多候选放行跑完
_REQ_C = "采购 2 台 2U 机架式服务器，预算 20 万元。"                  # 跑完


@pytest.fixture()
def ws():
    root = Path(__file__).resolve().parents[1] / ".tmp" / "plan"
    root.mkdir(parents=True, exist_ok=True)
    d = root / f"ws-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _svc(ws: Path, name: str = "cases.sqlite") -> ProcurementService:
    cfg = AppConfig.load()
    cfg.llm.provider = "mock"
    cfg.workflow.auto_approve_holds = False
    cfg.workflow.output_dir = str(ws / "out")
    return ProcurementService(config=cfg, case_db=str(ws / name))


def test_submit_many_plan_view(ws):
    """批量提交：各自成 case；缺预算条挂起、其余跑完；计划汇总正确。"""
    with _svc(ws) as svc:
        plan = svc.submit_many(
            [{"request_text": _REQ_A, "case_id": "PC-PLAN-A"},
             {"request_text": _REQ_B, "case_id": "PC-PLAN-B"},
             {"request_text": _REQ_C, "case_id": "PC-PLAN-C"}],
            plan_id="PLAN-TEST-1", actor="zhangsan",
        )
        assert plan["total"] == 3 and plan["submitted"] == 3
        statuses = {r["case_id"]: r["status"] for r in plan["results"]}
        assert statuses["PC-PLAN-A"] == "needs_input"
        assert statuses["PC-PLAN-B"] == "done"
        assert statuses["PC-PLAN-C"] == "done"

        # 各 case 可独立审批续跑（互不干扰）
        svc.resume("PC-PLAN-A", approved=True, actor="wangjl", comment="放行")
        summary = svc.list_plan("PLAN-TEST-1")
        assert summary["count"] == 3
        states = {c["case_id"]: c["status"] for c in summary["cases"]}
        assert states["PC-PLAN-A"] == "done"


def test_submit_many_blank_item_does_not_abort(ws):
    """空需求文本 -> 该条 error，不阻断整批。"""
    with _svc(ws) as svc:
        plan = svc.submit_many(
            [{"request_text": _REQ_B, "case_id": "PC-PLAN-OK"},
             {"request_text": "   ", "case_id": "PC-PLAN-BLANK"}],
            plan_id="PLAN-TEST-2", actor="zhangsan",
        )
        assert plan["total"] == 2 and plan["submitted"] == 1
        assert any(r["case_id"] == "PC-PLAN-BLANK" and r["status"] == "failed" for r in plan["results"])


def test_submit_many_rbac_and_audit(ws):
    """批量提交需 procurement.submit；动作与失败留审计。"""
    with _svc(ws) as svc:
        with pytest.raises(AuthorizationError):
            svc.submit_many([{"request_text": _REQ_B}], actor="hg")   # 合规岗无权
        plan = svc.submit_many([{"request_text": _REQ_B}], plan_id="PLAN-AUD", actor="zhangsan")
        audits = svc.store.list_audit(action="procurement.plan.submit")
        assert any(a["resource"] == "PLAN-AUD" and a["actor_id"] == "zhangsan" for a in audits)
        assert any(a["resource"] == "PLAN-AUD" for a in audits)
        assert plan["plan_id"] == "PLAN-AUD"


def test_list_plan_empty_plan(ws):
    """不存在的计划 -> count=0。"""
    with _svc(ws) as svc:
        assert svc.list_plan("PLAN-NONE")["count"] == 0


def test_case_directory_storage(ws):
    """save_report 目录化：产物写入 <out>/<case_id>/ 目录。"""
    cfg = AppConfig.load()
    cfg.llm.provider = "mock"
    cfg.workflow.auto_approve_holds = True
    out = Path(ws) / "out"
    cfg.workflow.output_dir = str(out)
    with ProcurementRunner(cfg) as runner:
        result = runner.run(_REQ_B, case_id="PC-DIR-1")
        md = result.save_report()
        assert md.parent == out / "PC-DIR-1"
        assert md.name == "report.md" and md.exists()
        assert result.trace_path is not None
        assert result.trace_path.parent == out / "PC-DIR-1"
        assert result.trace_path.exists()


def test_plan_http_batch_endpoint(ws):
    """HTTP 批量端点 + 计划查询端点（需身份）。"""
    from fastapi.testclient import TestClient

    import procurement_agents.web_api as web_api

    cfg = AppConfig.load()
    cfg.llm.provider = "mock"
    cfg.workflow.auto_approve_holds = False
    cfg.workflow.output_dir = str(Path(ws) / "out")
    svc = ProcurementService(config=cfg, case_db=str(Path(ws) / "web.sqlite"))
    web_api._service = svc
    try:
        client = TestClient(web_api.app)
        buyer = {"X-Actor-Id": "zhangsan"}
        r = client.post("/procurements/batch", json={
            "items": [{"request_text": _REQ_A, "case_id": "PC-BW-A"},
                      {"request_text": _REQ_B, "case_id": "PC-BW-B"}],
            "plan_id": "PLAN-WEB-1",
        }, headers=buyer)
        assert r.json()["code"] == 200
        data = r.json()["data"]
        assert data["plan_id"] == "PLAN-WEB-1" and data["submitted"] == 2

        g = client.get("/procurements/plans/PLAN-WEB-1", headers=buyer)
        assert g.json()["data"]["count"] == 2

        # 未带身份 -> 401
        anon = client.post("/procurements/batch", json={"items": [{"request_text": _REQ_B}]})
        assert anon.json()["code"] == 401
    finally:
        svc.close()
        web_api._service = None
