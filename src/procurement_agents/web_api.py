"""FastAPI 服务化层（P1.2）：把 ProcurementRunner 包成可运营的 REST API。

端点：
  * POST /procurements              —— 提交采购需求并执行流程
       body: {request_text, auto_approve?}  （auto_approve=false 默认，
             仲裁 hold 时挂起等待人工审批，返回 status=needs_input）
       resp: case 视图（case_id / status / summary / pending_approval ...）
  * GET  /procurements/{case_id}    —— 查询 case 状态与产物
  * POST /procurements/{case_id}/approve —— 人工审批决策（approved/rejected）
       body: {approved, approver?, comment?}，决策落 DB 后从断点续跑

运行（uvicorn）：
    python -m uvicorn procurement_agents.web_api:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from procurement_agents.service import ProcurementService

# 进程级单例服务（默认读 config/app.yaml；checkpointer=sqlite 时断点/元数据持久化）
_service: Optional[ProcurementService] = None


def get_service() -> ProcurementService:
    """惰性单例：进程内复用同一 checkpointer/store 连接。"""
    global _service
    if _service is None:
        _service = ProcurementService()
    return _service


class SubmitRequest(BaseModel):
    """提交采购需求请求体。"""

    request_text: str = Field(..., min_length=1, description="采购需求自由文本")
    auto_approve: bool = Field(False, description="True=仲裁 hold 自动放行；False=挂起等待人工审批")
    case_id: Optional[str] = Field(None, description="自定义案例号（缺省自动生成）")
    buyer: Optional[str] = Field(None, description="买方名称（可选）")


class ApproveRequest(BaseModel):
    """人工审批决策请求体。"""

    approved: bool = Field(..., description="True=批准放行；False=拒绝（流程阻断）")
    approver: str = Field("人工(审批接口)", description="审批人身份（审计用）")
    comment: str = Field("", description="审批意见")


def _ok(data: Any, code: int = 200) -> Dict[str, Any]:
    return {"code": code, "data": data}


def _err(message: str, code: int = 400) -> Dict[str, Any]:
    return {"code": code, "error": message}


def submit_procurement(body: SubmitRequest) -> Dict[str, Any]:
    """POST /procurements：提交并执行。"""
    svc = get_service()
    view = svc.submit(
        request_text=body.request_text,
        case_id=body.case_id,
        auto_approve=body.auto_approve,
        buyer=body.buyer,
    )
    return _ok(view)


def get_procurement(case_id: str) -> Dict[str, Any]:
    """GET /procurements/{case_id}：查询 case。"""
    svc = get_service()
    try:
        view = svc.view(case_id)
    except KeyError:
        return _err(f"case 不存在: {case_id}", code=404)
    return _ok(view)


def approve_procurement(case_id: str, body: ApproveRequest) -> Dict[str, Any]:
    """POST /procurements/{case_id}/approve：审批决策并续跑。"""
    svc = get_service()
    try:
        svc.view(case_id)  # case 不存在 -> KeyError(404)
    except KeyError:
        return _err(f"case 不存在: {case_id}", code=404)
    try:
        view = svc.resume(
            case_id=case_id,
            approved=body.approved,
            approver=body.approver,
            comment=body.comment,
        )
    except ValueError as exc:
        return _err(str(exc), code=409)
    return _ok(view)


def list_procurements(limit: int = 50) -> Dict[str, Any]:
    """GET /procurements：case 列表（最近优先）。"""
    return _ok(get_service().list_cases(limit=max(1, min(limit, 200))))


def health() -> Dict[str, Any]:
    return _ok({"service": "procurement-agents", "ok": True})


def create_app() -> Any:
    """构建 FastAPI 应用（路由层极薄，逻辑在 service 中便于单测）。"""
    from fastapi import FastAPI

    app = FastAPI(title="采购多Agent流程服务", version="0.2.0")

    @app.get("/health")
    def _health() -> Dict[str, Any]:
        return health()

    @app.post("/procurements")
    def _submit(body: SubmitRequest) -> Dict[str, Any]:
        return submit_procurement(body)

    @app.get("/procurements")
    def _list(limit: int = 50) -> Dict[str, Any]:
        return list_procurements(limit)

    @app.get("/procurements/{case_id}")
    def _get(case_id: str) -> Dict[str, Any]:
        return get_procurement(case_id)

    @app.post("/procurements/{case_id}/approve")
    def _approve(case_id: str, body: ApproveRequest) -> Dict[str, Any]:
        return approve_procurement(case_id, body)

    return app


app = create_app()
