"""P2-A 真 HITL 审计回归测试。

验证点（对照 ROADMAP P2「真 HITL」）：
  1. 审批记录为独立审计单元：approval_id / 审批人身份(approver_id) / 时间 / 意见 / 附件(含 sha256)；
  2. 决策走服务端接口（service.resume / POST approve），携带附件元数据全链路透传；
  3. 审计行幂等：同一 approval_id 重复同步不重复、不覆盖（append-only，不可篡改）；
  4. 旧库(v0.2.0 schema)打开时自动 ALTER 升级，历史审批行补 approval_id。
"""
from __future__ import annotations

import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from procurement_agents.config import AppConfig
from procurement_agents.service import ProcurementService
from procurement_agents.store import CaseStore

_MISSING_BUDGET = "紧急采购 10 台工业级交换机，具体预算与交期待定。"

_ATTACH = {
    "filename": "询价依据-供应商报价单.pdf",
    "content_type": "application/pdf",
    "size": 204800,
    "sha256": "ab" * 32,
    "note": "预算外采购依据",
}


@pytest.fixture()
def ws():
    root = Path(__file__).resolve().parents[1] / ".tmp" / "hitl"
    root.mkdir(parents=True, exist_ok=True)
    d = root / f"ws-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _cfg(ws: Path, **over) -> AppConfig:
    cfg = AppConfig.load()
    cfg.llm.provider = "mock"
    cfg.workflow.auto_approve_holds = False
    cfg.workflow.output_dir = str(ws / "out")
    for k, v in over.items():
        setattr(cfg.workflow, k, v)
    return cfg


def test_approval_audit_unit_with_attachments(ws):
    """审批记录含完整审计要素：身份/时间/意见/附件(sha256)/来源。"""
    cfg = _cfg(ws)
    with ProcurementService(config=cfg, case_db=str(ws / "a.sqlite")) as svc:
        svc.submit(_MISSING_BUDGET, case_id="PC-HITL-1")
        view = svc.resume(
            "PC-HITL-1",
            approved=True,
            approver="王经理",
            approver_id="wangjl",
            comment="预算内批准，附询价依据",
            attachments=[_ATTACH],
        )
        assert view["status"] == "done"
        recs = view["approvals"]
        human = [r for r in recs if r["decision"] == "approved"]
        assert len(human) == 1
        r = human[0]
        # 审计要素
        assert r["approval_id"]                     # 独立审计主键
        assert r["approver_id"] == "wangjl"
        assert r["approver"] == "王经理"
        assert r["comment"] == "预算内批准，附询价依据"
        assert r["created_at"]                      # 决策时间
        assert r["source"] == "api"
        # 附件元数据
        assert len(r["attachments"]) == 1
        at = r["attachments"][0]
        assert at["filename"] == _ATTACH["filename"]
        assert at["sha256"] == _ATTACH["sha256"]


def test_approval_rows_are_append_only_not_overwritten(ws):
    """审计行 append-only：同一 case 多次同步不会重复插入或覆盖历史行。"""
    cfg = _cfg(ws)
    db = str(ws / "b.sqlite")
    with ProcurementService(config=cfg, case_db=db) as svc:
        svc.submit(_MISSING_BUDGET, case_id="PC-HITL-2")
        v1 = svc.resume("PC-HITL-2", approved=True, approver="李总", approver_id="liz",
                        comment="放行", attachments=[_ATTACH])
        first_ids = {r["approval_id"] for r in v1["approvals"] if r["approval_id"]}
        assert first_ids

        # 模拟再次同步（幂等：不重复、不覆盖已有审计行）
        run = svc.runner.get_state("PC-HITL-2")
        svc.store.replace_approvals("PC-HITL-2", run.get("approvals") or [])
        second = svc.view("PC-HITL-2")["approvals"]
        second_ids = {r["approval_id"] for r in second if r["approval_id"]}
        assert second_ids == first_ids                      # 行数不膨胀
        approved = [r for r in second if r["decision"] == "approved"]
        assert len(approved) == 1
        assert approved[0]["comment"] == "放行"             # 未被改写


def test_rejected_approval_records_source_and_comment(ws):
    """拒绝决策同样完整审计（source=api + 拒绝意见 + 审批人）。"""
    cfg = _cfg(ws)
    with ProcurementService(config=cfg, case_db=str(ws / "c.sqlite")) as svc:
        svc.submit(_MISSING_BUDGET, case_id="PC-HITL-REJ")
        view = svc.resume("PC-HITL-REJ", approved=False, approver="合规部", approver_id="hg",
                          comment="证据不足，驳回")
        assert view["status"] == "blocked"
        rej = [r for r in view["approvals"] if r["decision"] == "rejected"]
        assert len(rej) == 1 and rej[0]["approver_id"] == "hg"
        assert rej[0]["source"] == "api"


def test_auto_approval_is_recorded_as_auto_source(ws):
    """auto_approve 放行记录决策来源=auto（非人工审计）。"""
    cfg = _cfg(ws)
    with ProcurementService(config=cfg, case_db=str(ws / "d.sqlite")) as svc:
        svc.submit(_MISSING_BUDGET, case_id="PC-HITL-AUTO", auto_approve=True)
        view = svc.view("PC-HITL-AUTO")
        assert view["status"] == "done"
        auto = [r for r in view["approvals"] if r["decision"] == "auto_approved"]
        assert auto and auto[0]["source"] == "auto"
        assert auto[0]["approval_id"]


def test_legacy_db_auto_upgrades_schema(ws):
    """v0.2.0 旧 schema 库（approvals 无新列）打开时自动升级并回填 approval_id。"""
    db = Path(ws) / "legacy.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE cases (
            case_id TEXT PRIMARY KEY, request_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
            meta TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
            phase TEXT NOT NULL, decision TEXT NOT NULL, approver TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        INSERT INTO approvals(case_id, phase, decision, approver, comment, created_at)
        VALUES ('PC-OLD-1', '人工审批', 'approved', '老审批人', '旧意见', '2026-01-01T10:00:00');
    """)
    conn.commit()
    conn.close()

    with CaseStore(str(db)) as store:
        rows = store.list_approvals("PC-OLD-1")
        assert len(rows) == 1
        r = rows[0]
        assert r["approval_id"].startswith("legacy-")       # 旧行回填审计主键
        assert r["approver"] == "老审批人"
        assert r["attachments"] == []                        # 新列默认空
        assert r["source"] == "api"


def test_submit_records_buyer_identity(ws):
    """提交记录发起人身份（buyer_id），供审计/RBAC 追溯。"""
    cfg = _cfg(ws)
    with ProcurementService(config=cfg, case_db=str(ws / "e.sqlite")) as svc:
        view = svc.submit(_MISSING_BUDGET, case_id="PC-HITL-BUY", buyer="采购中心-张三",
                          buyer_id="zhangsan")
        assert view["buyer"] == "采购中心-张三"
        assert view["buyer_id"] == "zhangsan"
