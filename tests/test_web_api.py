"""P1.2 FastAPI 服务化回归测试：三端点全链路（HTTP 层 + TestClient）。"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")  # 未安装 fastapi 时跳过（CI 已装）

import procurement_agents.web_api as web_api  # noqa: E402
from procurement_agents.config import AppConfig  # noqa: E402
from procurement_agents.service import ProcurementService  # noqa: E402

_MISSING_BUDGET = "紧急采购 10 台工业级交换机，具体预算与交期待定。"


@pytest.fixture()
def ws():
    root = Path(__file__).resolve().parents[1] / ".tmp" / "web"
    root.mkdir(parents=True, exist_ok=True)
    d = root / f"ws-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def client(ws):
    """隔离的单例 service（工作区 .tmp 内 sqlite）+ TestClient。"""
    cfg = AppConfig.load()
    cfg.llm.provider = "mock"
    cfg.workflow.auto_approve_holds = False
    cfg.workflow.output_dir = str(ws / "out")
    svc = ProcurementService(config=cfg, case_db=str(ws / "cases.sqlite"))
    web_api._service = svc  # 替换惰性单例
    from fastapi.testclient import TestClient

    yield TestClient(web_api.app)
    svc.close()
    web_api._service = None


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["data"]["ok"] is True


def test_submit_then_approve_end_to_end(client):
    # 1) 提交 -> 挂起
    r = client.post("/procurements", json={
        "request_text": _MISSING_BUDGET,
        "case_id": "PC-WEB-1",
        "auto_approve": False,
    })
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "needs_input"
    assert data["case_id"] == "PC-WEB-1"
    assert data["pending_approval"] is not None

    # 2) 查询（含产物）
    g = client.get("/procurements/PC-WEB-1")
    assert g.status_code == 200
    gd = g.json()["data"]
    assert gd["status"] == "needs_input"
    assert "strategy" in gd["artifacts"]

    # 3) 审批放行 -> done
    a = client.post("/procurements/PC-WEB-1/approve", json={
        "approved": True, "approver": "HTTP 审批人", "comment": "通过",
    })
    assert a.status_code == 200
    ad = a.json()["data"]
    assert ad["status"] == "done"
    assert any(x["decision"] == "approved" for x in ad["approvals"])


def test_approve_reject_blocks(client):
    client.post("/procurements", json={
        "request_text": _MISSING_BUDGET, "case_id": "PC-WEB-REJ",
    })
    r = client.post("/procurements/PC-WEB-REJ/approve", json={"approved": False})
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "blocked"


def test_unknown_case_404(client):
    assert client.get("/procurements/PC-NOPE").json()["code"] == 404
    r = client.post("/procurements/PC-NOPE/approve", json={"approved": True})
    assert r.json()["code"] == 404


def test_approve_non_pending_conflict(client):
    """未挂起（不存在）的 case 审批 -> 409（case 存在但已完成时）。"""
    client.post("/procurements", json={
        "request_text": _MISSING_BUDGET, "case_id": "PC-WEB-DONE",
        "auto_approve": True,
    })
    r = client.post("/procurements/PC-WEB-DONE/approve", json={"approved": True})
    assert r.json()["code"] in (404, 409)  # done 状态已无挂起点


def test_list_cases(client):
    client.post("/procurements", json={"request_text": _MISSING_BUDGET, "case_id": "PC-WEB-L1"})
    client.post("/procurements", json={"request_text": _MISSING_BUDGET, "case_id": "PC-WEB-L2"})
    r = client.get("/procurements")
    ids = {c["case_id"] for c in r.json()["data"]}
    assert {"PC-WEB-L1", "PC-WEB-L2"} <= ids
