"""Agents 层导出：6 个业务 Agent + 1 个仲裁 Agent。"""
from procurement_agents.agents.arbitrator import ArbitratorAgent
from procurement_agents.agents.base import AgentError, BaseAgent
from procurement_agents.agents.comparison import ComparisonAgent
from procurement_agents.agents.compliance import ComplianceAgent
from procurement_agents.agents.contract import ContractDraftAgent
from procurement_agents.agents.requirement import RequirementAgent
from procurement_agents.agents.strategy import StrategyAgent
from procurement_agents.agents.supplier import SupplierAgent

__all__ = [
    "AgentError",
    "ArbitratorAgent",
    "BaseAgent",
    "ComparisonAgent",
    "ComplianceAgent",
    "ContractDraftAgent",
    "RequirementAgent",
    "StrategyAgent",
    "SupplierAgent",
]

# 业务 Agent 注册表（供文档/追踪展示『分工』全景）
BUSINESS_AGENTS = [
    ("RequirementAgent", "需求理解", RequirementAgent),
    ("StrategyAgent", "采购策略判定", StrategyAgent),
    ("SupplierAgent", "供应商推荐(含库内价目)", SupplierAgent),
    ("ComparisonAgent", "比价分析", ComparisonAgent),
    ("ComplianceAgent", "合规审查", ComplianceAgent),
    ("ContractDraftAgent", "PO/合同草稿", ContractDraftAgent),
]

SUPERVISOR_AGENTS = [("ArbitratorAgent", "仲裁监控", ArbitratorAgent)]
