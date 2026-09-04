"""OpenAICompatProvider：对接 DeepSeek / OpenAI / Ollama 等任意兼容接口。

同一 Agent 代码在 mock 与真实模型间零改动切换：
  * parse_requirement 任务被翻译为 system+user 提示词（仅需求理解需要 LLM；
    价格与供应商数据来自 suppliers.csv，不经过模型）；
  * 请求 response_format=json_object，并对结果做 schema 级校验与重试。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from procurement_agents.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    extract_json_block,
    TASK_PARSE_REQUIREMENT,
)

logger = logging.getLogger(__name__)

# 任务 -> 提示模板
TASK_PROMPTS: Dict[str, str] = {
    TASK_PARSE_REQUIREMENT: (
        "你是一名资深采购需求分析师。请把采购需求文本解析为 JSON，键必须为："
        "title(字符串,需求标题), category(从[IT设备,办公设备,工业原料,生产设备,物流服务,专业服务,通用物资]选), "
        "urgency(从[一般,紧急,特急]选), "
        "items(数组,元素含 description(物品与规格), quantity(数字), uom(单位)), "
        "budget_amount(数字或null,人民币元,注意万元换算), delivery_days(数字或null,交付天数), "
        "quality_requirements(字符串数组), usage_scene(字符串), missing_fields(字符串数组,缺失关键字段), notes(字符串)。"
        "只输出 JSON。"
    ),
}


class OpenAICompatProvider(LLMProvider):
    """通用 OpenAI 兼容 Provider。"""

    name = "openai_compat"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        temperature: float = 0.0,
        timeout_seconds: float = 90.0,
        max_retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._temperature = temperature
        self._timeout = timeout_seconds
        self._max_retries = max_retries

    # -- 实现 -------------------------------------------------------------
    def complete(self, request: LLMRequest) -> LLMResponse:
        from openai import OpenAI  # 懒加载，未安装 openai 时 mock 模式不受影响

        client = OpenAI(api_key=self._api_key, base_url=self._base_url, timeout=self._timeout)
        try:
            return self._complete_with_client(client, request)
        finally:
            client.close()  # 显式释放连接，避免 ResourceWarning

    def _complete_with_client(self, client: Any, request: LLMRequest) -> LLMResponse:
        system = TASK_PROMPTS.get(request.task, "")
        user = request.text
        if request.hints:
            user += "\n\n辅助信息(JSON): " + json.dumps(request.hints, ensure_ascii=False)
        last_err = ""
        for attempt in range(self._max_retries + 1):
            try:
                resp = client.chat.completions.create(
                    model=self._model,
                    temperature=self._temperature,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                raw = resp.choices[0].message.content or "{}"
                data = extract_json_block(raw)
                data = self._normalize(request, data)
                return LLMResponse(data=data, provider=self.name, raw_text=raw, retries=attempt)
            except Exception as exc:  # 调用失败记录后进入重试
                last_err = str(exc)
                logger.warning("LLM 调用第 %d 次失败: %s", attempt + 1, last_err)
        return LLMResponse.failed(self.name, f"调用 {self._model} 失败: {last_err}")

    # -- schema 级修补（宽容真实模型的字段偏差） -----------------------------
    @staticmethod
    def _normalize(request: LLMRequest, data: Dict[str, Any]) -> Dict[str, Any]:
        if request.task == TASK_PARSE_REQUIREMENT:
            # 显式 None/缺失统一回退（setdefault 无法处理模型显式返回 None 的情况）
            for key, fallback in (("title", ""), ("category", "通用物资"), ("urgency", "一般")):
                if not data.get(key):
                    data[key] = fallback
            items = data.get("items")
            if not isinstance(items, list):
                items = []
            data["items"] = [
                {
                    "description": str(it.get("description", "")),
                    "quantity": float(it.get("quantity", 0)),
                    "uom": str(it.get("uom", "台")),
                }
                for it in items
                if isinstance(it, dict)
            ]
            for k in ("budget_amount", "delivery_days"):
                if k in data and data[k] is not None:
                    try:
                        data[k] = float(data[k])
                    except (TypeError, ValueError):
                        data[k] = None
            for key, fallback in (("quality_requirements", []), ("usage_scene", ""),
                                  ("missing_fields", []), ("notes", "")):
                if not data.get(key):
                    data[key] = fallback
        return data
