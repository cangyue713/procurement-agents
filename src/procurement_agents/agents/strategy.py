"""Agent-2 采购策略判定：依据需求特征选择合规的采购方式。"""
from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel

from procurement_agents.agents.base import AgentError, BaseAgent, require_keys
from procurement_agents.config import RuleConfig
from procurement_agents.domain.enums import PhaseName, ProcurementStrategy
from procurement_agents.domain.models import StrategyArtifact
from procurement_agents.knowledge.strategy_rules import decide_strategy

_VALID = {s.value for s in ProcurementStrategy}


class StrategyAgent(BaseAgent):
    """根据预算/紧急度/独家特征判定：直接采购、询比价、招标、单一来源等。"""

    phase = PhaseName.STRATEGY
    display_name = "策略判定Agent"
    description = "按内控规则判定采购策略，输出可追溯的判定依据"

    def __init__(self, rules: RuleConfig | None = None) -> None:
        self._rules = rules or RuleConfig()

    def invoke(self, inputs: Dict[str, Any]) -> BaseModel:
        require_keys(inputs, "requirement", agent=self.display_name)
        requirement = inputs["requirement"]
        result = decide_strategy(requirement, self._rules)
        if result["strategy"] not in _VALID:
            raise AgentError(f"[{self.display_name}] 策略判定异常: {result['strategy']}")
        return StrategyArtifact(**result)
