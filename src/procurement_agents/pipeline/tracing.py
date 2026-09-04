"""运行追踪与报告：把一次采购全流程导出为 JSON trace 与可读 Markdown 报告。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from procurement_agents.domain.enums import VerdictAction


def save_trace(state: Dict[str, Any], output_dir: str | Path) -> Path:
    """保存全量运行追踪（JSON）。"""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    case_id = state.get("case_id", datetime.now().strftime("%Y%m%d-%H%M%S"))
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out / f"trace_{case_id}_{ts}.json"
    path.write_text(json.dumps(_orderly(state), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _orderly(state: Dict[str, Any]) -> Dict[str, Any]:
    order = ["case_id", "status", "current_phase", "meta", "requirement", "strategy",
             "shortlist", "comparison", "compliance", "contract",
             "final_report", "arbitration", "approvals", "issues", "phase_history", "input"]
    data = {k: state.get(k) for k in order if k in state}
    for k, v in state.items():
        if k not in data:
            data[k] = v
    return data


def render_markdown(state: Dict[str, Any]) -> str:
    """渲染中文 Markdown 运行报告（含全程仲裁裁决）。"""
    lines: List[str] = []
    report = state.get("final_report") or {}
    meta = state.get("meta") or {}
    arb = state.get("arbitration") or []

    lines.append(f"# 采购流程自动化运行报告　{state.get('case_id', '')}")
    lines.append("")
    lines.append(f"- 状态：**{state.get('status')}**")
    lines.append(f"- 模型 Provider：{meta.get('provider', '-')}　开始：{meta.get('started_at', '')[:19]}　结束：{meta.get('finished_at', '')[:19]}")
    lines.append("")
    lines.append("## 一、采购结论")
    lines.append("")
    lines.append(report.get("summary", ""))
    lines.append("")

    # 阶段执行记录
    lines.append("## 二、阶段执行记录")
    lines.append("")
    lines.append("| 阶段 | 启动时间 | 状态 | 重试 | 说明 |")
    lines.append("|---|---|---|---|---|")
    for h in state.get("phase_history") or []:
        lines.append(f"| {h.get('phase', '')} | {str(h.get('started_at', ''))[11:19]} | {h.get('status')} | {h.get('retries', 0)} | {h.get('error', '')[:60] or 'ok'} |")
    lines.append("")

    # 候选供应商及库内价目一览
    shortlist = state.get("shortlist") or {}
    candidates = shortlist.get("candidates") or []
    if candidates:
        lines.append("## 三、候选供应商（库内价目，来自 suppliers.csv）")
        lines.append("")
        lines.append("| 供应商 | 风险 | 绩效 | 交期(天) | 质保(月) | 价目概览 |")
        lines.append("|---|---|---|---|---|---|")
        for c in candidates:
            items = str(c.get("price_items") or "")
            preview = "；".join(items.split("|")[:3])[:60] if items else "(无价目)"
            lines.append(
                f"| {c.get('name')} | {c.get('risk_level')} | {c.get('performance_rating')} | "
                f"{c.get('delivery_days') or '-'} | {c.get('warranty_months', 0)} | {preview} |"
            )
        lines.append("")

    comparison = state.get("comparison") or {}
    rows = comparison.get("rows") or []
    if rows:
        lines.append("## 四、比价表")
        lines.append("")
        lines.append("| 名次 | 供应商 | 总额(元) | 交期(天) | 价格分 | 交期分 | 绩效分 | 综合分 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in sorted(rows, key=lambda x: x.get("rank", 99)):
            lines.append(f"| {r.get('rank')} | {r.get('supplier_name')} | {r.get('total_amount'):,} | {r.get('delivery_days') or '-'} | {r.get('price_score')} | {r.get('delivery_score')} | {r.get('perf_score')} | {r.get('total_score')} |")
        lines.append("")

    # 全程仲裁裁决
    lines.append("## 五、仲裁 Agent 全程裁决")
    lines.append("")
    if arb:
        lines.append("| 阶段 | 裁决 | 质量分 | 摘要 |")
        lines.append("|---|---|---|---|")
        for r in arb:
            v = r.get("verdict")
            icon = {"proceed": "放行", "hold": "挂起", "block": "阻断"}.get(v, v)
            lines.append(f"| {r.get('phase')} | {icon} | {r.get('quality_score')} | {r.get('summary')} |")
        lines.append("")
        # 详细检查
        lines.append("### 各阶段仲裁检查明细")
        lines.append("")
        for r in arb:
            checks = r.get("checks") or []
            if not checks:
                continue
            lines.append(f"**{r.get('phase')}**（{r.get('verdict')}）")
            for c in checks:
                lv = {"通过": "✅", "提示": "⚠️", "不通过": "⛔"}.get(c.get("level"), c.get("level"))
                lines.append(f"- {lv} {c.get('name')}：{c.get('message')}")
            lines.append("")
    else:
        lines.append("（无仲裁记录）")

    approvals = state.get("approvals") or []
    if approvals:
        lines.append("## 六、人工审批记录")
        lines.append("")
        for a in approvals:
            lines.append(f"- [{a.get('decision')}] {a.get('phase')} by {a.get('approver')}：{a.get('comment')}")
        lines.append("")

    issues = state.get("issues") or []
    if issues:
        lines.append("## 七、升级问题")
        lines.append("")
        for i in issues:
            lines.append(f"- [{i.get('level')}] {i.get('stage')}：{i.get('message')}")
        lines.append("")

    # 合同草稿
    contract = state.get("contract") or {}
    if contract.get("contract_text"):
        lines.append("## 八、合同草稿")
        lines.append("")
        lines.append("```text")
        lines.append(contract["contract_text"])
        lines.append("```")
        lines.append("")
        lines.append("采购订单：")
        lines.append("```text")
        lines.append(contract.get("po_text", ""))
        lines.append("```")
    lines.append("")
    return "\n".join(lines)
