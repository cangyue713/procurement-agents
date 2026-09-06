"""案例与审批决策存储（P1 服务化：决策改走 DB；P2 审计升级：审批记录独立落库）。

在 SqliteSaver（LangGraph 断点/状态库）之外，用独立 sqlite 表记录：
  * cases        —— 每个采购 case 的元数据与最新状态（供列表/查询/审计）；
  * approvals    —— 人工审批决策留痕（P2 起为独立审计单元：
                    谁(approver_id/approver)/何时(recorded_at)/什么意见(comment)/
                    附件元数据(attachments, 含 sha256)/决策来源(source)）。

「决策改走 DB」：审批决策不再依赖进程级注册表（HUMAN_DECIDERS 已废弃），
而是落库后驱动 checkpointer 恢复续跑 —— 进程重启/多实例共享同一份数据。
旧库（v0.2.0 无附件列）打开时自动 ALTER 补列，向后兼容。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
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
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id  TEXT NOT NULL DEFAULT '',   -- 审计主键（P2 新增：审批记录唯一 ID）
    case_id      TEXT NOT NULL,
    phase        TEXT NOT NULL,
    decision     TEXT NOT NULL,              -- approved / rejected / auto_approved
    approver_id  TEXT NOT NULL DEFAULT '',   -- 审批人账号/工号（RBAC 身份）
    approver     TEXT NOT NULL,
    comment      TEXT NOT NULL DEFAULT '',
    attachments  TEXT NOT NULL DEFAULT '[]', -- 附件元数据 JSON（含 sha256）
    source       TEXT NOT NULL DEFAULT 'api',-- api / auto / resume
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_case ON approvals(case_id);
CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   TEXT NOT NULL,                -- 谁(who)
    action     TEXT NOT NULL,                -- 做了什么(what)：procurement.submit / approve ...
    resource   TEXT NOT NULL DEFAULT '',     -- 对象(case_id / 数据实体)
    result     TEXT NOT NULL DEFAULT 'ok',   -- ok / denied / error
    detail     TEXT NOT NULL DEFAULT '{}',   -- 补充(JSON：意见/被拒原因/结果状态)
    created_at TEXT NOT NULL                 -- 何时(when)
);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_logs(actor_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_logs(created_at);
CREATE TABLE IF NOT EXISTS masterdata_changes (
    change_id    TEXT PRIMARY KEY,
    action       TEXT NOT NULL,              -- import / update
    reason       TEXT NOT NULL DEFAULT '',
    old_sha256   TEXT NOT NULL DEFAULT '',   -- 变更前文件哈希
    new_sha256   TEXT NOT NULL DEFAULT '',   -- 变更后文件哈希
    csv_snapshot TEXT NOT NULL,              -- 新价目行表完整内容（待生效）
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending / applied / rejected
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    decided_by   TEXT NOT NULL DEFAULT '',
    decided_at   TEXT NOT NULL DEFAULT '',
    comment      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mdc_status ON masterdata_changes(status);
"""
# idx_approvals_uid 依赖 P2 新增列 approval_id，需在旧库升级(add column)之后创建，
# 因此从 _SCHEMA 移出，在 _ensure_approval_schema 内统一执行。

# v0.2.0 旧表缺的列 -> 打开时自动补（SQLite ADD COLUMN）
_APPROVAL_UPGRADE_COLS = {
    "approval_id": "TEXT NOT NULL DEFAULT ''",
    "approver_id": "TEXT NOT NULL DEFAULT ''",
    "attachments": "TEXT NOT NULL DEFAULT '[]'",
    "source": "TEXT NOT NULL DEFAULT 'api'",
}


