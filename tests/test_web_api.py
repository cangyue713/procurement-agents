"""P1.2 FastAPI 服务化回归测试 + P2-B RBAC 端点语义。

三端点全链路（HTTP 层 + TestClient）；P2-B 起业务请求必须携带 X-Actor-Id 身份头。
"""
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

# 身份头（用户目录 config/access.yaml）：zhangsan=采购员(buyer)，wangjl=审批人(approver)
_BUYER = {"X-Actor-Id": "zhangsan"}
_APPROVER = {"X-Actor-Id": "wangjl"}
_ADMIN = {"X-Actor-Id": "liz"}
_AUDITOR = {"X-Actor-Id": "chensh"}


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
    # 1) 采购员提交 -> 挂起
    r = client.post("/procurements", json={
        "request_text": _MISSING_BUDGET,
        "case_id": "PC-WEB-1",
        "auto_approve": False,
    }, headers=_BUYER)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "needs_input"
    assert data["case_id"] == "PC-WEB-1"
    assert data["pending_approval"] is not None
    assert data["buyer_id"] == "zhangsan"       # 发起人=请求身份

    # 2) 查询（含产物）
    g = client.get("/procurements/PC-WEB-1", headers=_BUYER)
    assert g.status_code == 200
    gd = g.json()["data"]
    assert gd["status"] == "needs_input"
    assert "strategy" in gd["artifacts"]

    # 3) 审批人放行 -> done（审批人身份取自请求头，非请求体伪造）
    a = client.post("/procurements/PC-WEB-1/approve", json={
        "approved": True, "comment": "通过",
    }, headers=_APPROVER)
    assert a.status_code == 200
    ad = a.json()["data"]
    assert ad["status"] == "done"
    approved = [x for x in ad["approvals"] if x["decision"] == "approved"]
    assert len(approved) == 1
    assert approved[0]["approver_id"] == "wangjl"       # 审计身份=审批会话主体
    assert approved[0]["approver"] == "王经理"


def test_approve_reject_blocks(client):
    client.post("/procurements", json={
        "request_text": _MISSING_BUDGET, "case_id": "PC-WEB-REJ",
    }, headers=_BUYER)
    r = client.post("/procurements/PC-WEB-REJ/approve", json={"approved": False},
                    headers=_APPROVER)
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "blocked"


def test_unknown_case_404(client):
    assert client.get("/procurements/PC-NOPE", headers=_BUYER).json()["code"] == 404
    r = client.post("/procurements/PC-NOPE/approve", json={"approved": True}, headers=_APPROVER)
    assert r.json()["code"] == 404


def test_approve_non_pending_conflict(client):
    """未挂起（不存在）的 case 审批 -> 409（case 存在但已完成时）。"""
    client.post("/procurements", json={
        "request_text": _MISSING_BUDGET, "case_id": "PC-WEB-DONE",
        "auto_approve": True,
    }, headers=_BUYER)
    r = client.post("/procurements/PC-WEB-DONE/approve", json={"approved": True},
                    headers=_APPROVER)
    assert r.json()["code"] in (404, 409)  # done 状态已无挂起点


def test_list_cases(client):
    client.post("/procurements", json={"request_text": _MISSING_BUDGET, "case_id": "PC-WEB-L1"},
                headers=_BUYER)
    client.post("/procurements", json={"request_text": _MISSING_BUDGET, "case_id": "PC-WEB-L2"},
                headers=_BUYER)
    r = client.get("/procurements", headers=_BUYER)
    ids = {c["case_id"] for c in r.json()["data"]}
    assert {"PC-WEB-L1", "PC-WEB-L2"} <= ids


def test_missing_identity_returns_401(client):
    """无身份头 -> body.code=401（匿名不可执行业务操作）。"""
    r = client.post("/procurements", json={"request_text": _MISSING_BUDGET})
    assert r.status_code == 200            # API 语义：HTTP 200 + body.code
    assert r.json()["code"] == 401
    assert client.get("/procurements", headers={}).json()["code"] == 401


def test_role_without_permission_returns_403(client):
    """合规岗(hg)无审批权/提交权 -> body.code=403；审计日志留 denied。"""
    compliance = {"X-Actor-Id": "hg"}
    r = client.post("/procurements", json={"request_text": _MISSING_BUDGET}, headers=compliance)
    assert r.json()["code"] == 403

    client.post("/procurements", json={"request_text": _MISSING_BUDGET, "case_id": "PC-WEB-403"},
                headers=_BUYER)
    r2 = client.post("/procurements/PC-WEB-403/approve", json={"approved": True},
                     headers=compliance)
    assert r2.json()["code"] == 403


def test_audit_endpoint_requires_audit_role(client):
    """审计日志端点：审计员可读，采购员 403。"""
    client.post("/procurements", json={"request_text": _MISSING_BUDGET, "case_id": "PC-AUD-1"},
                headers=_BUYER)
    # 触发一次越权拒绝（合规岗审批 -> denied），供审计员核查
    client.post("/procurements/PC-AUD-1/approve", json={"approved": True},
                headers={"X-Actor-Id": "hg"})

    ok = client.get("/audit", headers=_AUDITOR)
    rows = ok.json()["data"]
    assert any(r["action"] == "procurement.submit" and r["resource"] == "PC-AUD-1" for r in rows)
    assert any(r["action"] == "access.denied" for r in rows)   # denied 审计留痕

    denied = client.get("/audit", headers=_BUYER)
    assert denied.json()["code"] == 403
