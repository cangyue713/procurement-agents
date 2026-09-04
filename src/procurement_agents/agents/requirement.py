"""Agent-1 需求理解：自由文本采购需求 -> 结构化需求单。"""
from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, ValidationError

from procurement_agents.agents.base import AgentError, BaseAgent, require_keys
from procurement_agents.domain.enums import PhaseName, ProductCategory, UrgencyLevel
from procurement_agents.domain.models import RequirementArtifact
from procurement_agents.llm.base import TASK_PARSE_REQUIREMENT, LLMProvider, LLMRequest

_VALID_CATEGORIES = {c.value for c in ProductCategory}
_VALID_URGENCIES = {u.value for u in UrgencyLevel}


class RequirementAgent(BaseAgent):
    """把采购经理提出的自然语言需求解析为可被下游消费的结构化需求。"""

    phase = PhaseName.REQUIREMENT
    display_name = "需求理解Agent"
    description = "解析采购需求文本：品类/数量/预算/交期/质量要求，识别缺失字段"

    def __init__(self, provider: LLMProvider) -> None:
        self._llm = provider

    def invoke(self, inputs: Dict[str, Any]) -> BaseModel:
        require_keys(inputs, "request_text", agent=self.display_name)
        resp = self._llm.complete(LLMRequest(
            task=TASK_PARSE_REQUIREMENT,
            text=str(inputs["request_text"]),
            expect_schema="RequirementArtifact",
        ))
        if not resp.ok:
            raise AgentError(f"[{self.display_name}] 解析失败: {resp.error}")
        data = resp.data
        # 兜底清洗：非法枚举值回退
        if data.get("category") not in _VALID_CATEGORIES:
            data["category"] = ProductCategory.guess_from_text(str(inputs["request_text"])).value
        if data.get("urgency") not in _VALID_URGENCIES:
            data["urgency"] = UrgencyLevel.NORMAL.value
        try:
            return RequirementArtifact(**data)
        except ValidationError as exc:
            raise AgentError(f"[{self.display_name}] 结构化结果不合法: {exc}") from exc
