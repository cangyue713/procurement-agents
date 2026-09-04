"""高层运行入口（Runner）：一次调用跑完整条采购流程。

流程（无询价/报价环节，价格与供应商介绍同源来自 suppliers.csv）：
    需求理解 -> 采购策略判定 -> 供应商推荐 -> 比价分析 -> 合规审查 -> 合同草稿

用法：
    runner = ProcurementRunner()                       # 读 config/app.yaml，默认 Mock 模型
    result = runner.run(request_file="examples/input/request.txt")
    print(result.summary_text)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from procurement_agents.config import AppConfig
from procurement_agents.llm.base import LLMProvider
from procurement_agents.pipeline.graph import HUMAN_DECIDERS, build_pipeline, initial_state
from procurement_agents.pipeline.tracing import render_markdown, save_trace

logger = logging.getLogger(__name__)


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

    # ---- 输出 -------------------------------------------------------------
    def markdown_report(self) -> str:
        """渲染可读的中文 Markdown 报告。"""
        return render_markdown(self.state)

    def save_report(self, output_dir: str | Path | None = None) -> Path:
        """保存 Markdown 报告与 JSON trace，返回报告路径。"""
        out_dir = Path(output_dir) if output_dir else Path(self.config.workflow.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = self.state.get("meta", {}).get("finished_at", "")[:19].replace(":", "").replace("-", "").replace(" ", "-")
        md_path = out_dir / f"report_{self.case_id}_{ts or 'now'}.md"
        md_path.write_text(self.markdown_report(), encoding="utf-8")
        self.trace_path = save_trace(self.state, out_dir)
        return md_path


class ProcurementRunner:
    """采购多Agent流程运行器。"""

    def __init__(self, config: AppConfig | None = None, provider: LLMProvider | None = None) -> None:
        self.config = config or AppConfig.load()
        self.app, self.graph_meta = build_pipeline(self.config, provider)

    # ------------------------------------------------------------------
    def run(
        self,
        request_text: Optional[str] = None,
        case_id: Optional[str] = None,
        auto_approve: Optional[bool] = None,
        human_decider: Optional[Callable[[str, str], bool]] = None,
        request_file: Optional[str | Path] = None,
    ) -> ProcurementRun:
        """执行一次完整采购流程（无询价/报价环节，价格取自供应商库内价目）。

        参数:
            request_text: 采购需求自由文本（自然语言）
            request_file: 需求文本的 txt 文件路径（与 request_text 二选一；
                          项目直接读取该文件内容作为需求输入，UTF-8 容忍 BOM）
            case_id:      案例号（默认自动生成）
            auto_approve: 仲裁 hold 是否自动放行(默认取配置)
            human_decider: 人工决策回调 (phase, summary) -> bool（auto_approve=False 时启用）
        """
        import uuid
        from datetime import datetime

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
        state = initial_state(
            case_id=case_id,
            request_text=request_text.strip(),
            wf_config=wf,
            provider=self.graph_meta.get("provider", "mock"),
        )
        # 把审批策略注入 meta（approval 节点读取）；决策回调走注册表以保持状态可序列化
        state["meta"]["auto_approve"] = auto
        if human_decider is not None:
            state["meta"]["auto_approve"] = False
            state["meta"]["needs_human"] = True
            HUMAN_DECIDERS[case_id] = human_decider

        run_config = {"configurable": {"thread_id": case_id}}
        try:
            final_state = self.app.invoke(state, config=run_config)
        finally:
            HUMAN_DECIDERS.pop(case_id, None)
        logger.info("采购流程 %s 结束：status=%s", case_id, final_state.get("status"))
        return ProcurementRun(final_state, self.config)
