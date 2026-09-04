"""P1 服务化回归测试：挂起等待/审批续跑/拒绝阻断/跨进程重启恢复/并发隔离。

不依赖 FastAPI —— 直接驱动 ProcurementService（checkpointer 为 SqliteSaver），
验证 P1.3（进程重启可恢复）+ P1.4（决策走 DB、多 case 隔离、无进程级注册表）。
"""
from __future__ import annotations

import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from procurement_agents.config import AppConfig
from procurement_agents.service import ProcurementService

_MISSING_BUDGET = "紧急采购 10 台工业级交换机，具体预算与交期待定。"


@pytest.fixture()
def ws():
    """工作区内独立工作目录（沙箱系统 Temp 可能不可写）。

    不在 pytest 的 basetemp 下建（其 sessionfinish 清理会再碰沙箱限制），
    而是放在项目 .tmp 下自管理，测试结束尽力清理。
    """
    root = Path(__file__).resolve().parents[1] / ".tmp" / "svc"
    root.mkdir(parents=True, exist_ok=True)
    d = root / f"ws-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _cfg_with_tmp_db(ws: Path) -> AppConfig:
    cfg = AppConfig.load()
    cfg.llm.provider = "mock"
    cfg.workflow.auto_approve_holds = False  # 服务化默认：hold 挂起等待人工
    cfg.workflow.output_dir = str(ws / "out")
    return cfg


def test_submit_halts_and_approve_resumes(ws):
    """提交(缺预算) -> needs_input；人工批准 -> 跑完 done。"""
    cfg = _cfg_with_tmp_db(ws)
    with ProcurementService(config=cfg, case_db=str(ws / "cases.sqlite")) as svc:
        view = svc.submit(_MISSING_BUDGET, case_id="PC-SVC-1")
        assert view["status"] == "needs_input"
        pending = view["pending_approval"]
        assert pending is not None and "审批" in pending["summary"]
        # 决策落库审计（初始无人工审批记录）
        assert view["approvals"] == []

        view2 = svc.resume("PC-SVC-1", approved=True, approver="王经理", comment="预算内批准")
        assert view2["status"] == "done"
        decisions = [a["decision"] for a in view2["approvals"]]
        assert "approved" in decisions
        assert any(a["approver"] == "王经理" for a in view2["approvals"])


def test_reject_blocks_and_records(ws):
    """人工拒绝 -> 流程阻断并在 DB 留痕。"""
    cfg = _cfg_with_tmp_db(ws)
    with ProcurementService(config=cfg, case_db=str(ws / "cases.sqlite")) as svc:
        svc.submit(_MISSING_BUDGET, case_id="PC-SVC-REJ")
        view = svc.resume("PC-SVC-REJ", approved=False, approver="李总", comment="预算不明，驳回")
        assert view["status"] == "blocked"
        assert any("拒绝" in i.get("message", "") for i in view["issues"])
        assert any(a["decision"] == "rejected" and a["approver"] == "李总" for a in view["approvals"])


def test_resume_without_pending_raises(ws):
    """已完成/不处于待审批的 case 再次 resume 应拒绝。"""
    cfg = _cfg_with_tmp_db(ws)
    with ProcurementService(config=cfg, case_db=str(ws / "cases.sqlite")) as svc:
        svc.submit(_MISSING_BUDGET, case_id="PC-SVC-NOP")
        svc.resume("PC-SVC-NOP", approved=True)          # 放行 -> done
        with pytest.raises(ValueError, match="不处于待审批状态"):
            svc.resume("PC-SVC-NOP", approved=True)      # done 后无挂起


def test_case_registry_and_listing(ws):
    cfg = _cfg_with_tmp_db(ws)
    with ProcurementService(config=cfg, case_db=str(ws / "cases.sqlite")) as svc:
        svc.submit(_MISSING_BUDGET, case_id="PC-SVC-L1")
        svc.submit(_MISSING_BUDGET, case_id="PC-SVC-L2")
        rows = svc.list_cases()
        ids = {r["case_id"] for r in rows}
        assert {"PC-SVC-L1", "PC-SVC-L2"} <= ids
        assert all(r["status"] == "needs_input" for r in rows if r["case_id"].startswith("PC-SVC-L"))


def test_restart_recovers_pending_case(ws):
    """P1.3：关闭服务后重建（模拟进程重启），挂起 case 仍可查询并续跑。"""
    db = str(ws / "restart.sqlite")
    cfg = _cfg_with_tmp_db(ws)
    svc1 = ProcurementService(config=cfg, case_db=db)
    v1 = svc1.submit(_MISSING_BUDGET, case_id="PC-RESTART-1")
    assert v1["status"] == "needs_input"
    svc1.close()

    # 新进程/新实例：同一 sqlite 文件
    svc2 = ProcurementService(config=cfg, case_db=db)
    try:
        view = svc2.view("PC-RESTART-1")
        assert view["status"] == "needs_input"
        resumed = svc2.resume("PC-RESTART-1", approved=True, approver="跨进程审批人")
        assert resumed["status"] == "done"
    finally:
        svc2.close()


def test_auto_approve_still_runs_through(ws):
    """auto_approve=True 时保持全自动（与旧语义兼容）。"""
    cfg = AppConfig.load()
    cfg.llm.provider = "mock"
    cfg.workflow.auto_approve_holds = True
    cfg.workflow.output_dir = str(ws / "out")
    from procurement_agents.runner import ProcurementRunner

    with ProcurementRunner(cfg) as runner:
        result = runner.run(_MISSING_BUDGET, case_id="PC-AUTO-1")
        assert result.is_done
        assert any(a.get("decision") == "auto_approved" for a in result.state.get("approvals", []))


def test_concurrent_cases_isolated(ws):
    """P1.4：多 case 并发提交 + 各自独立审批决策，状态互不串扰。"""
    db = str(ws / "conc.sqlite")
    cfg = _cfg_with_tmp_db(ws)
    results: list = []
    lock = threading.Lock()  # sqlite 写需串行化（服务为单写者），决策/断点彼此隔离

    def worker(i: int):
        with ProcurementService(config=cfg, case_db=db) as svc:
            cid = f"PC-CONC-{i}"
            with lock:
                first = svc.submit(_MISSING_BUDGET, case_id=cid)
                second = svc.resume(cid, approved=(i % 2 == 0), approver=f"审批人{i}")
            return cid, first["status"], second["status"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        for r in pool.map(worker, range(4)):
            results.append(r)

    by_id = {cid: (s1, s2) for cid, s1, s2 in results}
    for i in range(4):
        cid = f"PC-CONC-{i}"
        assert by_id[cid][0] == "needs_input"          # 各自先挂起
        assert by_id[cid][1] == ("done" if i % 2 == 0 else "blocked")  # 偶数放行/奇数拒绝
    assert len(by_id) == 4
