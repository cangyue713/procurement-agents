"""供应商知识库：主数据装载、评分与短名单生成。

数据资产：
  * knowledge/data/suppliers.csv —— 供应商主数据（表头字段即列名；
    多值字段 aliases/categories/certifications/risk_flags 以 | 分隔，UTF-8 编码，可容忍 BOM）
  * knowledge/data/blacklist.json —— 禁入名单
评分透明可审计：绩效 45% + 资质 25% + 成熟度 15% + 注册资金 15% - 风险扣分。
"""
from __future__ import annotations

import csv
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from procurement_agents.domain.models import (
    ExcludedSupplier,
    SupplierCandidate,
    SupplierShortlistArtifact,
)

logger = logging.getLogger(__name__)
DATA_DIR = Path(__file__).resolve().parent / "data"
SUPPLIERS_CSV = DATA_DIR / "suppliers.csv"

# 从需求句子里提取的认证编号（如 "ISO9001"）
_CERT_PATTERN = re.compile(r"ISO\s?[-]?\s?\d{2,5}|ISO9001|ISO14001|ISO27001|ISO45001|CMMI\d|CNAS|CMA")

# CSV 中的多值字段：单元格内以 | 分隔 -> 装载时拆成列表
_MULTI_FIELDS = ("aliases", "categories", "certifications", "risk_flags")
# CSV 中的数字字段（字符串 -> 数值，缺失为 0）
_INT_FIELDS = ("founded_year", "registered_capital")
_FLOAT_FIELDS = ("performance_rating",)


def _clean(value: Any) -> str:
    """清洗单元格：去空白；容错 Excel 常见全角空格。"""
    return str(value or "").strip().replace("\u3000", " ")


def _split_multi(value: Any) -> List[str]:
    """按 | 拆多值字段，忽略空项。"""
    return [part.strip() for part in _clean(value).split("|") if part.strip()]


def _csv_row_to_supplier(raw: Dict[str, str]) -> Dict[str, Any]:
    """CSV 行 -> 与旧 JSON 等价的供应商 dict（下游接口不变）。"""
    def v(key: str) -> str:
        return _clean(raw.get(key))

    supplier: Dict[str, Any] = {
        "id": v("id"),
        "name": v("name"),
        "short_name": v("short_name"),
        "aliases": _split_multi(raw.get("aliases")),
        "categories": _split_multi(raw.get("categories")),
        "contact": v("contact"),
        "certifications": _split_multi(raw.get("certifications")),
        "performance_notes": v("performance_notes"),
        "risk_level": v("risk_level") or "低",
        "risk_flags": _split_multi(raw.get("risk_flags")),
        # 库内价目与商务条款（价格与供应商介绍放同一张表）
        "price_items": v("price_items"),
        "payment_terms": v("payment_terms") or "货到验收合格后 30 天电汇",
        "delivery_days": _opt_int(v("delivery_days")),     # 空 -> None(未知交期)
        "warranty_months": _opt_int(v("warranty_months")) or 0,
    }
    for key in _INT_FIELDS:
        supplier[key] = int(float(v(key))) if v(key) else 0
    for key in _FLOAT_FIELDS:
        supplier[key] = float(v(key)) if v(key) else 0.0
    return supplier


def _opt_int(value: Any) -> Optional[int]:
    """空/非数字 -> None。"""
    try:
        return int(float(_clean(value)))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 库内价目工具（价格与供应商介绍在同一张 suppliers.csv 表中）
