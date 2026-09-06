"""高层运行入口（Runner）：一次调用跑完整条采购流程（含 P1 服务化语义）。

流程（无询价/报价环节，价格与供应商介绍同源来自 suppliers.csv）：
    需求理解 -> 采购策略判定 -> 供应商推荐 -> 比价分析 -> 合规审查 -> 合同草稿

P1 起支持三种运行语义：
  * 全自动（auto_approve=True，默认）：仲裁 hold 自动放行留痕，一次跑完；
  * 挂起等待（auto_approve=False）：遇到首个待人工决策的 hold 即停在审批点，
    状态 status=needs_input 并持久化到 checkpointer；调用方随后以 resume() 提供
    决策（approved / rejected）续跑，进程重启后可基于 SqliteSaver 恢复；
  * 状态查询（get_state / list_cases）：从 checkpointer 读取最新快照。

用法：
    runner = ProcurementRunner()                       # 读 config/app.yaml
    result = runner.run(request_file="examples/input/request.txt")
    print(result.summary_text)
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from procurement_agents.config import AppConfig, WorkflowConfig
from procurement_agents.llm.base import LLMProvider
from procurement_agents.pipeline.graph import build_pipeline, initial_state
from procurement_agents.pipeline.tracing import render_markdown, save_trace

logger = logging.getLogger(__name__)

_TERMINAL = ("done", "blocked", "failed")


class ProcurementRun:
    """一次采购流程的运行结果（最终状态快照 + 便捷访问）。"""

    def __init__(self, state: Dict[str, Any], config: AppConfig) -> None:
        self.state = state
        self.config = config
        self.trace_path: Optional[Path] = None

    # ---- 状态快捷访问 -----------------------------------------------------
    @property
    def status(self) -> str:
        return str(self.state.get("status", "unknown"))

    @property
    def is_done(self) -> bool:
        return self.status == "done"

    @property
    def needs_input(self) -> bool:
        """流程停在人审点、等待人工决策。"""
        return self.status == "needs_input"

    @property
    def case_id(self) -> str:
        return str(self.state.get("case_id", ""))

    @property
    def summary_text(self) -> str:
        return str((self.state.get("final_report") or {}).get("summary", ""))

    @property
    def arbitration_records(self) -> List[Dict[str, Any]]:
        return self.state.get("arbitration") or []

    @property
    def issues(self) -> List[Dict[str, Any]]:
        return self.state.get("issues") or []

    @property
    def contract(self) -> Dict[str, Any]:
        return self.state.get("contract") or {}

    # ---- 人审信息 ---------------------------------------------------------
    @property
    def pending_approval(self) -> Optional[Dict[str, Any]]:
        """当前等待人工决策的仲裁挂起项（无则 None）。"""
        if not self.needs_input:
            return None
        arb = self.arbitration_records
        holds = [r for r in arb if r.get("verdict") == "hold"]
        return holds[-1] if holds else None

    # ---- 输出 -------------------------------------------------------------
    def markdown_report(self) -> str:
        """渲染可读的中文 Markdown 报告。"""
        return render_markdown(self.state)

    def save_report(self, output_dir: str | Path | None = None) -> Path:
        """保存该 case 的报告与追踪到『case 目录』（P2-F 目录化存储）。

        产物路径：<output_dir>/<case_id>/report.md 与 trace_*.json ——
        每个 case 的产物相互隔离，便于按 case 归档/批量计划汇总。
        返回 Markdown 报告路径。
        """
        base = Path(output_dir) if output_dir else Path(self.config.workflow.output_dir)
        case_dir = base / str(self.case_id or "case")
        case_dir.mkdir(parents=True, exist_ok=True)
        md_path = case_dir / "report.md"
        md_path.write_text(self.markdown_report(), encoding="utf-8")
        self.trace_path = save_trace(self.state, case_dir)
        return md_path


def _resolve_sqlite_path(path: str) -> Path:
    """sqlite 文件相对项目根解析（配置中常写 outputs/cases.sqlite）。"""
    from procurement_agents.config import PROJECT_ROOT

    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


class ProcurementRunner:
    """采购多Agent流程运行器（checkpointer 可内存 / SqliteSaver 持久化）。"""

    def __init__(
        self,
        config: AppConfig | None = None,
        provider: LLMProvider | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.config = config or AppConfig.load()
        # checkpointer：显式传入优先；否则按配置 memory / sqlite 构建
        self._owns_checkpointer = checkpointer is None
        self._checkpointer = checkpointer if checkpointer is not None else self._make_checkpointer(self.config.workflow)
        self._lock = threading.RLock()  # 同一实例复用时串行化 invoke/resume
        self.app, self.graph_meta = build_pipeline(
            self.config, provider, checkpointer=self._checkpointer
        )

    # ------------------------------------------------------------------
    def _make_checkpointer(self, wf: WorkflowConfig) -> Any:
        if wf.checkpointer == "sqlite":
            import sqlite3

            from langgraph.checkpoint.sqlite import SqliteSaver

            db = _resolve_sqlite_path(wf.sqlite_path)
            db.parent.mkdir(parents=True, exist_ok=True)
            # 显式持有连接（from_conn_string 的上下文生命周期不适合长期 Runner）
            conn = sqlite3.connect(str(db), check_same_thread=False)
            return SqliteSaver(conn)
        if wf.checkpointer == "memory":
            from langgraph.checkpoint.memory import MemorySaver

            return MemorySaver()
        return None  # none：无断点

    def close(self) -> None:
        """释放自建 checkpointer 持有的连接（SqliteSaver 场景）。"""
        if self._owns_checkpointer and self._checkpointer is not None:
            conn = getattr(self._checkpointer, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # 关闭失败可忽略（进程退出兜底）
                    pass
        self._checkpointer = None

    def __enter__(self) -> "ProcurementRunner":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    def _thread_config(self, case_id: str) -> Dict[str, Any]:
        return {"configurable": {"thread_id": case_id}}

    def run(
        self,
        request_text: Optional[str] = None,
        case_id: Optional[str] = None,
        auto_approve: Optional[bool] = None,
        request_file: Optional[str | Path] = None,
    ) -> ProcurementRun:
        """执行一次采购流程。

        参数:
            request_text: 采购需求自由文本（自然语言）
            request_file: 需求文本的 txt 文件路径（与 request_text 二选一；
                          项目直接读取该文件内容作为需求输入，UTF-8 容忍 BOM）
            case_id:      案例号（默认自动生成）
            auto_approve: 仲裁 hold 是否自动放行；False 时流程停在首个待审批挂起点
                          （status=needs_input），需随后调用 resume() 提供决策
        """
        import uuid

        # 需求文本来源：request_file 优先（项目读 txt），否则 request_text
        if request_file is not None:
            p = Path(request_file)
            if not p.exists():
                raise FileNotFoundError(f"需求输入文件不存在: {p}")
            request_text = p.read_text(encoding="utf-8-sig").strip()
        if not request_text or not request_text.strip():
            raise ValueError("缺少采购需求输入：请传 request_text 或 request_file")

        case_id = case_id or f"PC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        wf = self.config.workflow
        auto = wf.auto_approve_holds if auto_approve is None else auto_approve
        if not auto and self._checkpointer is None:
            raise ValueError("auto_approve=False 需要 checkpointer（memory/sqlite）支撑挂起语义，"
                             "请配置 workflow.checkpointer")
        state = initial_state(
            case_id=case_id,
            request_text=request_text.strip(),
            wf_config=wf,
            provider=self.graph_meta.get("provider", "mock"),
        )
        state["meta"]["auto_approve"] = auto  # 审批策略注入 meta（审批节点读取）

        config = self._thread_config(case_id)
        with self._lock:
            final_state = self.app.invoke(state, config=config)
        final_state = self._normalize(final_state)
        logger.info("采购流程 %s 结束：status=%s", case_id, final_state.get("status"))
        return ProcurementRun(final_state, self.config)

    # ------------------------------------------------------------------
    def resume(
        self,
        case_id: str,
        approved: bool,
        approver: str = "人工(审批接口)",
        comment: str = "",
        approver_id: str = "",
        attachments: Optional[List[Dict[str, Any]]] = None,
        source: str = "api",
    ) -> ProcurementRun:
        """为挂起中的 case 提供人工决策并续跑（含多次连续挂起）。

        基于同一 thread_id 的 checkpointer 从断点恢复：
        首次 invoke 带 Command(resume=...) 将决策交给 interrupt() 处的审批节点。
        P2：决策携带审批人身份（approver_id）与附件元数据（attachments，含 sha256），
        由审批节点写入 ApprovalRecord 审计单元。
        """
        from langgraph.types import Command

        decision: Dict[str, Any] = {
            "approved": approved,
            "approver": approver,
            "comment": comment,
            "approver_id": approver_id,
            "attachments": attachments or [],
            "source": source,
        }
        config = self._thread_config(case_id)
        with self._lock:
            out = self.app.invoke(Command(resume=decision), config=config)
        out = self._normalize(out)
        logger.info("case %s 审批决策 approved=%s -> status=%s", case_id, approved, out.get("status"))
        return ProcurementRun(out, self.config)

    # ------------------------------------------------------------------
    def get_state(self, case_id: str) -> Optional[Dict[str, Any]]:
        """从 checkpointer 读取 case 最新状态快照（跨进程可恢复后查询）。

        未找到返回 None；memory checkpointer 下需同一 Runner 实例。
        挂起判定：checkpoint 的 next 非空（流程停在待恢复节点）即 needs_input。
        """
        if self._checkpointer is None:
            return None
        try:
            snap = self.app.get_state(self._thread_config(case_id))
        except Exception as exc:  # 无该 thread 等
            logger.debug("get_state(%s) 失败: %s", case_id, exc)
            return None
        if snap is None or not snap.values:
            return None
        return self._normalize(dict(snap.values), next_nodes=list(snap.next or ()))

    def list_cases(self, limit: int = 50) -> List[Dict[str, Any]]:
        """列出 checkpointer 中存在的 case（含终态），按最近优先。"""
        if self._checkpointer is None:
            return []
        try:
            snapshots = list(self._checkpointer.list(None, limit=limit))
        except Exception as exc:  # 不同 saver 支持度差异
            logger.debug("list_cases 失败: %s", exc)
            return []
        seen: Dict[str, Dict[str, Any]] = {}
        for snap in snapshots:
            values = snap.values or {}
            cid = str(values.get("case_id") or "")
            if not cid or cid in seen:
                continue
            seen[cid] = self._normalize(dict(values))
        return list(seen.values())[:limit]

    # ------------------------------------------------------------------
    @staticmethod
    def _normalize(state: Dict[str, Any], next_nodes: Optional[List[str]] = None) -> Dict[str, Any]:
        """把 LangGraph 返回状态归一为对外契约：
        - 存在 __interrupt__（挂了但未阻塞/结束）或 checkpoint 的 next 非空
          -> status=needs_input
        - 移除 LangGraph 内部键，保持 JSON 安全。
        """
        out = {k: v for k, v in state.items() if not k.startswith("__")}
        interrupted = bool(state.get("__interrupt__")) or bool(next_nodes)
        status = str(out.get("status", "running"))
        if interrupted and status not in _TERMINAL:
            out["status"] = "needs_input"
        return out
