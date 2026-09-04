"""P1.1 金额 Decimal 化回归测试：money 工具、费率单一常量、状态序列化语义。"""
from __future__ import annotations

import inspect
from decimal import Decimal

from procurement_agents.domain.money import (
    PENALTY_DAILY_RATE,
    fmt_money,
    penalty_pct_text,
    to_decimal,
    to_decimal_or_zero,
)


def test_to_decimal_normalizes_all_representations():
    """状态/追踪中金额可能是 str/int/float/None，统一还原为 Decimal（精度无损）。"""
    assert to_decimal(None) is None
    assert to_decimal("") is None
    assert to_decimal("2126400.00") == Decimal("2126400.00")
    assert to_decimal("2,126,400.00") == Decimal("2126400.00")   # 容忍千分位
    assert to_decimal(2126400) == Decimal("2126400")
    assert to_decimal(2126400.0) == Decimal("2126400")           # float 整数无噪声
    assert to_decimal(Decimal("0.10")) == Decimal("0.10")
    # 非法输入不抛异常
    assert to_decimal("不是金额") is None


def test_to_decimal_or_zero_default():
    assert to_decimal_or_zero(None) == Decimal("0")
    assert to_decimal_or_zero("500") == Decimal("500")


def test_fmt_money():
    assert fmt_money(Decimal("2126400.00")) == "2,126,400.00"
    assert fmt_money("2126400") == "2,126,400.00"
    assert fmt_money(None) == "-"


def test_penalty_rate_single_source_of_truth():
    """违约金费率收敛为单一常量：模型默认值即常量值，文本渲染随常量联动。"""
    assert PENALTY_DAILY_RATE == Decimal("0.0005")
    assert penalty_pct_text() == "0.05%"
    # 领域模型 ContractArtifact 默认费率 == 常量（修复默认 0.005 vs 写入 0.0005 不一致）
    from procurement_agents.domain.models import ContractArtifact

    assert ContractArtifact().penalty_rate == PENALTY_DAILY_RATE
    # 合同 Agent 实际写入的费率必须引用常量（写死数值回归防线）
    from procurement_agents.agents.contract import ContractDraftAgent

    src = inspect.getsource(ContractDraftAgent.invoke)
    assert "penalty_rate=PENALTY_DAILY_RATE" in src
    # 模型字段默认声明也必须引用常量而非写死
    models_src = inspect.getsource(ContractArtifact)
    assert "penalty_rate: Decimal = Field(PENALTY_DAILY_RATE" in models_src


def test_decimal_roundtrip_through_model_json():
    """Decimal 经 pydantic json 序列化为字符串（精度无损），读回可还原。"""
    from procurement_agents.domain.models import QuoteLine

    line = QuoteLine(description="交换机", quantity=2, unit_price=Decimal("26000"), amount=Decimal("52000.00"))
    dumped = line.model_dump(mode="json")
    assert dumped["unit_price"] == "26000"
    assert dumped["amount"] == "52000.00"
    assert to_decimal(dumped["amount"]) == Decimal("52000.00")
