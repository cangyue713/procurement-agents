"""Mock 规则引擎：以确定性中文规则复刻 LLM 的『需求文本 -> 结构化』能力。

边界说明（有意为之）：
  * 面向格式化良好、一句一物的中文采购需求文本；
  * 更口语化/复杂写法请切换 openai_compat / deepseek Provider，
    由真实模型 + JSON 输出约束完成同样的任务 —— Agent 代码零改动。

注：价格数据不经过本引擎 —— 供应商库内价目由 suppliers.csv 直接提供（见 supplier_lib）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from procurement_agents.domain.enums import ProductCategory, UrgencyLevel

# --------------------------------------------------------------------------
# 中文数字/单位工具
# --------------------------------------------------------------------------
QTY_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([台套件个块吨批组张只根米桶箱罐袋瓶辆架支把])")

AMOUNT_WORD = {"万": 10_000, "w": 10_000, "W": 10_000, "千": 1_000}


def strip_numbers(text: str) -> str:
    """去掉文本中紧贴的数量(如 '40 台')，保留型号里的数字(如 2U、48口)。"""
    return QTY_UNIT_RE.sub("", text)


# --------------------------------------------------------------------------
# 需求解析
# --------------------------------------------------------------------------
URGENT_WORDS = ("紧急", "加急", "急需", "尽快", "紧急采购", "急用")
CRITICAL_WORDS = ("特急", "立即", "当日")

QUALITY_KEYWORDS = ("认证", "ISO", "质保", "保修", "标准", "要求", "规范", "检测", "验收", "须", "应")
USAGE_KEYWORDS = ("用于", "项目", "产线", "车间", "用途", "场景", "为满足", "配套")


def parse_requirement_text(text: str) -> Dict[str, Any]:
    """自由文本需求 -> RequirementArtifact 结构（JSON 安全 dict）。"""
    text = (text or "").strip()
    sentences = [s.strip() for s in re.split(r"[。；;\n]+", text) if s.strip()]

    # 1) 品类 / 紧急度
    category = ProductCategory.guess_from_text(text).value
    urgency = UrgencyLevel.NORMAL.value
    if any(k in text for k in CRITICAL_WORDS):
        urgency = UrgencyLevel.CRITICAL.value
    elif any(k in text for k in URGENT_WORDS):
        urgency = UrgencyLevel.URGENT.value

    # 2) 预算（支持：预算 280 万元 / 预算约 2,800,000 元 / 预算 300,000 元 千分位）
    budget: Optional[float] = None
    # 千分位金额：\d{1,3}(,\d{3})* 识别 "300,000"；再剥离逗号换算
    m = re.search(r"预算[^\d]{0,8}((?:\d{1,3}(?:,\d{3})*)(?:\.\d+)?)\s*(万|w|W|千)?", text)
    if m:
        amount = float(m.group(1).replace(",", ""))
        budget = amount * AMOUNT_WORD.get(m.group(2) or "", 1)

    # 3) 交付天数
    delivery_days: Optional[int] = None
    m = re.search(r"(\d{1,3})\s*天(?:内|之)?(?:交付|到货|交货|送达)?", text)
    if m:
        # 优先匹配 "X 天内交付 / 交付期 X 天" 形态，避免误抓质保天数
        for pat in (r"(\d{1,3})\s*天内?(?:交付|交货|到货)", r"交付[期时间]{0,2}[^\d]{0,4}(\d{1,3})\s*天"):
            mm = re.search(pat, text)
            if mm:
                delivery_days = int(mm.group(1))
                break
        if delivery_days is None:
            delivery_days = int(m.group(1))

    # 4) 明细行 + 质量要求 + 用途
    items: List[Dict[str, Any]] = []
    quality_reqs: List[str] = []
    usage_parts: List[str] = []

    for sentence in sentences:
        # 去掉句首引导词，便于识别“一行一物”
        core = re.sub(r"^(?:需|需要|采购|购买|求购|急?需采购|拟采购|采购需求[:：]?)", "", sentence).strip()
        hits = list(QTY_UNIT_RE.finditer(core))
        if hits:
            for i, hit in enumerate(hits):
                qty = float(hit.group(1))
                uom = hit.group(2)
                # 描述 = 该数量片段左右截取的文本（去掉其它数量段干扰）
                seg_start = hits[i - 1].end() if i > 0 else 0
                seg_end = hits[i + 1].start() if i + 1 < len(hits) else len(core)
                desc = core[seg_start:seg_end]
                desc = QTY_UNIT_RE.sub("", desc)
                desc = re.sub(r"[\s，,]+", " ", desc).strip(" -—")
                if not desc:
                    desc = f"物品{i + 1}"
                items.append({"description": desc, "quantity": qty, "uom": uom})
        else:
            # 无数量句：商务条款句归备注，其余按关键词归入质量要求 / 用途
            if any(k in sentence for k in ("付款", "支付", "电汇", "结算", "账期", "含税含运")):
                pass  # 商务条款句：留待 notes，不进质量要求
            elif any(k in sentence for k in QUALITY_KEYWORDS):
                quality_reqs.append(sentence[:120])
            if any(k in sentence for k in USAGE_KEYWORDS):
                usage_parts.append(sentence[:60])

    # 5) 标题（取首个有数量的句子，规整采购动词并去掉括号规格细节）
    title = ""
    for s in sentences:
        t = strip_numbers(s)
        t = re.sub(r"(?:急?需|拟|紧急)(?:采购|购买|求购)", "采购", t)
        t = re.sub(r"^(?:需|需要|拟)?(?:采购|购买|求购)[:：]?", "", t).strip(" ，,：:")
        t = t.split("（")[0].split("(")[0].strip()
        if t:
            t = t if "采购" in t else t + "采购"
            title = (t[:28] + "…") if len(t) > 28 else t
            break
    if not title:
        title = "未命名采购需求"

    missing: List[str] = []
    if not items:
        missing.append("明细行")
    if budget is None:
        missing.append("预算金额")
    if delivery_days is None:
        missing.append("交付期")

    return {
        "title": title,
        "category": category,
        "urgency": urgency,
        "items": items,
        "budget_amount": budget,
        "delivery_days": delivery_days,
        "quality_requirements": quality_reqs,
        "usage_scene": " ".join(usage_parts),
        "missing_fields": missing,
        "notes": text[:200],
    }


# --------------------------------------------------------------------------

