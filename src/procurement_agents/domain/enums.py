"""领域层：业务枚举定义。"""
from __future__ import annotations

from enum import Enum


class ProductCategory(str, Enum):
    """采购品类别 —— 决定供应商匹配范围与策略规则权重。"""

    IT_EQUIPMENT = "IT设备"
    OFFICE = "办公设备"
    INDUSTRIAL_MATERIAL = "工业原料"
    MACHINERY = "生产设备"
    LOGISTICS = "物流服务"
    SERVICE = "专业服务"
    GENERAL = "通用物资"

    @classmethod
    def guess_from_text(cls, text: str) -> "ProductCategory":
        """根据需求文本关键词粗判品类（供需求理解 Agent 兜底）。"""
        pairs = [
            (cls.IT_EQUIPMENT, ("服务器", "交换机", "存储", "工控机", "电脑", "笔记本", "软件", "防火墙", "GPU")),
            (cls.OFFICE, ("打印机", "办公椅", "办公桌", "复印机", "投影", "文具", "碎纸机")),
            (cls.INDUSTRIAL_MATERIAL, ("钢材", "铝锭", "塑料", "树脂", "焊条", "铜排", "面料", "原料", "化工")),
            (cls.MACHINERY, ("机床", "数控", "注塑机", "压铸机", "机器人", "产线", "包装机", "空压机")),
            (cls.LOGISTICS, ("运输", "仓储", "物流", "报关", "快递", "配送")),
            (cls.SERVICE, ("咨询", "审计", "翻译", "外包", "维保", "维修", "检测", "认证", "培训")),
        ]
        for category, keywords in pairs:
            if any(k in text for k in keywords):
                return category
        return cls.GENERAL


class UrgencyLevel(str, Enum):
    """需求紧急程度。"""

    NORMAL = "一般"
    URGENT = "紧急"
    CRITICAL = "特急"


class ProcurementStrategy(str, Enum):
    """采购策略类型。"""

    DIRECT_PURCHASE = "直接采购"          # 小额/框架内直采
    FRAMEWORK = "框架协议下单"            # 已有框架协议内下单
    RFQ = "询比价"                        # 常规询比价（主流）
    INVITED_TENDER = "邀请招标"           # 金额较大、邀请3家以上
    OPEN_TENDER = "公开招标"              # 大额/法定必招
    SINGLE_SOURCE = "单一来源采购"         # 独家/专利/紧急唯一供应


class SupplierRiskLevel(str, Enum):
    """供应商风险等级。"""

    LOW = "低"
    MEDIUM = "中"
    HIGH = "高"


class CheckLevel(str, Enum):
    """检查/校验级别（合规、仲裁通用）。"""

    PASS = "通过"
    WARN = "提示"
    FAIL = "不通过"


class VerdictAction(str, Enum):
    """仲裁 Agent 对某一阶段产物的裁决动作。"""

    PROCEED = "proceed"          # 放行，进入下一阶段
    HOLD = "hold"                # 挂起，需要人工确认（人审点）
    BLOCK = "block"              # 阻断，终止流程（不可自动通过）


class PhaseName(str, Enum):
    """流程阶段名 —— 与 LangGraph 节点、追踪、仲裁记录对应。"""

    REQUIREMENT = "需求理解"
    STRATEGY = "策略判定"
    SUPPLIER = "供应商推荐"
    COMPARISON = "比价分析"
    COMPLIANCE = "合规审查"
    CONTRACT = "合同草稿"
    FINAL = "收官汇总"

    # 辅助/非业务阶段
    APPROVAL = "人工审批"
    ARBITRATE = "仲裁裁决"
    START = "启动"
