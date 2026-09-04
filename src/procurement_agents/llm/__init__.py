"""LLM 层导出与 Provider 工厂。"""
from __future__ import annotations

import logging
import os

from procurement_agents.config import AppConfig
from procurement_agents.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    TASK_PARSE_REQUIREMENT,
)

logger = logging.getLogger(__name__)


def create_provider(config: AppConfig | None = None) -> LLMProvider:
    """按配置创建 Provider。默认 mock，可切换 deepseek / openai_compat。"""
    from procurement_agents.llm.mock import MockProvider
    from procurement_agents.llm.openai_compat import OpenAICompatProvider

    cfg = config or AppConfig.load()
    kind = (cfg.llm.provider or "mock").lower()
    if kind == "mock":
        return MockProvider()

    api_key = os.getenv(cfg.llm.api_key_env) or os.getenv("DEEPSEEK_API_KEY") or ""
    base_url = os.getenv("OPENAI_COMPAT_BASE_URL", cfg.llm.base_url)
    model = os.getenv("OPENAI_COMPAT_MODEL", cfg.llm.model)
    if not api_key:
        logger.warning(
            "未配置 API Key(环境变量 %s)。切换回 MockProvider；请在 .env 中配置后重试真实模型。",
            cfg.llm.api_key_env,
        )
        return MockProvider()
    logger.info("使用真实模型 Provider: %s model=%s", kind, model)
    return OpenAICompatProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=cfg.llm.temperature,
        timeout_seconds=cfg.llm.timeout_seconds,
    )


__all__ = [
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "create_provider",
    "TASK_PARSE_REQUIREMENT",
]
