"""领域层：全链路结构化产物（Artifact）与记录模型。

设计要点：
1. 每个 Agent 产出一种 Artifact，作为阶段间交接的"单据"；
   单据可被 JSON 序列化（mode="json"）写入 LangGraph 状态，
   从而支持 checkpoint / 追踪 / 人工审批恢复。
2. 金额字段使用 float（展示两位小数）；生产环境建议替换为 Decimal。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from procurement_agents.domain.enums import (
    CheckLevel,
    PhaseName,
    ProcurementStrategy,
    SupplierRiskLevel,
    UrgencyLevel,
    VerdictAction,
)


class RequirementItem(BaseModel):
    """采购需求中的一行（一种物品）。"""

    description: str = Field(..., description="物品名称/规格描述")
    quantity: float = Field(..., gt=0, description="数量")
    uom: str = Field("台", description="计量单位，如 台/件/吨/批")


class RequirementArtifact(BaseModel):
    """阶段1产物：结构化采购需求。"""

    title: str = Field(..., description="需求标题")
    category: str = Field(..., description="品类(ProductCategory 值)")
    urgency: UrgencyLevel = UrgencyLevel.NORMAL
    items: List[RequirementItem] = Field(default_factory=list, description="需求明细行")
    budget_amount: Optional[float] = Field(None, gt=0, description="预算金额上限(元)")
    delivery_days: Optional[int] = Field(None, gt=0, description="期望交付天数")
    quality_requirements: List[str] = Field(default_factory=list, description="质量/技术要求要点")
    usage_scene: str = Field("", description="用途场景")
    missing_fields: List[str] = Field(default_factory=list, description="缺失的关键字段(提示补全)")
    notes: str = Field("", description="原始需求备注摘录")

    @property
    def total_quantity(self) -> float:
        return sum(i.quantity for i in self.items)


class StrategyArtifact(BaseModel):
    """阶段2产物：采购策略判定结论。"""

    strategy: ProcurementStrategy
    rationale: List[str] = Field(default_factory=list, description="判定理由(可追溯)")
    rules_applied: List[str] = Field(default_factory=list, description="命中的规则编号")
    approval_needed: bool = Field(False, description="是否需要额外审批")
    remark: str = Field("", description="补充说明")


class SupplierCandidate(BaseModel):
    """阶段3产物中的单个推荐供应商（价格与供应商介绍同源，来自同一张主数据表）。"""

    supplier_id: str
    name: str
    matched_category: str
    score: float = Field(..., ge=0, le=100, description="综合推荐分(0-100)")
    reasons: List[str] = Field(default_factory=list)
    risk_level: SupplierRiskLevel = SupplierRiskLevel.LOW
    certifications: List[str] = Field(default_factory=list)
    performance_rating: float = Field(0, ge=0, le=5, description="历史绩效(0-5)")
    contact: str = ""
    # ---- 库内价目/商务信息（价格与供应商介绍放在一起）----
    price_items: str = Field("", description="价目表：`物品描述=单价`，多条以 | 分隔")
    delivery_days: Optional[int] = Field(None, description="库内承诺交付天数")
    warranty_months: int = Field(0, description="质保月数")
    payment_terms: str = Field("货到验收合格后 30 天电汇", description="付款条件")


class ExcludedSupplier(BaseModel):
    """被排除的供应商及其原因。"""

    supplier_id: str
    name: str
    reason: str


class SupplierShortlistArtifact(BaseModel):
    """阶段3产物：候选供应商短名单。"""

    category: str
    candidates: List[SupplierCandidate] = Field(default_factory=list)
    excluded: List[ExcludedSupplier] = Field(default_factory=list)
    rank_method: str = Field("品类匹配 + 绩效 + 认证 + 风险扣分", description="评分方法说明")


class QuoteLine(BaseModel):
    """行项目（价目/合同明细行通用）。"""

    description: str
    quantity: float
    unit_price: float = Field(..., ge=0)
    amount: float = Field(..., ge=0, description="小计金额")


class ComparisonRow(BaseModel):
    """比价表中的一行（一家供应商）。"""

    supplier_id: str
    supplier_name: str
    total_amount: float
    delivery_days: Optional[int] = None
    price_score: float = Field(0, ge=0, le=100, description="价格得分")
    delivery_score: float = Field(0, ge=0, le=100, description="交期得分")
    perf_score: float = Field(0, ge=0, le=100, description="历史绩效得分")
    total_score: float = Field(0, ge=0, le=100, description="综合得分")
    rank: int = Field(0, description="名次")
    notes: str = Field("", description="备注")


class Recommendation(BaseModel):
    """比价结论中的推荐。"""

    supplier_id: str
    supplier_name: str
    reason: str = Field(..., description="推荐理由（仲裁会复核是否与最低价一致）")
    price_gap_pct: Optional[float] = Field(None, description="相对最低价溢价百分比")


class ComparisonArtifact(BaseModel):
    """阶段5产物：比价分析。"""

    method: str = Field("价格60% + 交期20% + 历史绩效20%", description="评分方法")
    rows: List[ComparisonRow] = Field(default_factory=list)
    lowest_bidder_id: Optional[str] = Field(None, description="总价最低者")
    recommended: Optional[Recommendation] = Field(None, description="推荐中标对象")
    risks: List[str] = Field(default_factory=list, description="比价中发现的风险提示")
    concluded_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class ComplianceCheck(BaseModel):
    """合规检查单行。"""

    rule: str = Field(..., description="规则编号/名称")
    subject: str = Field(..., description="检查对象(供应商/条款/预算)")
    level: CheckLevel = CheckLevel.PASS
    message: str = Field("", description="结论说明")


class ComplianceArtifact(BaseModel):
    """阶段6产物：合规审查意见。"""

    eligible: bool = Field(False, description="是否存在可成交对象")
    checks: List[ComplianceCheck] = Field(default_factory=list)
    rejected: List[str] = Field(default_factory=list, description="被否决的供应商 id")
    approved_supplier_ids: List[str] = Field(default_factory=list, description="合规放行的供应商 id")
    conditions: List[str] = Field(default_factory=list, description="成交附带条件")
    conclusion: str = Field("", description="总体合规结论")
    audited_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class ContractArtifact(BaseModel):
    """阶段7产物：采购订单(PO)与合同草稿。"""

    po_number: str = Field("", description="采购订单号")
    contract_title: str = Field("", description="合同标题")
    buyer: str = Field("示例制造有限公司 采购中心", description="买方")
    supplier_id: str = Field("", description="成交供应商 id")
    supplier_name: str = Field("", description="成交供应商名称")
    total_amount: float = Field(0, description="合同总金额")
    currency: str = "CNY"
    delivery_days: Optional[int] = None
    payment_terms: str = ""
    warranty_months: int = 0
    penalty_rate: float = Field(0.005, description="逾期违约金日费率")
    items: List[QuoteLine] = Field(default_factory=list)
    conditions: List[str] = Field(default_factory=list, description="需载入合同的合规条件")
    po_text: str = Field("", description="采购订单文本")
    contract_text: str = Field("", description="合同草稿文本")
    generated_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    based_on: List[str] = Field(default_factory=list, description="依据的比价/合规结论")


class ArbitrationCheck(BaseModel):
    """仲裁检查单行。"""

    name: str = Field(..., description="检查项")
    level: CheckLevel = CheckLevel.PASS
    message: str = Field("", description="检查结果说明")


class ArbitrationRecord(BaseModel):
    """仲裁 Agent 对某一阶段的监控/裁决记录。"""

    phase: PhaseName
    verdict: VerdictAction = VerdictAction.PROCEED
    quality_score: int = Field(100, ge=0, le=100, description="该阶段质量分(0-100)")
    checks: List[ArbitrationCheck] = Field(default_factory=list)
    summary: str = Field("", description="裁决摘要")
    recorded_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def is_ok(self) -> bool:
        return self.verdict == VerdictAction.PROCEED


class ApprovalRecord(BaseModel):
    """人工审批记录（人审点）。"""

    phase: PhaseName
    decision: str = Field("auto_approved", description="approved / rejected / auto_approved")
    approver: str = Field("系统(自动审批配置)", description="审批人")
    comment: str = Field("", description="意见")
    recorded_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class StageRecord(BaseModel):
    """一个业务阶段的运行记录（供追踪/报告使用）。"""

    phase: PhaseName
    started_at: str = ""
    finished_at: str = ""
    status: str = "ok"          # ok / error / skipped
    retries: int = 0
    error: str = ""
    artifact_keys: List[str] = Field(default_factory=list, description="本阶段写入状态的产物键")
    detail: Dict[str, Any] = Field(default_factory=dict)
