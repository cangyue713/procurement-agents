"""健壮性/边界测试：补足异常路径与工具函数覆盖（CI coverage 门禁用）。"""
from __future__ import annotations

import json

import pytest

from procurement_agents.llm.base import (
    extract_json_block,
    json_compatible,
)
from procurement_agents.runner import ProcurementRunner


# --------------------------------------------------------------------------
# Runner 输入校验（异常路径）
# --------------------------------------------------------------------------
def test_runner_rejects_empty_request(config):
    runner = ProcurementRunner(config)
    with pytest.raises(ValueError, match="缺少采购需求输入"):
        runner.run(request_text="")


def test_runner_rejects_missing_file(config):
    runner = ProcurementRunner(config)
    with pytest.raises(FileNotFoundError):
        runner.run(request_file="不存在/的文件.txt")


# --------------------------------------------------------------------------
# LLM 工具函数
# --------------------------------------------------------------------------
def test_extract_json_block_fenced():
    obj = extract_json_block('```json\n{"a": 1, "b": [2]}\n```')
    assert obj == {"a": 1, "b": [2]}


def test_extract_json_block_padded_text():
    obj = extract_json_block('说明文字{"title": "采购", "items": []}结尾')
    assert obj["title"] == "采购"


def test_extract_json_block_invalid_raises():
    with pytest.raises(ValueError):
        extract_json_block("完全没有 JSON 结构的纯文本")


def test_json_compatible_handles_primitives_and_models():
    from pydantic import BaseModel

    class M(BaseModel):
        x: int

    assert json_compatible(M(x=1)) == {"x": 1}
    assert json_compatible({"k": [1, None, "s"]}) == {"k": [1, None, "s"]}
    assert json_compatible(None) is None
    # 未知对象兜底转字符串
    assert json_compatible(object())  # 非空即可


# --------------------------------------------------------------------------
# 策略判定规则补充分支
# --------------------------------------------------------------------------
def test_strategy_rules_urgency_and_single_source(config):
    from procurement_agents.knowledge.strategy_rules import decide_strategy

    # 特急/紧急会追加提速理由
    urgent = decide_strategy({"budget_amount": 10_000, "notes": "常规", "urgency": "特急"})
    assert urgent["strategy"] == "直接采购"
    assert any("紧急度" in r for r in urgent["rationale"])

    # 公开招标门槛（> 5 倍阈值）
    big = decide_strategy({"budget_amount": 6_000_000, "notes": "常规", "urgency": "一般"})
    assert big["strategy"] == "公开招标"
    assert "R5" in big["rules_applied"]


# --------------------------------------------------------------------------
# 仲裁默认放行未知阶段 / 路由收尾
# --------------------------------------------------------------------------
def test_arbitrator_unknown_phase_defaults_proceed():
    from procurement_agents.agents.arbitrator import ArbitratorAgent
    from procurement_agents.domain.enums import PhaseName, VerdictAction

    # 辅助阶段（人工审批等）无仲裁规则 -> 默认放行
    rec = ArbitratorAgent().invoke({"phase": PhaseName.APPROVAL.value, "artifact": {}})
    assert rec.verdict == VerdictAction.PROCEED


def test_router_after_block_goes_finish():
    from procurement_agents.pipeline.graph import _router_after_arbitration

    assert _router_after_arbitration({"status": "blocked"}) == "finish_node"
    assert _router_after_arbitration({"status": "failed"}) == "finish_node"
    # 最近一条仲裁为 block 时也收尾
    state = {"arbitration": [{"verdict": "block"}], "pending_next": "strategy_node"}
    assert _router_after_arbitration(state) == "finish_node"


# --------------------------------------------------------------------------
# 供应商价目解析边界
# --------------------------------------------------------------------------
def test_parse_price_items_boundaries():
    from procurement_agents.knowledge.supplier_lib import parse_price_items

    assert parse_price_items("") == {}
    assert parse_price_items("物品=100|坏行|物品2=2,500") == {"物品": 100.0, "物品2": 2500.0}
    assert parse_price_items("只有描述没有等号") == {}


