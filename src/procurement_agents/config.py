"""应用配置：读取 config/app.yaml 与 .env，向全项目提供参数。

配置加载优先级：yaml 默认值 <- .env 环境变量（LLM 密钥等）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import dotenv
import yaml

# 项目根目录 = <根>/src/procurement_agents/config.py -> parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "app.yaml"


@dataclass
class WorkflowConfig:
    """流程编排参数。"""

    max_agent_retries: int = 2            # 单个 Agent 节点失败最大重试次数
    agent_timeout_seconds: float = 60.0   # 单个 Agent 节点超时
    auto_approve_holds: bool = True       # 仲裁 hold(人审) 是否自动放行；False 则停在审批点
    checkpointer: str = "memory"          # memory | none
    shortlist_size: int = 3               # 供应商短名单规模
    tracing: bool = True                  # 是否产出结构化运行追踪
    output_dir: str = "outputs"           # 追踪/产物输出目录


@dataclass
class LLMConfig:
    """LLM Provider 配置。"""

    provider: str = "mock"                # mock | deepseek | openai_compat
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com/v1"
    api_key_env: str = "DEEPSEEK_API_KEY"  # 从环境变量读取密钥
    temperature: float = 0.0
    timeout_seconds: float = 90.0


@dataclass
class RuleConfig:
    """业务规则阈值（采购策略/合规判断用）。"""

    direct_purchase_max: float = 50_000.0    # 小于等于 → 直接采购
    rfq_max: float = 1_000_000.0             # 大于 → 招标（保留兼容）
    tender_threshold: float = 1_000_000.0    # 公开/邀请招标门槛
    invite_bid_min: int = 3                  # 邀请招标最少邀请家数
    max_delivery_days: int = 365
    mandatory_fields: List[str] = field(default_factory=lambda: ["title", "items", "budget_amount", "delivery_days"])


@dataclass
class AppConfig:
    """聚合配置对象。"""

    app_name: str = "procurement-agents"
    language: str = "zh-CN"
    default_currency: str = "CNY"
    llm: LLMConfig = field(default_factory=LLMConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    rules: RuleConfig = field(default_factory=RuleConfig)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str | None = None) -> "AppConfig":
        """从 YAML 加载；文件缺失时使用内置默认值。"""
        dotenv.load_dotenv(PROJECT_ROOT / ".env")
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        data: Dict[str, Any] = {}
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

        def _sec(key: str) -> Dict[str, Any]:
            v = data.get(key) or {}
            return v if isinstance(v, dict) else {}

        llm_cfg = LLMConfig(**_only(_sec("llm"), LLMConfig))
        wf_cfg = WorkflowConfig(**_only(_sec("workflow"), WorkflowConfig))
        rule_cfg = RuleConfig(**_only(_sec("rules"), RuleConfig))
        return cls(
            app_name=str(data.get("app", {}).get("name", "procurement-agents")),
            language=str(data.get("app", {}).get("language", "zh-CN")),
            default_currency=str(data.get("app", {}).get("default_currency", "CNY")),
            llm=llm_cfg,
            workflow=wf_cfg,
            rules=rule_cfg,
            raw=data,
        )


def _only(data: Dict[str, Any], dc_type: type) -> Dict[str, Any]:
    """过滤出 dataclass 声明过的字段，避免未知键报错。"""
    fields = {f for f in dc_type.__dataclass_fields__}
    return {k: v for k, v in data.items() if k in fields}