def _ensure_approval_schema(conn: sqlite3.Connection) -> None:
    """把 v0.2.0 旧 approvals 表就地升级到 P2 schema（幂等）。"""
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(approvals)").fetchall()
    }
    for col, ddl in _APPROVAL_UPGRADE_COLS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE approvals ADD COLUMN {col} {ddl}")
    # 旧行回填 approval_id（审计主键不可为空）
    conn.execute(
        "UPDATE approvals SET approval_id = 'legacy-' || id WHERE approval_id = ''"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_uid ON approvals(approval_id)")
    conn.commit()


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
            _ensure_approval_schema(self._conn)

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

    def list_cases(self, limit: int = 50, plan_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出 case（可按批量计划 plan_id 过滤；meta.plan_id 见 submit(plan_id=)）。"""
        sql = ("SELECT case_id, request_text, status, created_at, updated_at, summary "
               "FROM cases WHERE 1=1")
        args: List[Any] = []
        if plan_id:
            sql += " AND json_extract(meta, '$.plan_id')=?"
            args.append(plan_id)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # approvals（审批决策留痕 —— P2 起为独立审计单元）
    # ------------------------------------------------------------------
    def log_approval(
        self,
        case_id: str,
        phase: str,
        decision: str,
        approver: str,
        comment: str = "",
        *,
        approval_id: Optional[str] = None,
        approver_id: str = "",
        attachments: Optional[List[Dict[str, Any]]] = None,
        source: str = "api",
    ) -> str:
        """追加一条审批审计记录，返回 approval_id。

        决策来源与附件元数据一并落库；approval_id 缺省自动生成，
        重复调用同 approval_id 时幂等跳过（审计行不重复、不覆盖）。
        """
        aid = approval_id or uuid.uuid4().hex
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            dup = self._conn.execute(
                "SELECT 1 FROM approvals WHERE approval_id=?", (aid,)
            ).fetchone()
            if dup is None:
                self._conn.execute(
                    "INSERT INTO approvals(approval_id, case_id, phase, decision, approver_id, "
                    "approver, comment, attachments, source, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        aid, case_id, phase, decision, approver_id, approver, comment,
                        json.dumps(attachments or [], ensure_ascii=False),
                        source, now,
                    ),
                )
                self._conn.commit()
        return aid

    def replace_approvals(self, case_id: str, records: List[Dict[str, Any]]) -> None:
        """以状态内 approvals 为权威同步审计表（幂等，按 approval_id 增量）。

        已存在的 approval_id 不重复插入、不覆盖（独立落库不可篡改）；
        缺失的（如历史库遗留）则补插，最终与状态对齐。
        """
        with self._lock:
            existing = {
                r["approval_id"]
                for r in self._conn.execute(
                    "SELECT approval_id FROM approvals WHERE case_id=? AND approval_id<>''",
                    (case_id,),
                ).fetchall()
            }
            for r in records:
                aid = str(r.get("approval_id") or "")
                if not aid:
                    aid = uuid.uuid4().hex
                if aid in existing:
                    continue  # 审计行已在库，不改写
                attachments = r.get("attachments") or []
                if not isinstance(attachments, list):
                    attachments = []
                self._conn.execute(
                    "INSERT INTO approvals(approval_id, case_id, phase, decision, approver_id, "
                    "approver, comment, attachments, source, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        aid,
                        case_id,
                        str(r.get("phase") or ""),
                        str(r.get("decision") or ""),
                        str(r.get("approver_id") or ""),
                        str(r.get("approver") or "系统"),
                        str(r.get("comment") or ""),
                        json.dumps(attachments, ensure_ascii=False),
                        str(r.get("source")
                            or ("auto" if str(r.get("decision")) == "auto_approved" else "api")),
                        str(r.get("recorded_at") or r.get("created_at")
                            or datetime.now().isoformat(timespec="seconds")),
                    ),
                )
            self._conn.commit()

    def list_approvals(self, case_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT approval_id, phase, decision, approver_id, approver, comment, "
                "attachments, source, created_at "
                "FROM approvals WHERE case_id=? ORDER BY id",
                (case_id,),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["attachments"] = json.loads(d.get("attachments") or "[]")
            except (TypeError, ValueError):
                d["attachments"] = []
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # audit_logs（操作审计：who / what / when / result）
    # ------------------------------------------------------------------
    def log_action(
        self,
        actor_id: str,
        action: str,
        resource: str = "",
        result: str = "ok",
        detail: Optional[Dict[str, Any]] = None,
        at: Optional[str] = None,
    ) -> None:
        """追加一条操作审计记录（who 做了什么 on what，结果如何）。"""
        now = at or datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_logs(actor_id, action, resource, result, detail, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    actor_id or "",
                    action,
                    resource,
                    result,
                    json.dumps(detail or {}, ensure_ascii=False),
                    now,
                ),
            )
            self._conn.commit()

    def list_audit(
        self,
        limit: int = 100,
        actor_id: Optional[str] = None,
        action: Optional[str] = None,
        resource: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """审计日志查询（时间倒序，可按主体/动作/对象过滤）。"""
        sql = ("SELECT actor_id, action, resource, result, detail, created_at "
               "FROM audit_logs WHERE 1=1")
        args: List[Any] = []
        if actor_id:
            sql += " AND actor_id=?"
            args.append(actor_id)
        if action:
            sql += " AND action=?"
            args.append(action)
        if resource:
            sql += " AND resource=?"
            args.append(resource)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(limit, 1000)))
        with self._lock:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d.get("detail") or "{}")
            except (TypeError, ValueError):
                d["detail"] = {}
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # masterdata_changes（主数据变更单：待审批生效，P2-C）
    # ------------------------------------------------------------------
    def save_masterdata_change(self, change: Dict[str, Any]) -> None:
        """保存变更单（pending）；同 change_id 重复保存幂等。"""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO masterdata_changes"
                "(change_id, action, reason, old_sha256, new_sha256, csv_snapshot, "
                " status, created_by, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(change["change_id"]), str(change.get("action") or "import"),
                    str(change.get("reason") or ""), str(change.get("old_sha256") or ""),
                    str(change.get("new_sha256") or ""), str(change.get("csv_snapshot") or ""),
                    str(change.get("status") or "pending"),
                    str(change.get("created_by") or ""), str(change.get("created_at") or ""),
                ),
            )
            self._conn.commit()

    def get_masterdata_change(self, change_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT change_id, action, reason, old_sha256, new_sha256, csv_snapshot, "
                "status, created_by, created_at, decided_by, decided_at, comment "
                "FROM masterdata_changes WHERE change_id=?",
                (change_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_masterdata_changes(self, status: Optional[str] = None,
                                limit: int = 50) -> List[Dict[str, Any]]:
        sql = ("SELECT change_id, action, reason, old_sha256, new_sha256, status, "
               "created_by, created_at, decided_by, decided_at, comment "
               "FROM masterdata_changes WHERE 1=1")
        args: List[Any] = []
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        with self._lock:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def decide_masterdata_change(self, change_id: str, status: str,
                                 decided_by: str, comment: str = "") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "UPDATE masterdata_changes SET status=?, decided_by=?, decided_at=?, comment=? "
                "WHERE change_id=?",
                (status, decided_by, now, comment, change_id),
            )
            self._conn.commit()