# --------------------------------------------------------------------------
def parse_price_items(price_items: str) -> Dict[str, float]:
    """解析价目单元格：`物品描述=单价` 多条以 | 分隔 -> {描述: 单价}。"""
    result: Dict[str, float] = {}
    if not price_items:
        return result
    for part in price_items.split("|"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        desc, _, price = part.partition("=")
        desc = desc.strip()
        try:
            result[desc] = float(price.strip().replace(",", ""))
        except (TypeError, ValueError):
            continue
    return result


def _common_hanzi(a: str, b: str, min_len: int = 2) -> bool:
    """a/b 是否存在长度 >= min_len 的连续公共汉字子串（用于跨词序匹配）。"""
    a_cn = re.sub(r"[^\u4e00-\u9fa5]", "", a)
    b_cn = re.sub(r"[^\u4e00-\u9fa5]", "", b)
    if len(a_cn) < min_len or len(b_cn) < min_len:
        return False
    # 取较短串的连续子串在较长串中查找
    short, long_ = (a_cn, b_cn) if len(a_cn) <= len(b_cn) else (b_cn, a_cn)
    for start in range(len(short) - min_len + 1):
        sub = short[start:start + min_len]
        if sub in long_:
            return True
    return False


def match_price(price_items: str, wanted_desc: str) -> Optional[float]:
    """在价目表中为需求行描述找到匹配单价。

    匹配策略（宽松但防误匹配）：
      1) 完全相等；
      2) 长描述包含短描述（>=4 字符）；
      3) 存在连续公共汉字子串（如『工业级交换机』~『48口万兆交换机』都含『交换机』）。
    """
    if not wanted_desc or not price_items:
        return None
    catalog = parse_price_items(price_items)
    wanted = wanted_desc.strip()
    if wanted in catalog:
        return catalog[wanted]
    # 双向包含匹配
    for desc, price in catalog.items():
        a, b = desc.strip(), wanted
        if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
            return price
    # 公共汉字子串兜底（容忍数量词/型号前缀差异，如 48口/2U/万兆 等）
    for desc, price in catalog.items():
        if _common_hanzi(desc, wanted, min_len=3):
            return price
    return None


def catalog_lines(
    price_items: str,
    req_items: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], float, List[str]]:
    """把供应商库内价目与需求行结合成"报价行明细"。

    返回: (lines, total, missing_desc)
      lines:   [{"description","quantity","unit_price","amount"}, ...] 需求行全覆盖时逐行计价
      total:   合计金额
      missing: 价目未能覆盖的需求行描述（该部分计 0，比价/合规据此预警）
    """
    lines: List[Dict[str, Any]] = []
    missing: List[str] = []
    total = 0.0
    for it in req_items:
        desc = str(it.get("description", "")).strip()
        qty = float(it.get("quantity", 0) or 0)
        price = match_price(price_items, desc)
        if not desc or qty <= 0 or price is None:
            if desc:
                missing.append(desc)
            continue
        amount = round(qty * price, 2)
        total += amount
        lines.append({
            "description": desc,
            "quantity": qty,
            "unit_price": price,
            "amount": amount,
        })
    return lines, round(total, 2), missing


@lru_cache(maxsize=2)
def load_suppliers() -> List[Dict[str, Any]]:
    """从 suppliers.csv 加载全部供应商主数据（Agent 读取数据的入口）。"""
    if not SUPPLIERS_CSV.exists():
        raise FileNotFoundError(
            f"供应商主数据缺失: {SUPPLIERS_CSV}。请按表头列名提供 UTF-8 CSV，"
            "多值字段(aliases/categories/certifications/risk_flags)用 | 分隔。"
        )
    rows: List[Dict[str, Any]] = []
    # utf-8-sig：容忍 Excel 另存 CSV 时的 BOM 头
    with open(SUPPLIERS_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"suppliers.csv 为空或无表头: {SUPPLIERS_CSV}")
        for raw in reader:
            if raw is None or not _clean(raw.get("id")):
                continue  # 跳过空行
            rows.append(_csv_row_to_supplier(raw))
    if not rows:
        raise ValueError(f"suppliers.csv 未解析到任何供应商: {SUPPLIERS_CSV}")
    return rows


def _load_json(name: str) -> Dict[str, Any]:
    with open(DATA_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=2)
def load_blacklist() -> List[Dict[str, Any]]:
    """加载禁入名单。"""
    return _load_json("blacklist.json").get("blacklist", [])


@lru_cache(maxsize=2)
def blacklist_ids() -> Dict[str, str]:
    """id -> 原因。"""
    return {b["id"]: b["reason"] for b in load_blacklist()}


@lru_cache(maxsize=2)
def blacklist_names() -> Dict[str, str]:
    """名称 -> 原因（黑名单企业可能不在主库）。"""
    return {b["name"]: b["reason"] for b in load_blacklist()}


@lru_cache(maxsize=2)
def supplier_registry() -> Dict[str, Dict[str, Any]]:
    """id -> 供应商信息（供报价文本识别供应商）。"""
    return {s["id"]: s for s in load_suppliers()}


@lru_cache(maxsize=2)
def supplier_by_name() -> Dict[str, str]:
    """名称(含简称/别名) -> id。"""
    mapping: Dict[str, str] = {}
    for s in load_suppliers():
        for key in (s["name"], s["short_name"], *s.get("aliases", [])):
            mapping[key] = s["id"]
    return mapping


