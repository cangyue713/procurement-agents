"""案例与审批决策存储（P1 服务化：决策改走 DB）。

在 SqliteSaver（LangGraph 断点/状态库）之外，用独立 sqlite 表记录：
  * cases        —— 每个采购 case 的元数据与最新状态（供列表/查询/审计）；
  * approvals    —— 人工审批决策留痕（谁/何时/什么意见/决策，供审计追责）。

「决策改走 DB」：审批决策不再依赖进程级注册表（HUMAN_DECIDERS 已废弃），
而是落库后驱动 checkpointer 恢复续跑 —— 进程重启/多实例共享同一份数据。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id      TEXT PRIMARY KEY,
    request_text TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    meta         TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS approvals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    TEXT NOT NULL,
    phase      TEXT NOT NULL,
    decision   TEXT NOT NULL,          -- approved / rejected / auto_approved
    approver   TEXT NOT NULL,
    comment    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_case ON approvals(case_id);
"""


class CaseStore:
    """案例注册表 + 审批决策审计（sqlite，线程安全）。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "CaseStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # cases
    # ------------------------------------------------------------------
    def register_case(self, case_id: str, request_text: str, meta: Dict[str, Any] | None = None) -> None:
        """登记新 case（幂等：已存在则刷新请求文本与元数据）。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO cases(case_id, request_text, status, created_at, updated_at, meta) "
                "VALUES(?, ?, 'running', ?, ?, ?) "
                "ON CONFLICT(case_id) DO UPDATE SET request_text=excluded.request_text, "
                "updated_at=excluded.updated_at, meta=excluded.meta",
                (case_id, request_text, now, now, json.dumps(meta or {}, ensure_ascii=False)),
            )
            self._conn.commit()

    def update_status(self, case_id: str, status: str, summary: str = "") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "UPDATE cases SET status=?, summary=?, updated_at=? WHERE case_id=?",
                (status, summary, now, case_id),
            )
            self._conn.commit()

    def get_case(self, case_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT case_id, request_text, status, created_at, updated_at, summary, meta "
                "FROM cases WHERE case_id=?",
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["meta"] = json.loads(d.get("meta") or "{}")
        return d

    def list_cases(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT case_id, request_text, status, created_at, updated_at, summary "
                "FROM cases ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # approvals（审批决策留痕）
    # ------------------------------------------------------------------
    def log_approval(self, case_id: str, phase: str, decision: str,
                     approver: str, comment: str = "") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO approvals(case_id, phase, decision, approver, comment, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (case_id, phase, decision, approver, comment, now),
            )
            self._conn.commit()

    def replace_approvals(self, case_id: str, records: List[Dict[str, Any]]) -> None:
        """以状态内 approvals 为权威，整组重建该 case 的审计行（幂等）。"""
        with self._lock:
            self._conn.execute("DELETE FROM approvals WHERE case_id=?", (case_id,))
            for r in records:
                self._conn.execute(
                    "INSERT INTO approvals(case_id, phase, decision, approver, comment, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        case_id,
                        str(r.get("phase") or ""),
                        str(r.get("decision") or ""),
                        str(r.get("approver") or "系统"),
                        str(r.get("comment") or ""),
                        str(r.get("recorded_at") or r.get("created_at")
                            or datetime.now().isoformat(timespec="seconds")),
                    ),
                )
            self._conn.commit()

    def list_approvals(self, case_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT phase, decision, approver, comment, created_at "
                "FROM approvals WHERE case_id=? ORDER BY id",
                (case_id,),
            ).fetchall()
        return [dict(r) for r in rows]
