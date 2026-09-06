"""LLM 契约样本集（P2-E）：统一的『需求解析』测试/录制输入。

供三处复用：
  * tools/record_provider.py —— 用真实 Provider（DeepSeek/OpenAI 兼容）录制输出，
    生成 tests/fixtures/llm/*.json 契约夹具；
  * tests/test_llm_contract.py —— 对录制夹具做结构契约/语义 golden 断言；
  * 需要『同一批输入』做 mock ↔ 真实对照的地方。

样本覆盖典型形态：万元预算换算 / 缺预算缺交期 / 中文数量紧跟描述 /
多物品多品类 / 紧急度 / 口语化需求 / 资质要求 / 金额大数。
"""
from __future__ import annotations

from typing import Dict, List

# 每条样本给出强语义锚点（契约测试按需断言；宽松不脆）
SAMPLE_REQUESTS: List[str] = [
    # demo 主剧本：万元预算 + 交期 + ISO 资质 + 多物品
    "紧急采购 10 台工业级交换机与 2 台 2U 机架式服务器，预算 280 万，25 天内交付，"
    "需通过 ISO9001 认证，含 3 台会议平板。",
    # 单物品 + 中文数量紧跟描述 + 万元预算 + 交期
    "采购 10 台工业级交换机，预算 20 万元，要求 15 天内交付。",
    # 缺预算、缺交期（触发 missing_fields）
    "采购 5 台激光打印机。",
    # 办公品类 + 明确预算与质保倾向
    "采购 20 把人体工学办公椅和 6 台会议平板，预算 8 万元，希望 10 天到货，品牌不限。",
    # 紧急 + 指定品牌/单一来源特征（需保留在 notes/title 供策略层识别）
    "特急采购指定品牌华智的原厂专用变频器 3 台，独家供应，越快越好，预算 50 万。",
    # 金额大数（逗号分隔）与工业原料
    "采购 5 吨铝合金型材(6063)，预算 300,000 元，20 天内交付。",
    # 口语化、多子句需求
    "我们想买 5 台空调，大概 3 万块，能快点送货吗，最好带安装。",
    # 专业服务类 + 预算缺失但交期明确
    # 注：『服务 8 项』句法超出 mock 引擎能力（README 已声明的边界），
    # 契约只锚定品类；真实模型录制后仍可校验结构契约。
    "需要第三方检测认证服务 8 项，30 天内完成并出具报告。",
]

# 每条样本的『语义锚点』：只做宽松断言，避免把合理模型偏差当回归
SEMANTIC_ANCHORS: Dict[str, Dict[str, object]] = {
    SAMPLE_REQUESTS[0]: {"category": "IT设备", "min_items": 3, "budget": 2_800_000.0},
    SAMPLE_REQUESTS[1]: {"category": "IT设备", "min_items": 1, "budget": 200_000.0},
    SAMPLE_REQUESTS[2]: {"budget_none": True, "min_items": 1},
    SAMPLE_REQUESTS[3]: {"min_items": 2, "budget": 80_000.0},
    SAMPLE_REQUESTS[4]: {"min_items": 1},
    SAMPLE_REQUESTS[5]: {"min_items": 1, "budget": 300_000.0},
    SAMPLE_REQUESTS[6]: {"min_items": 1},
    SAMPLE_REQUESTS[7]: {"category": "专业服务"},
}

# 归一化契约：解析结果必须具备的键（缺失/为 None 都会触发契约失败）
REQUIRED_KEYS = ("title", "category", "urgency", "items", "budget_amount",
                 "delivery_days", "quality_requirements", "usage_scene",
                 "missing_fields", "notes")
VALID_CATEGORIES = ("IT设备", "办公设备", "工业原料", "生产设备", "物流服务", "专业服务", "通用物资")
VALID_URGENCIES = ("一般", "紧急", "特急")
