"""端到端演示：一次运行跑通『需求理解 -> ... -> 合同草稿』+ 仲裁全程监控。

运行（在项目根目录）：
    python examples/run_demo.py                # 默认 Mock 模型
    python examples/run_demo.py --provider deepseek   # 需 .env 配 DEEPSEEK_API_KEY

产物：outputs/report_*.md（可读报告）与 outputs/trace_*.json（全量追踪）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证可直接 `python examples/run_demo.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows 控制台输出 UTF-8，避免中文乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 抑制第三方库的 pending 弃用警告（langchain/langgraph 以 simplefilter('always') 强制显示，
# 需先重置过滤器再按需忽略；仅作用于本演示脚本进程）
import warnings  # noqa: E402

warnings.resetwarnings()
warnings.filterwarnings("ignore", message=r".*allowed_objects.*")

from examples.demo_case import (  # noqa: E402
    CASE_ID,
    EXPECTED_STRATEGY,
    EXPECTED_WINNER,
)
from procurement_agents.config import AppConfig  # noqa: E402
from procurement_agents.runner import ProcurementRunner  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="采购多Agent流程 端到端演示")
    parser.add_argument("--provider", default=None, help="mock | deepseek | openai_compat（默认取配置）")
    parser.add_argument("--no-trace", action="store_true", help="不写 outputs 产物")
    args = parser.parse_args()

    config = AppConfig.load()
    if args.provider:
        config.llm.provider = args.provider

    runner = ProcurementRunner(config)
    print("=" * 72)
    print(f"  采购多Agent流程 · 演示案例 {CASE_ID}")
    print(f"  Provider = {runner.graph_meta.get('provider')} | 图节点数 = {runner.graph_meta.get('graph_node_count')}")
    print("=" * 72)

    result = runner.run(
        request_file=str(Path(__file__).resolve().parent / "input" / "request.txt"),
        case_id=CASE_ID,
    )

    print()
    print(result.summary_text)
    print()

    # 仲裁全程裁决一览
    print("-" * 72)
    print("  仲裁 Agent 全程裁决")
    print("-" * 72)
    for rec in result.arbitration_records:
        icon = {"proceed": "放行", "hold": "挂起", "block": "阻断"}.get(rec.get("verdict"), rec.get("verdict"))
        print(f"  [{rec.get('phase')}] {icon}  质量分 {rec.get('quality_score')}  {rec.get('summary')}")
    for appr in result.state.get("approvals") or []:
        print(f"  (人审) {appr.get('phase')} -> {appr.get('decision')} by {appr.get('approver')}")

    if result.issues:
        print("-" * 72)
        for i in result.issues:
            print(f"  [升级] {i.get('message')}")

    if not args.no_trace:
        md = result.save_report()
        print()
        print(f"报告已保存: {md.resolve()}")
        print(f"追踪已保存: {result.trace_path.resolve()}")

    # 断言预期，作为演示自检
    ok = result.is_done
    strategy = (result.state.get("strategy") or {}).get("strategy")
    winner = (result.state.get("contract") or {}).get("supplier_name")
    print()
    print("-" * 72)
    print(f"  状态={result.status} | 策略={strategy} | 成交={winner}")
    if strategy != EXPECTED_STRATEGY or winner != EXPECTED_WINNER or not ok:
        print("  !! 演示结果与剧本预期不符，请检查")
        return 1
    print("  演示自检通过：与剧本预期一致 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
