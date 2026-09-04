"""金额工具：采购全链路 Decimal 化（P1.1 领域硬伤修复）。

约定：
  * 领域层所有金额字段(money)一律为 ``decimal.Decimal``，禁止 float 参与金额算术；
  * pydantic v2 的 ``model_dump(mode="json")`` 会把 Decimal 序列化为**字符串**
    （保留精度，不会出现 0.1+0.2 类误差）——因此 LangGraph 状态/追踪里的金额是
    str；读回计算时必须经 :func:`to_decimal` 还原为 Decimal；
  * 所有对外展示（报告/模板/日志）统一走 :func:`fmt_money`。

违约金费率单一常量：合同模板约定『每逾期一日交付，按合同总价 0.05% 支付违约金』，
即 0.0005/日。此前 `domain/models.py` 默认 0.005 与 `agents/contract.py` 写入的
0.0005 不一致（相差 10 倍），已收敛为本常量并在两处共用。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

#: 违约金日费率（单一事实来源）：0.05% = 0.0005，与 contract.md.tpl 模板文本一致
PENALTY_DAILY_RATE = Decimal("0.0005")

#: 金额量化步长（两位小数）
CENT = Decimal("0.01")


def to_decimal(value: Any) -> Optional[Decimal]:
    """把状态/追踪中的金额（Decimal / str / int / float / None）安全还原为 Decimal。

    兼容形态：
      * ``None`` / 空串       -> None
      * ``Decimal``           -> 原样返回
      * ``"2126400.00"``      -> Decimal("2126400.00")（容忍千分位逗号）
      * ``2126400.0`` / 整数  -> Decimal(str(v))（以十进制字面量构造，避免二进制噪声）
    非法输入返回 None（不抛异常，供外部判断缺值）。
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def to_decimal_or_zero(value: Any) -> Decimal:
    """缺值视为 0（供金额比较的便捷入口）。"""
    d = to_decimal(value)
    return d if d is not None else Decimal("0")


def fmt_money(value: Any) -> str:
    """金额展示：千分位 + 两位小数；空值显示 ``-``。接受 Decimal/str/int/float。"""
    d = to_decimal(value)
    return f"{d:,.2f}" if d is not None else "-"


def penalty_pct_text() -> str:
    """违约金费率的人类可读百分比文本（模板渲染用，随常量联动）。"""
    return f"{float(PENALTY_DAILY_RATE * 100):g}%"
