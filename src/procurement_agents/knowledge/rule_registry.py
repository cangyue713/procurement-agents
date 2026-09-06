"""规则版本注册表（P2-D）：合规 C1~C6 / 采购策略 R1~R6 / 仲裁各阶段规则
按「版本 + 元数据」管理 —— 命中留痕可追溯、规则变更可登记回滚。

设计：
  * 每条规则为 RuleSpec（rule_id / 域 / 标签 / 说明 / 引入版本 / 变更历史）；
  * 规则集整体有 RULESET_VERSION（对齐发布节奏）与 ruleset_snapshot() 快照
    （版本 + 内容 hash），每次审查/裁决把所用版本写入产物与运行报告；
  * 规则实现仍是代码（compliance/strategy/arbitrator），但"版本登记"统一在此：
    变更规则语义 = 改实现 + bump 对应规则 version + 追加 RULESET_CHANGES ——
    老 case 的产物保留当时版本，可实现跨版本追溯与回滚定位（git revert 佐证）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

# 当前规则集版本（随规则变更 bump；历史 case 产物保留各自版本）
RULESET_VERSION = "2026.09-v1"

# 规则元数据注册表（登记即生效；供报告/审计展示与契约测试断言）
RuleSpec = Dict[str, str]

_COMPLIANCE_RULES: List[RuleSpec] = [
    {"rule_id": "C1", "label": "禁入名单检查", "domain": "compliance",
     "rule": "fail：黑名单/经营异常禁止成交", "version": "2026.09-v1"},
    {"rule_id": "C2", "label": "高风险供应商检查", "domain": "compliance",
     "rule": "fail：高风险；中风险 warn（专项审批）", "version": "2026.09-v1"},
    {"rule_id": "C3", "label": "交期满足性", "domain": "compliance",
     "rule": "fail：库内交期 > 需求交期*1.3；warn：> 需求交期", "version": "2026.09-v1"},
    {"rule_id": "C4", "label": "预算约束", "domain": "compliance",
     "rule": "fail：价目×数量计得总价 > 预算（Decimal 精度）", "version": "2026.09-v1"},
    {"rule_id": "C5", "label": "价目有效性", "domain": "compliance",
     "rule": "warn：库内无价目或价目未覆盖需求行", "version": "2026.09-v1"},
    {"rule_id": "C6", "label": "资质匹配", "domain": "compliance",
     "rule": "warn：需求要求的认证编号供应商缺失", "version": "2026.09-v1"},
]

_STRATEGY_RULES: List[RuleSpec] = [
    {"rule_id": "R1", "label": "单一来源特征", "domain": "strategy",
     "rule": "含独家/专利/唯一供应特征 -> 单一来源采购", "version": "2026.09-v1"},
    {"rule_id": "R2", "label": "预算缺失", "domain": "strategy",
     "rule": "无预算 -> 询比价 + 人工确认", "version": "2026.09-v1"},
    {"rule_id": "R3", "label": "小额直采", "domain": "strategy",
     "rule": "预算 <= direct_purchase_max -> 直接采购", "version": "2026.09-v1"},
    {"rule_id": "R4", "label": "招标门槛", "domain": "strategy",
     "rule": "预算 > tender_threshold -> 邀请招标(>=3家)", "version": "2026.09-v1"},
    {"rule_id": "R5", "label": "公开招标门槛", "domain": "strategy",
     "rule": "预算 > 5*tender_threshold 或法定情形 -> 公开招标", "version": "2026.09-v1"},
    {"rule_id": "R6", "label": "常规询比价", "domain": "strategy",
     "rule": "其余预算区间 -> 询比价", "version": "2026.09-v1"},
]

# 仲裁各阶段规则（监测点登记；实现见 agents/arbitrator.py）
_ARBITRATION_RULES: List[RuleSpec] = [
    {"rule_id": "ARB-REQ", "label": "需求阶段质检", "domain": "arbitration",
     "rule": "明细/数量/预算/交期完整性", "version": "2026.09-v1"},
    {"rule_id": "ARB-STR", "label": "策略阶段复核", "domain": "arbitration",
     "rule": "策略结果/审批要求/规则依据", "version": "2026.09-v1"},
    {"rule_id": "ARB-SUP", "label": "推荐阶段监控", "domain": "arbitration",
     "rule": "候选数量/低分/排除留痕/价目缺失与覆盖/招标家数", "version": "2026.09-v1"},
    {"rule_id": "ARB-CMP", "label": "比价阶段一致性", "domain": "arbitration",
     "rule": "比价基数/定标建议/高风险定标预警/非最低价复核", "version": "2026.09-v1"},
    {"rule_id": "ARB-CL", "label": "合规阶段复核", "domain": "arbitration",
     "rule": "可成交对象/否决依据/定标切换预警", "version": "2026.09-v1"},
    {"rule_id": "ARB-CT", "label": "合同阶段守门", "domain": "arbitration",
     "rule": "定标对象/预算红线/合规联动/文件产出/定标切换确认", "version": "2026.09-v1"},
    {"rule_id": "ARB-FIN", "label": "收官全程复核", "domain": "arbitration",
     "rule": "历史阻断复盘/放行水位/挂起处置确认", "version": "2026.09-v1"},
]

# 规则变更登记（可追溯/回滚依据：什么规则、什么版本、改了什么、何时）
RULESET_CHANGES: List[Dict[str, str]] = [
    {"version": "2026.09-v1", "at": "2026-09-04", "what": "初始发布（P0/P1 规则定版）",
     "affected": "C1~C6, R1~R6, ARB-*"},
]

RULES_BY_DOMAIN: Dict[str, List[RuleSpec]] = {
    "compliance": _COMPLIANCE_RULES,
    "strategy": _STRATEGY_RULES,
    "arbitration": _ARBITRATION_RULES,
}

# 扁平索引 rule_id -> spec
ALL_RULES: Dict[str, RuleSpec] = {
    spec["rule_id"]: spec
    for rules in RULES_BY_DOMAIN.values()
    for spec in rules
}


def ruleset_snapshot(domain: str | None = None) -> Dict[str, Any]:
    """规则集快照：版本 + 内容 hash + 规则清单（供产物/审计留痕）。"""
    domains = [domain] if domain else list(RULES_BY_DOMAIN)
    specs = [spec for d in domains for spec in RULES_BY_DOMAIN[d]]
    payload = json.dumps(sorted(specs, key=lambda s: s["rule_id"]), ensure_ascii=False,
                         sort_keys=True)
    return {
        "version": RULESET_VERSION,
        "hash": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
        "domains": domains,
        "rule_count": len(specs),
    }


def rule_spec(rule_id: str) -> RuleSpec:
    """按规则号取元数据（C1/R2/ARB-CL 等）；未登记返回占位。"""
    return ALL_RULES.get(rule_id, {"rule_id": rule_id, "label": rule_id,
                                   "domain": "unknown", "rule": "",
                                   "version": RULESET_VERSION})
