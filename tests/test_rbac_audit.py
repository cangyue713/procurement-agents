"""P2-B RBAC + 操作审计日志回归测试。

验证点（对照 ROADMAP P2「权限与审计」）：
  1. 角色权限矩阵：采购(buyer)/审批(approver)/合规(compliance)/管理(admin)/审计(auditor)；
  2. service 层动作审计：submit/approve 落 audit_logs（who/what/when/resource/result）；
  3. 越权与未知主体 -> AuthorizationError，且 denied 审计留痕；
  4. 系统内部调用（无 actor）默认全权限，动作以 __system__ 留痕可区分。
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from procurement_agents.access import (
    AuthorizationError,
    Permission,
    load_actor,
)
from procurement_agents.config import AppConfig
from procurement_agents.service import ProcurementService

_MISSING_BUDGET = "紧急采购 10 台工业级交换机，具体预算与交期待定。"


@pytest.fixture()
def ws():
    root = Path(__file__).resolve().parents[1] / ".tmp" / "rbac"
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


def test_role_permission_matrix():
    """角色 -> 权限矩阵符合内控口径。"""
    buyer = load_actor("zhangsan")
    approver = load_actor("wangjl")
    compliance = load_actor("hg")
    admin = load_actor("liz")
    auditor = load_actor("chensh")

    assert buyer is not None and buyer.has(Permission.PROCUREMENT_SUBMIT)
    assert not buyer.has(Permission.PROCUREMENT_APPROVE)

    assert approver.has(Permission.PROCUREMENT_APPROVE)
    assert approver.has(Permission.PROCUREMENT_SUBMIT)     # 审批人亦可发起

    assert compliance.has(Permission.PROCUREMENT_VIEW)
    assert not compliance.has(Permission.PROCUREMENT_SUBMIT)
    assert not compliance.has(Permission.AUDIT_VIEW)

    assert admin.has(Permission.MASTERDATA_CHANGE)
    assert admin.has(Permission.RULES_CHANGE)
    assert admin.has(Permission.AUDIT_VIEW)

    assert auditor.has(Permission.AUDIT_VIEW)
    assert auditor.has(Permission.PROCUREMENT_VIEW)
    assert not auditor.has(Permission.PROCUREMENT_APPROVE)


def test_unknown_user_rejected_with_denied_audit(ws):
    """未收录主体 -> AuthorizationError + denied 审计。"""
    with _svc(ws, "u.sqlite") as svc:
        with pytest.raises(AuthorizationError):
            svc.submit(_MISSING_BUDGET, case_id="PC-RBAC-UNK", actor="hacker")
        rows = svc.store.list_audit(actor_id="hacker")
        assert any(r["action"] == "procurement.submit" and r["result"] == "denied" for r in rows)


def test_role_without_permission_denied(ws):
    """合规岗不能提交采购 -> denied 审计含权限名。"""
    with _svc(ws, "d.sqlite") as svc:
        with pytest.raises(AuthorizationError):
            svc.submit(_MISSING_BUDGET, case_id="PC-RBAC-HG", actor="hg")
        denied = [r for r in svc.store.list_audit(actor_id="hg")
                  if r["result"] == "denied"]
        assert denied and denied[0]["detail"].get("permission") == "procurement.submit"


def test_submit_and_approve_audit_trail(ws):
    """完整链路审计：buyer 提交 + approver 审批，audit_logs 按时间倒序可查。"""
    with _svc(ws, "t.sqlite") as svc:
        svc.submit(_MISSING_BUDGET, case_id="PC-RBAC-1", actor="zhangsan")
        svc.resume("PC-RBAC-1", approved=True, comment="同意", actor="wangjl")

        submits = svc.store.list_audit(action="procurement.submit", resource="PC-RBAC-1")
        assert len(submits) == 1
        assert submits[0]["actor_id"] == "zhangsan" and submits[0]["result"] == "ok"

        approves = svc.store.list_audit(action="procurement.approve", resource="PC-RBAC-1")
        assert len(approves) == 1
        assert approves[0]["actor_id"] == "wangjl"
        assert approves[0]["detail"]["approved"] is True

        # who/what/when 齐备
        row = approves[0]
        assert row["created_at"] and row["action"] and row["actor_id"]


def test_approver_identity_bound_to_actor(ws):
    """审批人身份绑定会话主体：service 显式 actor 时 approver_id 取 actor。"""
    with _svc(ws, "a.sqlite") as svc:
        svc.submit(_MISSING_BUDGET, case_id="PC-RBAC-ID", actor="zhangsan")
        view = svc.resume("PC-RBAC-ID", approved=True, comment="ok", actor="wangjl")
        human = [r for r in view["approvals"] if r["decision"] == "approved"]
        assert human[0]["approver_id"] == "wangjl"


def test_system_actor_has_full_permissions_and_is_traced(ws):
    """内部调用（无 actor）仍成功，但以 __system__ 身份留审计（可区分人工）。"""
    with _svc(ws, "s.sqlite") as svc:
        svc.submit(_MISSING_BUDGET, case_id="PC-RBAC-SYS")
        rows = svc.store.list_audit(action="procurement.submit", resource="PC-RBAC-SYS")
        assert rows and rows[0]["actor_id"] == "__system__"


def test_resume_without_pending_still_authorized_then_valueerror(ws):
    """未挂起 case：先过权限校验（approver 可通过），再因状态拒绝（409 语义）。"""
    with _svc(ws, "v.sqlite") as svc:
        svc.submit(_MISSING_BUDGET, case_id="PC-RBAC-V", actor="zhangsan")
        svc.resume("PC-RBAC-V", approved=True, actor="wangjl")   # -> done
        with pytest.raises(ValueError, match="不处于待审批状态"):
            svc.resume("PC-RBAC-V", approved=True, actor="wangjl")
