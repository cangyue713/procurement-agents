"""录制 LLM 契约夹具（P2-E）：用真实 Provider 跑一遍契约样本并保存输出。

用途：
    1) 首次/换模型后录制：真实模型输出 -> tests/fixtures/llm/parse_requirement.json
    2) 之后 CI/本地跑 tests/test_llm_contract.py：回放夹具断言
       『归一化/解析逻辑不因模型更新回退』（模型输出变了 -> 测试红 -> 人工评审）。

用法：
    # 真实模型（需 .env 配置 DEEPSEEK_API_KEY；也可指定 provider=openai_compat）
    python -X utf8 tools/record_provider.py --provider deepseek \
        --out tests/fixtures/llm/parse_requirement.json

    # 无 API Key 时允许用 mock 生成 baseline（供应商默认动作）
    python -X utf8 tools/record_provider.py --provider mock \
        --out tests/fixtures/llm/parse_requirement.json

评审指引：录制真实模型后若契约测试变红，先人工判断是『模型输出语义漂移』
（更新样本/金标准）还是『归一化逻辑缺陷』（修 llm/openai_compat._normalize）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from procurement_agents.config import PROJECT_ROOT, AppConfig
from procurement_agents.llm.base import LLMProvider
from procurement_agents.llm.contract_samples import SAMPLE_REQUESTS
from procurement_agents.llm.recorder import record_to_fixture

DEFAULT_OUT = PROJECT_ROOT / "tests" / "fixtures" / "llm" / "parse_requirement.json"


def _make_provider(kind: str) -> LLMProvider:
    from procurement_agents.llm.mock import MockProvider
    from procurement_agents.llm.openai_compat import OpenAICompatProvider

    if kind == "mock":
        return MockProvider()
    cfg = AppConfig.load()
    api_key = os.getenv(cfg.llm.api_key_env) or os.getenv("DEEPSEEK_API_KEY") or ""
    if not api_key:
        sys.exit("未配置 API Key（环境变量 %s）。需真实模型请先配置 .env；"
                 "或使用 --provider mock 录制离线 baseline。" % cfg.llm.api_key_env)
    base_url = os.getenv("OPENAI_COMPAT_BASE_URL", cfg.llm.base_url)
    model = os.getenv("OPENAI_COMPAT_MODEL", cfg.llm.model)
    return OpenAICompatProvider(
        api_key=api_key, base_url=base_url, model=model,
        temperature=cfg.llm.temperature, timeout_seconds=cfg.llm.timeout_seconds,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="录制 LLM 契约夹具")
    parser.add_argument("--provider", default="deepseek",
                        choices=["deepseek", "openai_compat", "mock"],
                        help="录制的 Provider（mock=离线 baseline）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="夹具输出路径（默认 tests/fixtures/llm/parse_requirement.json）")
    args = parser.parse_args()

    provider = _make_provider(args.provider)
    out = record_to_fixture(provider, SAMPLE_REQUESTS,
                            args.out, model=os.getenv("OPENAI_COMPAT_MODEL", ""))
    print(f"录制完成：{len(SAMPLE_REQUESTS)} 条样本 -> {out} (provider={provider.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
