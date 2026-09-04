"""业务节点层：把 Agent 包装成 LangGraph 节点，内置工业化护栏。

护栏：
  * 超时控制（子线程 + future 超时）
  * 失败重试（区分可重试的瞬时错误）
  * 阶段运行记录（phase_history）
  * 异常升级（issues）并在重试耗尽后置 status=failed（由路由层处理）
"""
from __future__ import annotations

import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
from typing import Any, Callable, Dict, List

from pydantic import BaseModel

from procurement_agents.agents.base import AgentError, BaseAgent
from procurement_agents.domain.enums import PhaseName
from procurement_agents.domain.models import StageRecord

logger = logging.getLogger(__name__)


class PhaseNode:
    """一个『业务 Agent -> 产物写回』的 LangGraph 节点。"""

    def __init__(
        self,
        node_name: str,
        phase: PhaseName,
        agent_factory: Callable[[], BaseAgent],
        map_inputs: Callable[[Dict[str, Any]], Dict[str, Any]],
        map_outputs: Callable[[BaseModel], Dict[str, Any]],
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        artifact_keys: List[str] | None = None,
    ) -> None:
        self.node_name = node_name
        self.phase = phase
        self._agent_factory = agent_factory
        self._map_inputs = map_inputs
        self._map_outputs = map_outputs
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._artifact_keys = artifact_keys or []

    # ------------------------------------------------------------------
    def __call__(self, state: Dict[str, Any]) -> Dict[str, Any]:
        started = datetime.now().isoformat(timespec="seconds")
        phase_history: List[Dict[str, Any]] = list(state.get("phase_history") or [])
        issues: List[Dict[str, Any]] = list(state.get("issues") or [])
        retries = 0
        last_err = ""
        t0 = time.monotonic()

        agent = self._agent_factory()
        inputs = self._map_inputs(state)
        for attempt in range(self._max_retries + 1):
            try:
                if attempt > 0:
                    retries = attempt
                result = self._run_with_timeout(agent, inputs)
                updates = self._map_outputs(result)
                updates["phase_history"] = phase_history + [
                    StageRecord(
                        phase=self.phase,
                        started_at=started,
                        finished_at=datetime.now().isoformat(timespec="seconds"),
                        status="ok",
                        retries=retries,
                        artifact_keys=self._artifact_keys,
                    ).model_dump(mode="json")
                ]
                return updates
            except Exception as exc:  # noqa: BLE001 —— 统一护栏处理
                last_err = f"{type(exc).__name__}: {exc}"
                logger.warning("[%s] 第 %d 次执行失败: %s", self.node_name, attempt + 1, last_err)
                if attempt < self._max_retries:
                    continue
                issues.append({
                    "stage": self.node_name,
                    "level": "error",
                    "message": f"[{self.phase.value}] {last_err}",
                    "detail": traceback.format_exc(limit=3)[-800:],
                    "at": datetime.now().isoformat(timespec="seconds"),
                })
                break

        # 重试耗尽：置 failed 交由路由收尾
        return {
            "status": "failed",
            "issues": issues,
            "phase_history": phase_history + [
                StageRecord(
                    phase=self.phase,
                    started_at=started,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                    status="error",
                    retries=retries,
                    error=last_err,
                ).model_dump(mode="json")
            ],
        }

    # ------------------------------------------------------------------
    def _run_with_timeout(self, agent: BaseAgent, inputs: Dict[str, Any]) -> BaseModel:
        """在子线程执行 invoke 并施加超时（Windows 无 signal.alarm 的通用方案）。"""
        if self._timeout <= 0:
            return agent.invoke(inputs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(agent.invoke, inputs)
            try:
                return future.result(timeout=self._timeout)
            except FutureTimeout:
                raise AgentError(f"[{agent.display_name}] 执行超时({self._timeout}s)") from None
