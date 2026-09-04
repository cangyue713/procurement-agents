"""mock 引擎 vs 真实 DeepSeek 模型 对照实测（新流程：价格来自供应商库内价目）。

用法（先配置好 .env 的 DEEPSEEK_API_KEY）：
    python -X utf8 examples/compare_providers.py

说明：同一份需求(txt)分别在两种 LLM Provider 下端到端跑一遍，
供应商/价格数据均来自 suppliers.csv，对照解析与采购结论差异。
真实模型调用会消耗少量 API 额度。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

import warnings  # noqa: E402

warnings.resetwarnings()
warnings.filterwarnings("ignore", message=r".*allowed_objects.*")

from examples.demo_case import load_request_text  # noqa: E402
from procurement_agents.config import AppConfig  # noqa: E402
from procurement_agents.runner import ProcurementRunner  # noqa: E402

REQUEST_FILE = Path(__file__).resolve().parent / "input" / "request.txt"


def run_once(provider: str, case_id: str) -> dict:
    config = AppConfig.load()
    config.llm.provider = provider
    runner = ProcurementRunner(config)
    t0 = time.monotonic()
    result = runner.run(request_file=str(REQUEST_FILE), case_id=case_id)
    cost = time.monotonic() - t0
    requirement = result.state.get("requirement") or {}
    comparison = result.state.get("comparison") or {}
    compliance = result.state.get("compliance") or {}
    contract = result.contract
    shortlist = result.state.get("shortlist") or {}
    return {
        "provider": runner.graph_meta.get("provider"),
        "seconds": round(cost, 1),
        "status": result.status,
        "title": requirement.get("title", ""),
        "category": requirement.get("category"),
        "urgency": requirement.get("urgency"),
        "items": requirement.get("items", []),
        "budget": requirement.get("budget_amount"),
        "delivery_days": requirement.get("delivery_days"),
        "strategy": (result.state.get("strategy") or {}).get("strategy"),
        "candidates": [c["name"] for c in shortlist.get("candidates", [])],
        "recommended": (comparison.get("recommended") or {}).get("supplier_name"),
        "approved": compliance.get("approved_supplier_ids"),
        "rejected": compliance.get("rejected"),
        "winner": contract.get("supplier_name"),
        "total": contract.get("total_amount"),
        "holds": sum(1 for r in result.arbitration_records if r.get("verdict") == "hold"),
        "issues": len(result.issues),
    }


def render(a: dict, b: dict) -> None:
    print("=" * 78)
    print(f"  {'字段':<12} {'Mock 规则引擎':<32} DeepSeek 真实模型")
    print("=" * 78)
    keys = [
        ("状态/耗时", lambda d: f"{d['status']}（{d['seconds']}s）"),
        ("需求标题", lambda d: str(d["title"])),
        ("品类 / 紧急度", lambda d: f"{d['category']} / {d['urgency']}"),
        ("明细行数", lambda d: f"{len(d['items'])} 行"),
        ("预算 / 交期", lambda d: f"{d['budget']:,.0f} 元 / {d['delivery_days']} 天"),
        ("采购策略", lambda d: str(d["strategy"])),
        ("候选供应商", lambda d: "、".join(d["candidates"])),
        ("比价推荐", lambda d: str(d["recommended"])),
        ("合规放行/否决", lambda d: f"{len(d['approved'])}家放行 / {d['rejected']}"),
        ("成交对象", lambda d: str(d["winner"]) or "-"),
        ("成交金额", lambda d: f"{d['total']:,.2f} 元" if d["total"] else "-"),
        ("仲裁挂起/问题", lambda d: f"{d['holds']} 次 / {d['issues']} 项"),
    ]
    for label, fn in keys:
        try:
            left, right = fn(a), fn(b)
            print(f"  {label:<12} {left:<32} {right}")
        except Exception as exc:  # 单字段渲染失败不中断对照输出
            print(f"  {label:<12} ! {exc}")
    print("=" * 78)


def main() -> int:
    print(f">>> 需求数据源：{REQUEST_FILE}")
    print(">>> 第 1/2 轮：Mock 规则引擎（离线）")
    mock = run_once("mock", "PC-COMPARE-MOCK")
    print(f"    status={mock['status']} 耗时 {mock['seconds']}s，成交={mock['winner']}")

    print(">>> 第 2/2 轮：DeepSeek 真实模型（联网调用中...）")
    ds = run_once("deepseek", "PC-COMPARE-DEEPSEEK")
    print(f"    status={ds['status']} 耗时 {ds['seconds']}s，成交={ds['winner']}")

    print()
    render(mock, ds)
    print()
    ok = mock["status"] == "done" and ds["status"] == "done" and mock["winner"] == ds["winner"]
    print("对照结论：两引擎结论一致 ✔" if ok else "对照结论：存在差异，请结合报告人工判断")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
