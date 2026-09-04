"""Mock 规则引擎与 Provider 测试（需求解析；价格数据不再走 LLM 层）。"""
from __future__ import annotations

from procurement_agents.llm.base import TASK_PARSE_REQUIREMENT, LLMRequest
from procurement_agents.llm.mock_engines import parse_requirement_text


def test_parse_requirement_demo(demo_data):
    data = parse_requirement_text(demo_data["request_text"])
    assert data["category"] == "IT设备"
    assert data["urgency"] == "紧急"
    assert data["budget_amount"] == 2800000.0
    assert data["delivery_days"] == 25
    assert len(data["items"]) == 3
    qty_uom = [(it["quantity"], it["uom"]) for it in data["items"]]
    assert (40, "台") in qty_uom and (2, "台") in qty_uom and (3, "台") in qty_uom
    assert any("ISO9001" in q for q in data["quality_requirements"])
    assert data["missing_fields"] == []
    assert "采购" in data["title"]


def test_parse_requirement_missing_fields():
    data = parse_requirement_text("采购 5 台打印机。")
    assert data["budget_amount"] is None
    assert data["delivery_days"] is None
    assert {"预算金额", "交付期"} <= set(data["missing_fields"])


def test_parse_requirement_item_followed_by_chinese():
    """'10 台工业级交换机' 这类数量后直接跟商品名也应被解析。"""
    data = parse_requirement_text("采购 10 台工业级交换机，预算 20 万元，要求 15 天内交付。")
    assert len(data["items"]) == 1
    assert data["items"][0]["quantity"] == 10.0
    assert "交换机" in data["items"][0]["description"]
    assert data["budget_amount"] == 200000.0
    assert data["delivery_days"] == 15


def test_mock_provider_requirement_task(provider):
    resp = provider.complete(LLMRequest(task=TASK_PARSE_REQUIREMENT, text="采购 3 台笔记本电脑。预算 2 万元。"))
    assert resp.ok
    assert resp.data["category"] == "IT设备"
    assert resp.data["budget_amount"] == 20000.0
