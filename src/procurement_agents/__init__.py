"""供应链/采购流程自动化 多Agent项目。

业务链路：
    需求理解 -> 采购策略判定 -> 供应商推荐 -> 询价/报价解析
  -> 比价分析 -> 合规审查 -> PO/合同草稿生成
外加一个 仲裁 Agent(Arbitrator) 全程监控与裁决。

工程形态：LangGraph 状态机编排 + pydantic 领域模型 + 可插拔 LLM Provider
（默认 Mock 规则引擎，可切换 DeepSeek / OpenAI 兼容真实模型）。
"""

__version__ = "0.1.0"

from procurement_agents.config import AppConfig  # noqa: F401
