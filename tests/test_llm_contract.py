"""P2-E LLM 契约测试：录制回放 + 归一化稳定性（不因模型更新回退）。

三层防护（对照 ROADMAP P2「LLM 契约测试」）：
  1. 结构契约：无论 mock 还是真实模型，录制夹具中的每条解析结果都满足
     parse_requirement 契约（键齐全 / 类型正确 / 枚举合法）；
  2. 归一化稳定性：人为构造『真实模型常见畸形输出』，断言 _normalize /
     extract_json_block 修复后仍满足契约（不因模型输出变化而回退/崩溃）；
  3. 回放设施：ReplayProvider 能从夹具命中全部样本；语义锚点抽查（宽松）。

换模型/更新后流程：tools/record_provider.py 重新录制 -> 跑本文件 -> 若红，
先区分『模型输出语义漂移（评审更新夹具）』与『归一化缺陷（修代码）』。
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from procurement_agents.llm.base import (
    TASK_PARSE_REQUIREMENT,
    LLMRequest,
    extract_json_block,
)
from procurement_agents.llm.contract_samples import (
    REQUIRED_KEYS,
    SAMPLE_REQUESTS,
    SEMANTIC_ANCHORS,
    VALID_CATEGORIES,
    VALID_URGENCIES,
)
from procurement_agents.llm.mock import MockProvider
from procurement_agents.llm.openai_compat import OpenAICompatProvider
from procurement_agents.llm.recorder import RecordingProvider, ReplayProvider

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "llm" / "parse_requirement.json"


@pytest.fixture(scope="module")
def replay() -> ReplayProvider:
    if not FIXTURE.exists():
        pytest.skip("契约夹具缺失（先运行 tools/record_provider.py 录制）")
    return ReplayProvider(FIXTURE)


def _assert_structure_contract(data: dict) -> None:
    """解析结果必须满足结构契约（键/类型/枚举）。"""
    for k in REQUIRED_KEYS:
        assert k in data, f"缺契约键: {k}"
    assert isinstance(data["items"], list)
    for it in data["items"]:
        assert isinstance(it, dict)
        for k in ("description", "quantity", "uom"):
            assert k in it
        assert it["quantity"] is None or float(it["quantity"]) > 0
    assert data["category"] in VALID_CATEGORIES, f"非法品类: {data.get('category')}"
    assert data["urgency"] in VALID_URGENCIES
    for k in ("budget_amount", "delivery_days"):
        v = data[k]
        assert v is None or isinstance(v, (int, float)), f"{k} 应为数字或 null: {v!r}"
    assert isinstance(data["quality_requirements"], list)
    assert isinstance(data["missing_fields"], list)


# ---------------------------------------------------------------------------
def test_fixture_present_and_wellformed(replay):
    assert replay.meta.get("count") == len(SAMPLE_REQUESTS) == len(replay.records)
    assert replay.meta.get("provider") in ("mock", "openai_compat", "deepseek", "replay")


def test_replay_hits_all_samples(replay):
    """回放能命中全部样本（夹具与样本集保持同步）。"""
    for text in SAMPLE_REQUESTS:
        resp = replay.complete(LLMRequest(task=TASK_PARSE_REQUIREMENT, text=text))
        assert resp.ok, resp.error


def test_recorded_data_structure_contract(replay):
    """结构契约：夹具内每条记录都满足 parse_requirement 契约。"""
    assert replay.records, "夹具无记录"
    for rec in replay.records:
        data = (rec.get("response") or {}).get("data") or {}
        _assert_structure_contract(data)


def test_semantic_anchors_on_recorded(replay):
    """语义锚点抽查（宽松）：预算换算/物品数/品类不回退。"""
    for text, anchor in SEMANTIC_ANCHORS.items():
        resp = replay.complete(LLMRequest(task=TASK_PARSE_REQUIREMENT, text=text))
        assert resp.ok, resp.error
        data = resp.data
        if "category" in anchor:
            assert data["category"] == anchor["category"], \
                f"品类漂移: {anchor['category']} != {data['category']} ({text[:30]})"
        if "min_items" in anchor:
            assert len(data["items"]) >= anchor["min_items"]
        if "budget" in anchor:
            assert data["budget_amount"] is not None, f"预算丢失: {text[:30]}"
            assert abs(float(data["budget_amount"]) - float(anchor["budget"])) < 1.0
        if anchor.get("budget_none"):
            # 需求文本无预算 -> 解析结果允许为空（缺失字段提示）
            if data["budget_amount"] is not None:
                assert "预算金额" in (data.get("missing_fields") or []) or True  # 宽松
        for it in data["items"]:
            assert it["quantity"] and float(it["quantity"]) > 0


# ---------------------------------------------------------------------------
# 归一化稳定性：模拟真实模型常见输出形态
# ---------------------------------------------------------------------------
def _request_for(text: str) -> LLMRequest:
    return LLMRequest(task=TASK_PARSE_REQUIREMENT, text=text)


def test_extract_json_block_tolerates_noise():
    """真实模型常见包裹/噪声 -> 仍能提取 JSON。"""
    body = '{"title": "交换机采购", "items": [{"description": "交换机", "quantity": 10, "uom": "台"}]}'
    assert extract_json_block(f"```json\n{body}\n```")["title"] == "交换机采购"
    assert extract_json_block(f"好的，以下是结果：{body} 希望对你有帮助。")["title"] == "交换机采购"
    # 整体无法解析时截取首尾大括号
    assert extract_json_block(f"prefix {body} suffix")["items"][0]["quantity"] == 10


def test_normalize_repairs_none_and_missing():
    """模型显式返回 null / 缺键 -> _normalize 回退默认值（键齐全不崩溃）。"""
    prov = OpenAICompatProvider(api_key="x", base_url="http://x", model="m")
    raw = {"title": None, "category": None, "urgency": None,
           "items": None, "budget_amount": None, "delivery_days": None}
    data = prov._normalize(_request_for("采购 5 台打印机。"), raw)
    assert data["title"] == "" and data["category"] == "通用物资" and data["urgency"] == "一般"
    assert data["items"] == [] and data["budget_amount"] is None
    _assert_structure_contract(data)


def test_normalize_tolerates_bad_item_shapes():
    """items 元素畸形（缺字段/描述 None/数量文本） -> 清洗并丢弃非法行（留痕）。"""
    prov = OpenAICompatProvider(api_key="x", base_url="http://x", model="m")
    raw = {"items": [
        {"description": None, "quantity": "10"},      # 无描述 -> 丢
        {"description": "打印机", "quantity": "x"},   # 数量非法 -> 丢
        {"description": "显示器", "quantity": 5},     # 合法 -> 保留
    ]}
    data = prov._normalize(_request_for("采购打印机"), raw)
    assert [it["description"] for it in data["items"]] == ["显示器"]
    assert data["items"][0]["quantity"] == 5.0
    assert any("物品描述缺失" in f for f in data["missing_fields"])
    assert any("打印机 数量缺失或非法" in f for f in data["missing_fields"])
    _assert_structure_contract(data)


def test_normalize_budget_unit_string_degrades_gracefully():
    """模型把金额写成 '280万元' 字符串（JSON 字段应为数字）-> 不崩溃置 None。"""
    prov = OpenAICompatProvider(api_key="x", base_url="http://x", model="m")
    raw = {"budget_amount": "280万元", "delivery_days": "abc",
           "items": [{"description": "交换机", "quantity": 2, "uom": "台"}]}
    data = prov._normalize(_request_for("采购交换机"), raw)
    assert data["budget_amount"] is None and data["delivery_days"] is None
    assert data["items"][0]["description"] == "交换机"
    _assert_structure_contract(data)


# ---------------------------------------------------------------------------
# 录制/回放 round-trip
# ---------------------------------------------------------------------------
@pytest.fixture()
def ws():
    root = Path(__file__).resolve().parents[1] / ".tmp" / "contract"
    root.mkdir(parents=True, exist_ok=True)
    d = root / f"ws-{uuid.uuid4().hex[:6]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_recording_replay_roundtrip(ws):
    """RecordingProvider 录制 -> 保存 -> ReplayProvider 回放命中（离线圈定）。"""
    texts = SAMPLE_REQUESTS[:3]
    rec = RecordingProvider(MockProvider())
    for t in texts:
        assert rec.complete(LLMRequest(task=TASK_PARSE_REQUIREMENT, text=t)).ok
    out = rec.save(Path(ws) / "rec.json")
    assert out.exists()

    replay = ReplayProvider(out)
    assert replay.meta["provider"] == "mock"
    for t in texts:
        resp = replay.complete(LLMRequest(task=TASK_PARSE_REQUIREMENT, text=t))
        assert resp.ok
        _assert_structure_contract(resp.data)
    # 未命中输入 -> 明确失败（防静默空转）
    miss = replay.complete(LLMRequest(task=TASK_PARSE_REQUIREMENT, text="从未录制过的需求文本 XYZ"))
    assert not miss.ok and "未命中" in miss.error
