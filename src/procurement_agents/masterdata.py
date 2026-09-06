"""供应商主数据治理（P2-C）：schema 校验 + 导入/变更审批工具。

业务语义（对齐「数据资产可治理、变更可追溯」）：
  * 价目主数据 = supplier_items.csv 结构化行表 supplier_item × price
    （列：supplier_id, item_description, unit_price）；
  * 任何导入/变更先生成「变更单」(pending) —— 记录 old/new sha256 与完整快照；
  * 变更单须由具 masterdata.change 权限（admin）的主体审批：
      - 批准 -> 备份现文件为 .bak-<ts>，写入新行表，生效并清装载缓存；
      - 拒绝 -> 不落盘，仅留痕；
  * 全程写操作审计（masterdata.change.requested / .applied / .rejected）。

用法（服务化/脚本均可）：
    store = CaseStore("outputs/cases.sqlite")
    svc = MasterDataService(store)
    ch = svc.propose_import(actor="liz", csv_text=NEW_CSV, reason="供应商 X 价格调整")
    svc.decide(actor="liz", change_id=ch["change_id"], approved=True)
"""
from __future__ import annotations

import csv
import hashlib
import io
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from procurement_agents.access import (
    Actor,
    AuthorizationError,
    Permission,
    load_actor,
    require,
)
from procurement_agents.domain.money import to_decimal
from procurement_agents.knowledge.supplier_lib import (
    SUPPLIER_ITEMS_CSV,
    clear_master_cache,
    load_suppliers_raw,
)
from procurement_agents.store import CaseStore

_REQUIRED_COLS = ("supplier_id", "item_description", "unit_price")


