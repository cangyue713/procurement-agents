"""LLM 层：统一的可插拔 Provider 协议。

设计动机：
  采购流程要求结果可审计、可复现。我们把 Agent 与"模型"解耦：
  - Agent 只声明要完成的"任务"(task)与输入文本/提示(hints)；
  - Provider 负责把任务变成结果：
      * MockProvider     —— 内置中文规则引擎，离线、确定、可测（默认）；
      * OpenAICompatProvider —— 调 DeepSeek/OpenAI 兼容接口，走 JSON 输出约束。
  这样切换 Provider 无需改动任何 Agent 代码。
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# ----------------------- 任务常量 -----------------------
TASK_PARSE_REQUIREMENT = "parse_requirement"  # 自由文本需求 -> 结构化需求


@dataclass
class LLMRequest:
    """一次模型调用请求。"""

    task: str
    text: str = ""
    hints: Dict[str, Any] = field(default_factory=dict)
    # 期望输出的 JSON 结构描述（给人话提示词，或给 schema 名）
    expect_schema: str = ""


@dataclass
class LLMResponse:
    """模型返回（已转 JSON 安全 dict）。"""

    data: Dict[str, Any]
    provider: str = ""
    ok: bool = True
    error: str = ""
    raw_text: str = ""
    retries: int = 0

    @classmethod
    def failed(cls, provider: str, error: str) -> "LLMResponse":
        return cls(data={}, provider=provider, ok=False, error=error)


class LLMProvider(ABC):
    """可插拔模型服务抽象。"""

    name: str = "base"

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        """执行一次任务并返回 JSON 安全结果。"""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LLMProvider:{self.name}>"


# ----------------------- 通用小工具 -----------------------
def json_compatible(value: Any) -> Any:
    """尽力把任意对象转成 JSON 安全结构。"""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, dict):
        return {str(k): json_compatible(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_compatible(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def extract_json_block(text: str) -> Dict[str, Any]:
    """从模型文本中稳健提取首个 JSON 对象/数组（容忍 ```json 围栏等）。"""
    cleaned = re.sub(r"```(?:json)?", "", text)
    # 先尝试整体解析
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else {"data": obj}
    except Exception:
        pass
    # 再尝试截取首尾大括号
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except Exception:
            pass
    raise ValueError(f"无法从模型输出中解析 JSON: {text[:200]}")