def test_match_price_containment_and_missing():
    from procurement_agents.knowledge.supplier_lib import match_price

    assert match_price("48口万兆交换机=26000|工业级交换机=10000", "48口万兆交换机") == 26000.0
    # 找不到时不返回价格
    assert match_price("服务器=100", "完全无关的物品XYZ") is None
    assert match_price("", "任意物品") is None


# --------------------------------------------------------------------------
# 追踪渲染与 JSON 序列化
# --------------------------------------------------------------------------
def test_render_markdown_contains_sections():
    from procurement_agents.pipeline.tracing import render_markdown

    md = render_markdown({
        "case_id": "PC-X",
        "status": "done",
        "meta": {"provider": "mock"},
        "final_report": {"summary": "采购结论摘要"},
        "phase_history": [{"phase": "需求理解", "status": "ok", "retries": 0, "started_at": "2026-01-01T10:00:00"}],
        "arbitration": [{"phase": "需求理解", "verdict": "proceed", "quality_score": 100,
                         "summary": "放行", "checks": []}],
        "contract": {"contract_text": "合同正文示例", "po_text": "PO 示例"},
    })
    assert "采购结论摘要" in md
    assert "仲裁 Agent 全程裁决" in md
    assert "合同正文示例" in md


def test_openai_compat_normalize_requirement():
    """schema 级归一化：宽容真实模型的字段偏差（纯函数，不联网）。"""
    from procurement_agents.llm.base import LLMRequest, TASK_PARSE_REQUIREMENT
    from procurement_agents.llm.openai_compat import OpenAICompatProvider

    norm = OpenAICompatProvider._normalize
    req = LLMRequest(task=TASK_PARSE_REQUIREMENT)
    # items 非 list / budget 为字符串 / 缺字段 -> 全部兜底
    out = norm(req, {
        "title": "T",
        "category": "IT设备",
        "items": "不是数组",
        "budget_amount": "2800000",
        "delivery_days": None,
        "quality_requirements": None,
        "usage_scene": None,
        "missing_fields": None,
        "notes": None,
    })
    assert out["items"] == []
    assert out["budget_amount"] == 2800000.0
    assert out["delivery_days"] is None
    assert out["quality_requirements"] == []
    assert out["missing_fields"] == []


class _DirtyProvider:
    """返回非法枚举值/非法结构的假 Provider（测试 RequirementAgent 兜底）。"""

    name = "dirty"

    def __init__(self, data: dict) -> None:
        self._data = data

    def complete(self, request):
        from procurement_agents.llm.base import LLMResponse

        return LLMResponse(data=self._data, provider=self.name)


def test_requirement_agent_fallback_and_validation():
    """非法品类/紧急度回退；非法结构触发 AgentError。"""
    from procurement_agents.agents.base import AgentError
    from procurement_agents.agents.requirement import RequirementAgent

    agent = RequirementAgent(_DirtyProvider({
        "title": "测试需求", "category": "不存在的品类", "urgency": "不存在的紧急度",
        "items": [{"description": "交换机", "quantity": 1, "uom": "台"}],
        "budget_amount": 100, "delivery_days": 5,
        "quality_requirements": [], "usage_scene": "", "missing_fields": [], "notes": "",
    }))
    art = agent.invoke({"request_text": "采购 1 台 48口万兆交换机。"})
    assert art.category == "IT设备"      # 关键词兜底
    assert art.urgency == "一般"          # 默认紧急度

    # 完全非法的结构（缺 items 且非对象）-> AgentError
    bad = RequirementAgent(_DirtyProvider({"not": "a valid artifact"}))
    with pytest.raises(AgentError):
        bad.invoke({"request_text": "任意文本"})


def test_trace_roundtrip(config, runner):
    """trace JSON 可被 json 完整加载（全状态 JSON 安全）。"""
    from examples.demo_case import DEMO_REQUEST_TEXT

    result = runner.run(
        request_text=DEMO_REQUEST_TEXT,
        case_id="PC-T-TRACE",
    )
    assert result.is_done, result.summary_text
    md_path = result.save_report()   # save_report 才产出 trace 文件
    assert md_path.exists()
    trace_path = result.trace_path
    assert trace_path is not None
    data = json.loads(trace_path.read_text(encoding="utf-8"))
    assert data["status"] == "done"
    assert data["case_id"] == "PC-T-TRACE"
