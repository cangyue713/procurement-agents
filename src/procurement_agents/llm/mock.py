"""MockProvider：内置中文规则引擎，离线、确定、可测。"""
from __future__ import annotations

from procurement_agents.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    TASK_PARSE_REQUIREMENT,
)
from procurement_agents.llm.mock_engines import parse_requirement_text


class MockProvider(LLMProvider):
    """以确定性规则引擎充当 '模型'，用于离线演示、单元测试与 CI。"""

    name = "mock"

    def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            if request.task == TASK_PARSE_REQUIREMENT:
                data = parse_requirement_text(request.text)
            else:
                return LLMResponse.failed(self.name, f"Mock 引擎不支持任务: {request.task}")
            return LLMResponse(data=data, provider=self.name)
        except Exception as exc:  # 边界兜底：引擎错误统一包装为失败响应
            return LLMResponse.failed(self.name, str(exc))