# --------------------------------------------------------------------------
# 评分
# --------------------------------------------------------------------------
RISK_PENALTY = {"低": 0, "中": 8, "高": 20}


def score_supplier(info: Dict[str, Any], category: str) -> float:
    """计算某供应商对某品类的推荐分(0-100)。所有扣分均给出 reasons。"""
    perf = float(info.get("performance_rating", 3.0)) / 5.0 * 100
    certs = len(info.get("certifications", []))
    cert_score = min(100.0, 30 + certs * 20)

    age = max(0, 2025 - int(info.get("founded_year", 2020)))
    maturity = 100.0 if age >= 10 else (85.0 if age >= 5 else (70.0 if age >= 3 else 55.0))

    capital = float(info.get("registered_capital", 0))
    if capital >= 1e8:
        capital_score = 100.0
    elif capital >= 5e7:
        capital_score = 90.0
    elif capital >= 2e7:
        capital_score = 80.0
    elif capital >= 1e7:
        capital_score = 70.0
    elif capital >= 5e6:
        capital_score = 60.0
    else:
        capital_score = 40.0

    score = 0.45 * perf + 0.25 * cert_score + 0.15 * maturity + 0.15 * capital_score
    score -= RISK_PENALTY.get(str(info.get("risk_level", "低")), 0)
    return round(max(0.0, min(100.0, score)), 1)


def recommend_suppliers(
    category: str,
    size: int = 3,
    demand_hints: List[str] | None = None,
) -> SupplierShortlistArtifact:
    """按品类推荐候选并给出排除记录（黑名单 / 高风险 / 资质不满足）。"""
    demand_hints = demand_hints or []
    pool: List[Dict[str, Any]] = []
    excluded: List[ExcludedSupplier] = []
    blacklisted = blacklist_ids()

    for info in load_suppliers():
        if info["id"] in blacklisted:
            excluded.append(ExcludedSupplier(
                supplier_id=info["id"], name=info["name"], reason=f"禁入名单：{blacklisted[info['id']]}"
            ))
            continue
        if category not in info.get("categories", []):
            continue
        # 需求明确要求的认证编号若供应商不具备，直接排除并留痕
        req_certs = {m.group(0).replace(" ", "").upper() for h in demand_hints for m in _CERT_PATTERN.finditer(h)}
        have_certs = {c.replace(" ", "").upper() for c in info.get("certifications", [])}
        missing_certs = sorted(req_certs - have_certs)
        if missing_certs:
            excluded.append(ExcludedSupplier(
                supplier_id=info["id"], name=info["name"],
                reason=f"不满足需求资质要求：缺少 {'、'.join(missing_certs)}",
            ))
            continue
        if info.get("risk_level") == "高":
            excluded.append(ExcludedSupplier(
                supplier_id=info["id"], name=info["name"],
                reason="高风险供应商(如处罚/授权问题)不进候选池，详见风险标记",
            ))
            continue
        pool.append(info)

    ranked = sorted(pool, key=lambda s: score_supplier(s, category), reverse=True)
    candidates: List[SupplierCandidate] = []
    for info in ranked[:size]:
        reasons = [
            f"品类匹配:{category}",
            f"历史绩效 {info.get('performance_rating')}/5",
            f"资质: {'、'.join(info.get('certifications', []) or ['无'])}",
            f"风险等级:{info.get('risk_level')}",
        ]
        if info.get("risk_flags"):
            reasons.append("风险标记:" + ";".join(info["risk_flags"]))
        if not info.get("price_items"):
            reasons.append("注意：该供应商库内无价目，需补录后参与比价")
        candidates.append(SupplierCandidate(
            supplier_id=info["id"],
            name=info["name"],
            matched_category=category,
            score=score_supplier(info, category),
            reasons=reasons,
            risk_level=str(info.get("risk_level", "低")),
            certifications=info.get("certifications", []),
            performance_rating=float(info.get("performance_rating", 0)),
            contact=info.get("contact", ""),
            # 价格数据（与供应商介绍同源）
            price_items=info.get("price_items", ""),
            delivery_days=info.get("delivery_days"),
            warranty_months=int(info.get("warranty_months", 0) or 0),
            payment_terms=info.get("payment_terms") or "货到验收合格后 30 天电汇",
        ))

    return SupplierShortlistArtifact(category=category, candidates=candidates, excluded=excluded)