class MasterDataError(ValueError):
    """主数据校验/状态错误（propose 校验失败或审批状态非法）。"""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_and_validate_items_csv(csv_text: str, known_supplier_ids: set[str]) -> List[Dict[str, str]]:
    """解析并逐行 schema 校验价目行表内容，返回清洗后的行列表。

    校验项：列名齐全 / 描述非空 / 单价为正数 Decimal / 供应商引用存在 /
    同供应商物品描述不重复。任何失败抛 MasterDataError（列出全部问题）。
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise MasterDataError("价目行表为空或无表头，需列: supplier_id,item_description,unit_price")
    missing = [c for c in _REQUIRED_COLS if c not in reader.fieldnames]
    if missing:
        raise MasterDataError(f"价目行表缺列: {'、'.join(missing)}")

    rows: List[Dict[str, str]] = []
    errors: List[str] = []
    seen: Dict[str, set] = {}
    for i, raw in enumerate(reader, start=2):
        sid = str(raw.get("supplier_id") or "").strip()
        desc = str(raw.get("item_description") or "").strip()
        price_raw = str(raw.get("unit_price") or "").strip()
        if not sid and not desc and not price_raw:
            continue  # 空行
        if sid not in known_supplier_ids:
            errors.append(f"第{i}行: 供应商 id 不存在于主库: {sid!r}")
        if not desc:
            errors.append(f"第{i}行: 物品描述为空")
        price = to_decimal(price_raw)
        if price is None or price <= 0:
            errors.append(f"第{i}行: 单价非法（须为正数）: {price_raw!r}")
        seen.setdefault(sid, set()).add(desc)
        rows.append({"supplier_id": sid, "item_description": desc, "unit_price": price_raw})
    for sid in seen:
        counts: Dict[str, int] = {}
        for r in rows:
            if r["supplier_id"] == sid:
                counts[r["item_description"]] = counts.get(r["item_description"], 0) + 1
        dup = [d for d, n in counts.items() if n > 1]
        if dup:
            errors.append(f"供应商 {sid} 物品重复: {'、'.join(dup)}")
    if errors:
        raise MasterDataError("价目行表校验失败：\n  - " + "\n  - ".join(errors[:20]))
    return rows


class MasterDataService:
    """供应商主数据变更审批服务（依赖 CaseStore 审计 + access RBAC）。"""

    def __init__(
        self,
        store: CaseStore,
        items_csv_path: str | Path | None = None,
    ) -> None:
        self._store = store
        self._items_path = Path(items_csv_path) if items_csv_path else SUPPLIER_ITEMS_CSV

    @property
    def store(self) -> CaseStore:
        return self._store

    @property
    def items_csv_path(self) -> Path:
        return self._items_path

    # ------------------------------------------------------------------
    def _require(self, actor_id: str, permission: Permission,
                 resource: str = "") -> Actor:
        """解析并授权主体；未收录/无权限抛 AuthorizationError（先留 denied 审计）。"""
        try:
            actor = load_actor(actor_id)
            if actor is None:
                raise AuthorizationError(actor_id or "(未提供)", permission.value)
            return require(actor, permission)
        except AuthorizationError as exc:
            self._store.log_action(
                actor_id or "(匿名)", "masterdata.change", resource, "denied",
                {"permission": exc.permission},
            )
            raise

    def _audit(self, actor: Actor, action: str, resource: str,
               result: str = "ok", detail: Dict[str, Any] | None = None) -> None:
        self._store.log_action(actor.user_id, action, resource, result, detail)

    # ------------------------------------------------------------------
    # 变更单生命周期
    # ------------------------------------------------------------------
    def propose_import(
        self,
        actor_id: str,
        csv_text: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        """提交价目行表导入/变更申请（先校验、再落 pending 变更单）。

        仅生成待审批变更，不写主数据文件 —— 生效须经 decide(approved)。
        """
        actor = self._require(actor_id, Permission.MASTERDATA_CHANGE)
        known = {s["id"] for s in load_suppliers_raw()}
        _parse_and_validate_items_csv(csv_text, known)   # 校验失败直接抛
        old_text = self._items_path.read_text(encoding="utf-8") if self._items_path.exists() else ""
        change_id = uuid.uuid4().hex
        change: Dict[str, Any] = {
            "change_id": change_id,
            "action": "import",
            "reason": reason,
            "old_sha256": _sha256_text(old_text) if old_text else "",
            "new_sha256": _sha256_text(csv_text),
            "csv_snapshot": csv_text,
            "status": "pending",
            "created_by": actor.user_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._store.save_masterdata_change(change)
        self._audit(actor, "masterdata.change.requested", change_id,
                    "ok", {"reason": reason, "new_sha256": change["new_sha256"]})
        return change

    def list_changes(self, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        return self._store.list_masterdata_changes(status=status, limit=limit)

    def decide(self, actor_id: str, change_id: str, approved: bool,
               comment: str = "") -> Dict[str, Any]:
        """审批变更单：批准 -> 备份 + 落盘生效；拒绝 -> 仅留痕。全程审计。"""
        actor = self._require(actor_id, Permission.MASTERDATA_CHANGE, change_id)
        change = self._store.get_masterdata_change(change_id)
        if change is None:
            raise KeyError(f"变更单不存在: {change_id}")
        if change["status"] != "pending":
            raise MasterDataError(f"变更单 {change_id} 已处置（status={change['status']}），不可重复审批")

        if approved:
            # 备份现文件后再写（回滚凭据：.bak-<ts> + old_sha256）
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            if self._items_path.exists():
                backup = self._items_path.with_name(f"{self._items_path.name}.bak-{ts}")
                backup.write_text(self._items_path.read_text(encoding="utf-8"), encoding="utf-8")
            self._items_path.parent.mkdir(parents=True, exist_ok=True)
            self._items_path.write_text(change["csv_snapshot"], encoding="utf-8")
            clear_master_cache()
            new_status = "applied"
            self._audit(actor, "masterdata.change.applied", change_id, "ok",
                        {"old_sha256": change["old_sha256"], "new_sha256": change["new_sha256"]})
        else:
            new_status = "rejected"
            self._audit(actor, "masterdata.change.rejected", change_id, "ok",
                        {"comment": comment})
        self._store.decide_masterdata_change(change_id, new_status,
                                             actor.user_id, comment)
        return self._store.get_masterdata_change(change_id) or change
