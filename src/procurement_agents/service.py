"""ProcurementService：采购流程的编排服务（P1 服务化核心，不依赖 FastAPI）。

职责：
  * 提交需求 -> 同步执行到『完成 / 阻断 / 首个待人工审批挂起点』；
  * 查询 case（checkpointer 最新状态 + DB 元数据 + 审批决策历史）；
  * 审批决策（approved/rejected）落库（CaseStore）后驱动 runner.resume 续跑；
  * 进程重启恢复：runner 使用 SqliteSaver 持久化断点，store 记录元数据，
    重启后基于同一 sqlite 文件即可 get/resume。

设计约束：
  * 金额/状态全程 JSON 安全（Decimal 以字符串保留精度）；
  * 审批决策不再走进程级注册表，一律经 DB 记录 + checkpointer resume。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from procurement_agents.config import PROJECT_ROOT, AppConfig
from procurement_agents.runner import ProcurementRun, ProcurementRunner
from procurement_agents.store import CaseStore


def _resolve_path(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else PROJECT_ROOT / p


class ProcurementService:
    """采购流程服务：case 提交 / 查询 / 审批（同一进程内线程安全）。

    生产语义（P1.3）：runner 的 checkpointer 固定为 SqliteSaver 且与案例/审批
    元数据同库（同一 sqlite 文件），进程重启后可从断点恢复并续跑。
    仅当调用方显式传入 runner（如测试用 memory 断点）时例外。
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        case_db: str | Path | None = None,
        runner: ProcurementRunner | None = None,
    ) -> None:
        self.config = config or AppConfig.load()
        db = _resolve_path(str(case_db)) if case_db else _resolve_path(self.config.workflow.sqlite_path)
        # case/审批元数据 + LangGraph checkpoint 共用同一 sqlite 文件（表不同）
        self._store = CaseStore(db)
        self._case_db_path = db

        self._runner_owned = runner is None
        if runner is not None:
            self._runner = runner
        else:
            # 服务化固定 sqlite 断点（覆盖 memory/none 配置），保证跨进程可恢复
            cfg = self._with_sqlite_checkpointer(self.config, db)
            self._runner = ProcurementRunner(cfg)

    @staticmethod
    def _with_sqlite_checkpointer(config: AppConfig, db: Path) -> AppConfig:
        """返回一份 checkpointer=sqlite 的配置副本（不改动原配置对象）。"""
        import dataclasses

        wf = dataclasses.replace(config.workflow, checkpointer="sqlite", sqlite_path=str(db))
        return dataclasses.replace(config, workflow=wf)

    # ------------------------------------------------------------------
    def close(self) -> None:
        self._store.close()
        if self._runner_owned:
            self._runner.close()

    def __enter__(self) -> "ProcurementService":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def store(self) -> CaseStore:
        return self._store

    @property
    def runner(self) -> ProcurementRunner:
        return self._runner

    # ------------------------------------------------------------------
    # 提交 / 执行
    # ------------------------------------------------------------------
    def submit(
        self,
        request_text: str,
        case_id: Optional[str] = None,
        auto_approve: bool = False,
        buyer: Optional[str] = None,
    ) -> Dict[str, Any]:
        """提交采购需求并执行到第一个终态/挂起点，返回 case 视图。

        auto_approve=False（默认）：仲裁 hold 时停在审批点等待人工决策
        （status=needs_input，含 pending_approval 详情）。
        """
        case_id = case_id or f"PC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        self._store.register_case(case_id, request_text.strip(), meta={"buyer": buyer or ""})
        run = self._runner.run(request_text=request_text, case_id=case_id, auto_approve=auto_approve)
        self._sync_case_from_run(run)
        return self.view(case_id)

    def resume(
        self,
        case_id: str,
        approved: bool,
        approver: str = "人工(审批接口)",
        comment: str = "",
    ) -> Dict[str, Any]:
        """对挂起 case 提供审批决策并续跑（连续挂起可多次调用）。"""
        phase = self._pending_phase(case_id)
        if phase is None:
            raise ValueError(f"case {case_id} 当前不处于待审批状态")
        run = self._runner.resume(case_id, approved=approved, approver=approver, comment=comment)
        self._sync_case_from_run(run)
        return self.view(case_id)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def view(self, case_id: str) -> Dict[str, Any]:
        """case 完整视图：DB 元数据 + checkpointer 最新状态 + 审批历史。"""
        meta = self._store.get_case(case_id)
        if meta is None:
            raise KeyError(f"case 不存在: {case_id}")
        state = self._runner.get_state(case_id) or {}
        return {
            "case_id": case_id,
            "status": state.get("status") or meta.get("status", "unknown"),
            "summary": state.get("final_report", {}).get("summary", "") if state else meta.get("summary", ""),
            "created_at": meta.get("created_at", ""),
            "updated_at": state.get("meta", {}).get("finished_at", "") or meta.get("updated_at", ""),
            "pending_approval": self._extract_pending(state),
            "approvals": self._store.list_approvals(case_id),
            "artifacts": {
                "requirement": state.get("requirement"),
                "strategy": state.get("strategy"),
                "comparison": state.get("comparison"),
                "compliance": state.get("compliance"),
                "contract": state.get("contract"),
                "final_report": state.get("final_report"),
            },
            "issues": state.get("issues", []),
        }

    def list_cases(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._store.list_cases(limit=limit)
        out: List[Dict[str, Any]] = []
        for r in rows:
            state = self._runner.get_state(r["case_id"]) or {}
            out.append({
                "case_id": r["case_id"],
                "status": state.get("status") or r.get("status", "unknown"),
                "created_at": r.get("created_at", ""),
                "summary": (state.get("final_report", {}) or {}).get("summary", "") or r.get("summary", ""),
            })
        return out

    # ------------------------------------------------------------------
    def _sync_case_from_run(self, run: ProcurementRun) -> None:
        status = run.status
        summary = run.summary_text or ""
        self._store.update_status(run.case_id, status, summary)
        # 状态内 approvals（自动放行/人工审批，含时间）为权威，整组重建审计表
        self._store.replace_approvals(run.case_id, run.state.get("approvals") or [])

    def _pending_phase(self, case_id: str) -> Optional[str]:
        state = self._runner.get_state(case_id) or {}
        pending = self._extract_pending(state)
        return pending.get("phase") if pending else None

    @staticmethod
    def _extract_pending(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not state or state.get("status") != "needs_input":
            return None
        arb = state.get("arbitration") or []
        holds = [r for r in arb if r.get("verdict") == "hold"]
        if not holds:
            return None
        latest = holds[-1]
        return {
            "phase": latest.get("phase"),
            "summary": latest.get("summary"),
            "quality_score": latest.get("quality_score"),
            "checks": latest.get("checks", [])[:5],
        }
