"""LangGraph 工作流状态定义。

状态内全部为 JSON 安全结构（dict/list/基本类型），
因此天然支持 checkpoint（MemorySaver）与人审暂停/恢复。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class WorkflowState(TypedDict, total=False):
    """采购全流程状态（LangGraph StateSchema）。"""

    # ---- 输入与元信息 ----
    case_id: str                      # 案例号（贯穿编号 PO）
    input: Dict[str, Any]             # 原始输入：request_text（需求来自 txt 或字符串）/ buyer
    meta: Dict[str, Any]              # provider、config 快照、审批策略

    # ---- 运行控制 ----
    status: str                       # running | done | blocked | failed | needs_input
    current_phase: str                # 当前阶段(PhaseName 值)
    pending_next: str                 # hold 被人工放行后应去的节点名

    # ---- 各阶段产物(JSON 安全) ----
    requirement: Optional[Dict[str, Any]]
    strategy: Optional[Dict[str, Any]]
    shortlist: Optional[Dict[str, Any]]     # 候选含库内价目(price_items 等)
    comparison: Optional[Dict[str, Any]]
    compliance: Optional[Dict[str, Any]]
    contract: Optional[Dict[str, Any]]
    final_report: Optional[Dict[str, Any]]

    # ---- 全程记录 ----
    arbitration: List[Dict[str, Any]]   # ArbitrationRecord dumps（仲裁全程裁决留痕）
    approvals: List[Dict[str, Any]]     # ApprovalRecord dumps（人审/自动放行留痕）
    issues: List[Dict[str, Any]]        # 升级问题/异常留痕
    phase_history: List[Dict[str, Any]]  # StageRecord dumps（每个节点运行记录）
