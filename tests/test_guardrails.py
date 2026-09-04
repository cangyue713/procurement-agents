"""节点护栏测试：重试计数、超时、失败升级（不依赖 LangGraph）。"""
from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel

from procurement_agents.agents.base import AgentError, BaseAgent
from procurement_agents.domain.enums import PhaseName
from procurement_agents.pipeline.nodes import PhaseNode


class _EchoArtifact(BaseModel):
    value: str


class _FlakyAgent(BaseAgent):
    phase = PhaseName.REQUIREMENT
    display_name = "波动Agent"

    def __init__(self, fail_times: int) -> None:
        self._left = fail_times

    def invoke(self, inputs: Dict[str, Any]) -> BaseModel:
        if self._left > 0:
            self._left -= 1
            raise AgentError("瞬时不稳")
        return _EchoArtifact(value=str(inputs["x"]))


class _AlwaysFailAgent(BaseAgent):
    phase = PhaseName.REQUIREMENT
    display_name = "必败Agent"

    def invoke(self, inputs: Dict[str, Any]) -> BaseModel:
        raise AgentError("数据非法")


def test_retry_recovers():
    node = PhaseNode(
        node_name="flaky", phase=PhaseName.REQUIREMENT,
        agent_factory=lambda: _FlakyAgent(fail_times=2),
        map_inputs=lambda s: {"x": s["k"]},
        map_outputs=lambda a: {"result": a.model_dump(mode="json")},
        timeout_seconds=5, max_retries=3,
    )
    updates = node({"k": 1})
    assert updates["result"]["value"] == "1"
    # 阶段历史应记录重试 2 次
    stage = updates["phase_history"][-1]
    assert stage["status"] == "ok" and stage["retries"] == 2


def test_retry_exhausted_upgrades():
    node = PhaseNode(
        node_name="doomed", phase=PhaseName.REQUIREMENT,
        agent_factory=_AlwaysFailAgent,
        map_inputs=lambda s: {},
        map_outputs=lambda a: {},
        timeout_seconds=5, max_retries=2,
    )
    updates = node({})
    assert updates["status"] == "failed"
    assert any(i["level"] == "error" for i in updates["issues"])
    stage = updates["phase_history"][-1]
    assert stage["status"] == "error" and stage["retries"] == 2


class _SlowAgent(BaseAgent):
    phase = PhaseName.REQUIREMENT
    display_name = "慢Agent"

    def invoke(self, inputs: Dict[str, Any]) -> BaseModel:
        import time

        time.sleep(1.0)
        return _EchoArtifact(value="ok")


def test_timeout_raises_and_upgrades():
    node = PhaseNode(
        node_name="slow", phase=PhaseName.REQUIREMENT,
        agent_factory=_SlowAgent,
        map_inputs=lambda s: {},
        map_outputs=lambda a: {"r": 1},
        timeout_seconds=0.1, max_retries=0,
    )
    updates = node({})
    assert updates["status"] == "failed"
    assert "超时" in updates["issues"][-1]["message"]
