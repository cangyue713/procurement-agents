"""Agent 基类与公共异常。

设计约定（“分工”的契约）：
  * 每个业务 Agent 是一个纯领域组件：`invoke(inputs: dict) -> pydantic Artifact`；
    不感知 LangGraph，便于单测与复用。
  * 输入一律为 JSON 安全 dict（即上一阶段产物或自由文本）；
    输出一律为 pydantic Artifact（可 model_dump(mode='json') 写回状态）。
  * 节点层负责：取数 -> invoke -> 序列化 -> 记录，以及重试/超时/仲裁挂钩。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

from pydantic import BaseModel

from procurement_agents.domain.enums import PhaseName


class AgentError(Exception):
    """Agent 业务执行失败（会被节点层捕获并触发重试/降级）。"""


class BaseAgent(ABC):
    """业务 Agent 抽象基类。"""

    #: 阶段名(见 PhaseName)，子类必须覆盖
    phase: PhaseName = PhaseName.START
    #: 人读名称
    display_name: str = ""
    #: 职责说明
    description: str = ""

    @abstractmethod
    def invoke(self, inputs: Dict[str, Any]) -> BaseModel:
        """执行阶段任务：inputs -> Artifact。"""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Agent:{self.display_name or self.__class__.__name__}>"


def require_keys(inputs: Dict[str, Any], keys: str | Any, *, agent: str) -> None:
    """输入契约校验：缺失即抛 AgentError（触发重试/仲裁拦截）。"""
    keys = [keys] if isinstance(keys, str) else list(keys)
    missing = [k for k in keys if inputs.get(k) is None]
    if missing:
        raise AgentError(f"[{agent}] 缺少必要输入: {', '.join(missing)}")
