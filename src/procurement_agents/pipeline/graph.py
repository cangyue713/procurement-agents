"""LangGraph 工作流组装：『6+1』Agent 的工业化编排。

业务链路（价格与供应商介绍同源，来自 suppliers.csv，无询价/报价环节）：
    需求理解 -> 采购策略判定 -> 供应商推荐(库内价目)
  -> 比价分析 -> 合规审查 -> PO/合同草稿生成

图结构（线性主链 + 逐阶段仲裁监督）：
    START -> 业务节点 -> 仲裁节点 -(裁决路由)->
               ├─ proceed -> 下一业务节点
               ├─ hold    -> 人工审批节点 ->(放行) 下一业务节点 /(拒绝)收尾
               ├─ block / 业务失败 -> 收尾节点(阻断)
    ... -> final_node -> 仲裁收官 -> finish_node -> END

工业化要素：内存 checkpointer(可暂停/恢复)、条件路由、重试与超时(节点内)、
全程仲裁留痕 + 审批留痕 + 阶段运行记录。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, List

from pydantic import BaseModel

from procurement_agents.agents.arbitrator import ArbitratorAgent
from procurement_agents.agents.comparison import ComparisonAgent
from procurement_agents.agents.compliance import ComplianceAgent
from procurement_agents.agents.contract import ContractDraftAgent
from procurement_agents.agents.requirement import RequirementAgent
from procurement_agents.agents.strategy import StrategyAgent
from procurement_agents.agents.supplier import SupplierAgent
from procurement_agents.config import AppConfig, WorkflowConfig
from procurement_agents.domain.enums import PhaseName, VerdictAction
from procurement_agents.domain.models import ApprovalRecord
from procurement_agents.llm.base import LLMProvider
from procurement_agents.pipeline.nodes import PhaseNode
from procurement_agents.pipeline.state import WorkflowState

logger = logging.getLogger(__name__)

# ---- 节点名常量 -----------------------------------------------------------
N_REQ = "requirement_node"
N_STR = "strategy_node"
N_SUP = "supplier_node"
N_CMP = "comparison_node"
N_CL = "compliance_node"
N_CT = "contract_node"
N_FIN = "final_node"
N_APPROVE = "approval_node"
N_FINISH = "finish_node"

# 人工决策器注册表（case_id -> callable）。
# 说明：回调函数不可被 checkpoint 序列化，故不进状态，
# 由 Runner 在运行前注册、运行后注销。
HUMAN_DECIDERS: Dict[str, Callable[[str, str], bool]] = {}


# ==========================================================================
# 业务节点构造
# ==========================================================================
def _build_business_nodes(cfg: AppConfig, provider: LLMProvider) -> Dict[str, PhaseNode]:
    timeout, retries = cfg.workflow.agent_timeout_seconds, cfg.workflow.max_agent_retries
    llm = provider
    wf = cfg.workflow
    rules = cfg.rules
    nodes: Dict[str, PhaseNode] = {}

    def _reg(name: str, phase: PhaseName, factory: Callable[[], BaseAgent],
             to_inputs: Callable[[Dict[str, Any]], Dict[str, Any]],
             to_updates: Callable[[BaseModel], Dict[str, Any]],
             artifact_keys: List[str]) -> None:
        nodes[name] = PhaseNode(
            node_name=name, phase=phase, agent_factory=factory,
            map_inputs=to_inputs, map_outputs=to_updates,
            timeout_seconds=timeout, max_retries=retries, artifact_keys=artifact_keys,
        )

    _reg(N_REQ, PhaseName.REQUIREMENT,
         lambda: RequirementAgent(llm),
         lambda s: {"request_text": (s.get("input") or {}).get("request_text", "")},
         lambda a: {"requirement": a.model_dump(mode="json")},
         ["requirement"])

    _reg(N_STR, PhaseName.STRATEGY,
         lambda: StrategyAgent(rules),
         lambda s: {"requirement": s.get("requirement")},
         lambda a: {"strategy": a.model_dump(mode="json")},
         ["strategy"])

    _reg(N_SUP, PhaseName.SUPPLIER,
         lambda: SupplierAgent(wf),
         lambda s: {"requirement": s.get("requirement")},
         lambda a: {"shortlist": a.model_dump(mode="json")},
         ["shortlist"])

    # 比价/合规/合同直接消费候选(短名单)中的库内价目
    _reg(N_CMP, PhaseName.COMPARISON,
         lambda: ComparisonAgent(),
         lambda s: {"requirement": s.get("requirement"),
                    "candidates": (s.get("shortlist") or {}).get("candidates", [])},
         lambda a: {"comparison": a.model_dump(mode="json")},
         ["comparison"])

    _reg(N_CL, PhaseName.COMPLIANCE,
         lambda: ComplianceAgent(rules),
         lambda s: {"requirement": s.get("requirement"),
                    "candidates": (s.get("shortlist") or {}).get("candidates", [])},
         lambda a: {"compliance": a.model_dump(mode="json")},
         ["compliance"])

    _reg(N_CT, PhaseName.CONTRACT,
         lambda: ContractDraftAgent(),
         lambda s: {
             "requirement": s.get("requirement"),
             "comparison": s.get("comparison"),
             "compliance": s.get("compliance"),
             "candidates": (s.get("shortlist") or {}).get("candidates", []),
             "case_id": s.get("case_id"),
         },
         lambda a: {"contract": a.model_dump(mode="json")},
         ["contract"])
    return nodes


# ==========================================================================
# 仲裁节点构造
# ==========================================================================
_ARTIFACT_VIEW: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    PhaseName.REQUIREMENT.value: lambda s: s.get("requirement") or {},
    PhaseName.STRATEGY.value: lambda s: s.get("strategy") or {},
    PhaseName.SUPPLIER.value: lambda s: s.get("shortlist") or {},
    PhaseName.COMPARISON.value: lambda s: s.get("comparison") or {},
    PhaseName.COMPLIANCE.value: lambda s: s.get("compliance") or {},
    PhaseName.CONTRACT.value: lambda s: s.get("contract") or {},
    PhaseName.FINAL.value: lambda s: s.get("final_report") or {},
}


def _make_arbitrator_node(phase: PhaseName, pending_next: str) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def arb(state: Dict[str, Any]) -> Dict[str, Any]:
        agent = ArbitratorAgent()
        record = agent.invoke({
            "phase": phase.value,
            "artifact": _ARTIFACT_VIEW[phase.value](state),
            "context": state,
        })
        updates: Dict[str, Any] = {
            "arbitration": list(state.get("arbitration") or []) + [record.model_dump(mode="json")],
            "pending_next": pending_next,
            "current_phase": phase.value,
        }
        return updates
    return arb


# ==========================================================================
# 路由与收尾
# ==========================================================================
def _router_after_arbitration(state: Dict[str, Any]) -> str:
    """仲裁后路由：proceed->下一节点 / hold->人审 / block 或失败->收尾。"""
    if state.get("status") in ("failed", "blocked"):
        return N_FINISH
    arb = state.get("arbitration") or []
    last = arb[-1] if arb else {}
    if last.get("verdict") == VerdictAction.BLOCK.value:
        return N_FINISH
    if last.get("verdict") == VerdictAction.HOLD.value:
        return N_APPROVE
    return state.get("pending_next") or N_FINISH


def _approval_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """人工审批节点：auto_approve 配置或外部人工决策器。"""
    arb = state.get("arbitration") or []
    hold_records = [r for r in arb if r.get("verdict") == VerdictAction.HOLD.value]
    latest = hold_records[-1] if hold_records else {}
    meta = state.get("meta") or {}
    auto = bool(meta.get("auto_approve", True))
    # 决策器从进程级注册表读取（不进 state，保持 checkpoint 可序列化）
    decider = HUMAN_DECIDERS.get(str(state.get("case_id"))) if not auto else None

    approve = auto
    approver = "系统(自动审批配置)"
    comment = f"仲裁挂起项自动放行：{latest.get('summary', '')}"
    if not auto and callable(decider):
        approve = bool(decider(latest.get("phase", ""), latest.get("summary", "")))
        approver = "人工(外部决策器)"
        comment = "人工审批通过" if approve else "人工审批拒绝"

    record = ApprovalRecord(
        phase=PhaseName(latest.get("phase", PhaseName.APPROVAL.value)),
        decision="auto_approved" if auto else ("approved" if approve else "rejected"),
        approver=approver,
        comment=comment,
    )
    updates: Dict[str, Any] = {
        "approvals": list(state.get("approvals") or []) + [record.model_dump(mode="json")],
    }
    if not approve:
        issues = list(state.get("issues") or [])
        issues.append({"stage": N_APPROVE, "level": "error", "message": "人工审批拒绝挂起事项，流程终止",
                       "at": datetime.now().isoformat(timespec="seconds")})
        updates["issues"] = issues
        updates["status"] = "blocked"
    return updates


def _router_after_approval(state: Dict[str, Any]) -> str:
    if state.get("status") == "blocked":
        return N_FINISH
    return state.get("pending_next") or N_FINISH


def _final_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """收官汇总：生成可读的采购结论报告（业务层）。"""
    requirement = state.get("requirement") or {}
    comparison = state.get("comparison") or {}
    compliance = state.get("compliance") or {}
    contract = state.get("contract") or {}
    strategy = state.get("strategy") or {}
    shortlist = state.get("shortlist") or {}

    approved = compliance.get("approved_supplier_ids") or []
    summary_lines = [
        f"采购需求: {requirement.get('title')}（{requirement.get('category')}，预算 "
        f"{requirement.get('budget_amount') or '未填':,} 元）" if requirement.get("budget_amount") else
        f"采购需求: {requirement.get('title')}（{requirement.get('category')}，预算未填）",
        f"采购策略: {strategy.get('strategy')}（{'; '.join(strategy.get('rationale') or [])[:120]}）",
        f"候选供应商: {len(shortlist.get('candidates') or [])} 家（库内价目随候选提供）",
        f"比价推荐: {(comparison.get('recommended') or {}).get('supplier_name', '-')}",
        f"合规放行: {len(approved)} 家{('，否决 ' + '、'.join(str(x) for x in (compliance.get('rejected') or []))) if compliance.get('rejected') else ''}",
    ]
    if contract.get("supplier_name"):
        summary_lines.append(
            f"定标成交: {contract.get('supplier_name')}，金额 {contract.get('total_amount'):,.2f} 元，"
            f"PO 编号 {contract.get('po_number')}"
        )
    else:
        summary_lines.append("定标成交: 未生成（流程未走到合同阶段）")

    report = {
        "summary": "\n".join(summary_lines),
        "status": state.get("status", "running"),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "stage_counts": {
            phase: sum(1 for h in (state.get("phase_history") or []) if h.get("phase") == phase)
            for phase in {h.get("phase") for h in (state.get("phase_history") or [])}
        },
        "hold_count": sum(1 for r in (state.get("arbitration") or []) if r.get("verdict") == VerdictAction.HOLD.value),
        "warn_count": sum(1 for r in (state.get("arbitration") or [])
                          for c in r.get("checks", []) if c.get("level") == "提示"),
    }
    return {"final_report": report}


def _finish_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """收尾：根据全程裁决与异常最终定状态并补元信息。"""
    issues = state.get("issues") or []
    arb = state.get("arbitration") or []
    blocked = any(r.get("verdict") == VerdictAction.BLOCK.value for r in arb)
    errored = any(i.get("level") == "error" for i in issues)
    status = "blocked" if (blocked or errored) else "done"
    report = dict(state.get("final_report") or {})
    report["status"] = status
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    if blocked:
        blockers = [r.get("summary") for r in arb if r.get("verdict") == VerdictAction.BLOCK.value]
        report["blockers"] = blockers[:3]
        report["summary"] = (report.get("summary", "") + "\n[阻断] " + "；".join(blockers[:2]))
    elif errored:
        report["summary"] = report.get("summary", "") + "\n[异常] 流程因节点错误终止，详见 issues"
    meta = dict(state.get("meta") or {})
    meta["finished_at"] = report["finished_at"]
    return {"status": status, "final_report": report, "meta": meta}


# ==========================================================================
# 主构建入口
# ==========================================================================
def build_pipeline(config: AppConfig | None = None, provider: LLMProvider | None = None):
    """构建并编译 LangGraph 采购流程。返回 (compiled_app, app_meta)。"""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    cfg = config or AppConfig.load()
    if provider is None:
        from procurement_agents.llm import create_provider
        provider = create_provider(cfg)

    builder = StateGraph(WorkflowState)

    # 1) 业务节点
    biz = _build_business_nodes(cfg, provider)
    for name, node in biz.items():
        builder.add_node(name, node)

    # 2) 仲裁节点 + 路由（线性主链：每个业务节点后跟一个仲裁节点）
    chain: List[tuple[str, PhaseName, str]] = [
        (N_REQ, PhaseName.REQUIREMENT, N_STR),
        (N_STR, PhaseName.STRATEGY, N_SUP),
        (N_SUP, PhaseName.SUPPLIER, N_CMP),   # 供应商推荐(含库内价目)后直接比价
        (N_CMP, PhaseName.COMPARISON, N_CL),
        (N_CL, PhaseName.COMPLIANCE, N_CT),
        (N_CT, PhaseName.CONTRACT, N_FIN),
    ]
    for (node_name, phase, nxt) in chain:
        arb_name = f"arb_{node_name}"
        builder.add_node(arb_name, _make_arbitrator_node(phase, nxt))
        builder.add_edge(node_name, arb_name)
        builder.add_conditional_edges(arb_name, _router_after_arbitration)

    # 3) 收官节点
    builder.add_node(N_FIN, _final_node)
    builder.add_node(f"arb_{N_FIN}", _make_arbitrator_node(PhaseName.FINAL, N_FINISH))
    builder.add_edge(N_FIN, f"arb_{N_FIN}")
    builder.add_conditional_edges(f"arb_{N_FIN}", _router_after_arbitration)

    # 4) 人审 + 收尾
    builder.add_node(N_APPROVE, _approval_node)
    builder.add_conditional_edges(N_APPROVE, _router_after_approval)
    builder.add_node(N_FINISH, _finish_node)
    builder.add_edge(N_FINISH, END)

    builder.add_edge(START, N_REQ)

    checkpointer = None
    if cfg.workflow.checkpointer == "memory":
        checkpointer = MemorySaver()
    compiled = builder.compile(checkpointer=checkpointer)
    meta = {
        "provider": provider.name,
        "graph_node_count": len(builder.nodes),
        "checkpointer": cfg.workflow.checkpointer,
    }
    return compiled, meta


def initial_state(case_id: str, request_text: str,
                  wf_config: WorkflowConfig | None = None, **meta_extra: Any) -> Dict[str, Any]:
    """构造工作流初始状态。"""
    wf = wf_config or WorkflowConfig()
    return {
        "case_id": case_id,
        "input": {"request_text": request_text},
        "meta": {
            "auto_approve": wf.auto_approve_holds,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            **meta_extra,
        },
        "status": "running",
        "current_phase": PhaseName.START.value,
        "arbitration": [],
        "approvals": [],
        "issues": [],
        "phase_history": [],
    }
