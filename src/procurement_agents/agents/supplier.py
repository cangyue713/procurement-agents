"""Agent-3 供应商推荐：从供应商库筛出候选短名单（受邀询价对象）。"""
from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel

from procurement_agents.agents.base import AgentError, BaseAgent, require_keys
from procurement_agents.config import WorkflowConfig
from procurement_agents.domain.enums import PhaseName
from procurement_agents.knowledge.supplier_lib import recommend_suppliers


class SupplierAgent(BaseAgent):
    """按品类匹配 + 绩效/资质/成熟度评分，剔除黑名单与高风险，取 Top-N。"""

    phase = PhaseName.SUPPLIER
    display_name = "供应商推荐Agent"
    description = "检索供应商库，评分排序生成候选短名单，排除黑名单/高风险对象"

    def __init__(self, wf_config: WorkflowConfig | None = None) -> None:
        self._wf = wf_config or WorkflowConfig()

    def invoke(self, inputs: Dict[str, Any]) -> BaseModel:
        require_keys(inputs, "requirement", agent=self.display_name)
        requirement = inputs["requirement"]
        category = str(requirement.get("category") or "通用物资")
        quality_hints = [str(q) for q in (requirement.get("quality_requirements") or [])]
        artifact = recommend_suppliers(category, size=self._wf.shortlist_size, demand_hints=quality_hints)
        if not artifact.candidates:
            raise AgentError(f"[{self.display_name}] 品类『{category}』无合格候选供应商，请扩充供应商库")
        return artifact
