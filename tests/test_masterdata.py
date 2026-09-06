"""P2-C 供应商主数据升级回归测试。

验证点（对照 ROADMAP P2「供应商主数据升级」）：
  1. 价目 = 结构化行表 supplier_item × price（supplier_items.csv），
     load_suppliers 聚合生成兼容文本，逐条与行表一致；
  2. schema 校验：列名/描述/单价(正 Decimal)/供应商引用/物品不重复；
  3. 导入/变更审批工具：propose -> pending（含 old/new sha256 与快照）-> decide；
     批准落盘生效并留 .bak；拒绝不改文件；全程操作审计；
  4. RBAC：主数据变更仅 masterdata.change（admin）；审批状态不可重复处置。
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from procurement_agents.access import AuthorizationError
from procurement_agents.knowledge import supplier_lib as sl
from procurement_agents.masterdata import MasterDataError, MasterDataService
from procurement_agents.store import CaseStore

_ADMIN = "liz"
_BUYER = "zhangsan"


@pytest.fixture()
def ws():
    root = Path(__file__).resolve().parents[1] / ".tmp" / "mdc"
    root.mkdir(parents=True, exist_ok=True)
    d = root / f"ws-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def items_csv(ws) -> Path:
    """在临时工作区复制一份真实行表作为“现状文件”（避免污染仓库数据）。"""
    src = sl.SUPPLIER_ITEMS_CSV
    if not src.exists():
        pytest.skip("仓库无 supplier_items.csv")
    dst = Path(ws) / "supplier_items.csv"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


@pytest.fixture()
def svc(ws, items_csv):
    db = CaseStore(str(Path(ws) / "md.sqlite"))
    yield MasterDataService(db, items_csv_path=items_csv)
    db.close()


# ---------------------------------------------------------------------------
def test_rows_aggregate_into_price_items_text(ws):
    """行表逐行聚合 -> price_items 文本，价目完整且与行表逐字一致。"""
    items = sl.load_supplier_items()
    assert items and all(r["unit_price"] for r in items)
    sup = {s["id"]: s for s in sl.load_suppliers()}
    for sid in {r["supplier_id"] for r in items}:
        text = str(sup[sid].get("price_items") or "")
        parsed = sl.parse_price_items(text)
        rows = [r for r in items if r["supplier_id"] == sid]
        assert len(parsed) == len(rows), f"{sid} 行表行数 != 聚合条目数"
        for r in rows:
            from procurement_agents.domain.money import to_decimal
            assert parsed[r["item_description"]] == to_decimal(r["unit_price"])


def test_validate_supplier_items_passes_on_repo(ws):
    """仓库真实行表通过 schema 校验。"""
    assert sl.validate_supplier_items() == []


def _new_csv(supplier_id: str = "SUP-1001", price: str = "53000") -> str:
    """构造：华云 服务器调价 + 新增一行可售物品的合法新行表。"""
    rows = sl.load_supplier_items()
    lines = ["supplier_id,item_description,unit_price"]
    for r in rows:
        unit = price if (r["supplier_id"] == supplier_id
                         and "服务器" in r["item_description"]) else r["unit_price"]
        lines.append(f"{r['supplier_id']},{r['item_description']},{unit}")
    lines.append(f"{supplier_id},新物品-测试调价,999")
    return "\n".join(lines) + "\n"


def test_propose_import_creates_pending_change(svc, items_csv):
    new_csv = _new_csv()
    ch = svc.propose_import(_ADMIN, new_csv, reason="华云服务器调价")
    assert ch["status"] == "pending"
    assert ch["action"] == "import"
    assert ch["created_by"] == _ADMIN
    assert ch["new_sha256"] and ch["new_sha256"] != ch["old_sha256"]
    assert items_csv.read_text(encoding="utf-8") != new_csv   # 未生效
    audits = svc.store.list_audit(action="masterdata.change.requested")
    assert audits and audits[0]["resource"] == ch["change_id"]


def test_propose_rejects_invalid_csv(svc):
    bad_price = _new_csv(price="abc")                       # 单价非法
    with pytest.raises(MasterDataError, match="单价非法"):
        svc.propose_import(_ADMIN, bad_price)

    bad_col = "a,b,c\n1,2,3\n"                              # 缺列
    with pytest.raises(MasterDataError, match="缺列"):
        svc.propose_import(_ADMIN, bad_col)

    dup = "supplier_id,item_description,unit_price\nSUP-1001,交换机,10\nSUP-1001,交换机,11\n"
    with pytest.raises(MasterDataError, match="重复"):
        svc.propose_import(_ADMIN, dup)

    unk = "supplier_id,item_description,unit_price\nSUP-9999,物品,10\n"
    with pytest.raises(MasterDataError, match="不存在"):
        svc.propose_import(_ADMIN, unk)


def test_decide_approved_applies_and_backs_up(svc, items_csv):
    new_csv = _new_csv(price="53000")
    ch = svc.propose_import(_ADMIN, new_csv, reason="服务器调价")
    old_bytes = items_csv.read_bytes()

    out = svc.decide(_ADMIN, ch["change_id"], approved=True, comment="同意调价")
    assert out["status"] == "applied"
    assert items_csv.read_text(encoding="utf-8") == new_csv     # 生效
    # 备份存在且等于旧内容
    baks = sorted(items_csv.parent.glob("supplier_items.csv.bak-*"))
    assert baks and baks[-1].read_bytes() == old_bytes
    audits = svc.store.list_audit(action="masterdata.change.applied")
    assert audits and audits[0]["detail"]["old_sha256"] == ch["old_sha256"]


def test_decide_rejected_keeps_file(svc, items_csv):
    new_csv = _new_csv(price="53000")
    ch = svc.propose_import(_ADMIN, new_csv, reason="拟调价")
    before = items_csv.read_bytes()
    out = svc.decide(_ADMIN, ch["change_id"], approved=False, comment="暂不调价")
    assert out["status"] == "rejected"
    assert items_csv.read_bytes() == before                    # 未落盘
    assert svc.store.list_audit(action="masterdata.change.rejected")


def test_decide_twice_raises(svc):
    ch = svc.propose_import(_ADMIN, _new_csv(), reason="调价")
    svc.decide(_ADMIN, ch["change_id"], approved=True)
    with pytest.raises(MasterDataError, match="不可重复审批"):
        svc.decide(_ADMIN, ch["change_id"], approved=True)


def test_decide_unknown_change_raises(svc):
    with pytest.raises(KeyError):
        svc.decide(_ADMIN, "nope", approved=True)


def test_rbac_masterdata_requires_admin(svc):
    """非 admin（采购员）无 masterdata.change 权限 -> 拒绝。"""
    with pytest.raises(AuthorizationError):
        svc.propose_import(_BUYER, _new_csv(), reason="越权")
    ch = svc.propose_import(_ADMIN, _new_csv(), reason="调价")
    with pytest.raises(AuthorizationError):
        svc.decide(_BUYER, ch["change_id"], approved=True)
    denied = svc.store.list_audit(actor_id=_BUYER)
    assert any(r["result"] == "denied" for r in denied)


def test_list_changes_filter_by_status(svc):
    svc.propose_import(_ADMIN, _new_csv(), reason="r1")
    ch2 = svc.propose_import(_ADMIN, _new_csv(), reason="r2")
    svc.decide(_ADMIN, ch2["change_id"], approved=True)
    pend = svc.list_changes(status="pending")
    assert len(pend) == 1
    assert pend[0]["reason"] == "r1"
    assert len(svc.list_changes(status="applied")) == 1
