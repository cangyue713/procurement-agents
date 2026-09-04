"""pytest 公共夹具：路径注入 + 配置 + Mock Provider + Runner。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def config():
    from procurement_agents.config import AppConfig

    cfg = AppConfig.load()
    cfg.llm.provider = "mock"
    cfg.workflow.output_dir = str(ROOT / "outputs" / "test")
    return cfg


@pytest.fixture()
def provider(config):
    from procurement_agents.llm import create_provider

    return create_provider(config)


@pytest.fixture()
def runner(config):
    from procurement_agents.runner import ProcurementRunner

    return ProcurementRunner(config)


@pytest.fixture(scope="session")
def demo_data():
    from examples.demo_case import (
        CASE_ID,
        DEMO_REQUEST_TEXT,
        EXPECTED_STRATEGY,
        EXPECTED_WINNER,
    )

    return {
        "case_id": CASE_ID,
        "request_text": DEMO_REQUEST_TEXT,
        "expected_strategy": EXPECTED_STRATEGY,
        "expected_winner": EXPECTED_WINNER,
    }
