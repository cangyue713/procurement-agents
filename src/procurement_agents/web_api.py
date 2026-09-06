"""FastAPI 服务化层（P1.2 起；P2-B：RBAC + 操作审计的强制入口）。

身份与授权（对齐企业内控口径）：
  * 每个业务请求必须携带身份头 `X-Actor-Id`（用户目录见 config/access.yaml）；
    缺身份 -> 401；未收录用户 -> 403。
  * 权限矩阵：submit 需 procurement.submit；approve 需 procurement.approve；
    GET 流程需 procurement.view；GET /audit 需 audit.view。
  * 审批人/发起人身份一律取自已认证主体（X-Actor-Id），不接受请求体伪造；
  * 每次动作由 service 落审计日志（who/what/when），拒绝(denied)同样留痕。

端点：
  * POST /procurements                提交并执行（挂起等待人工审批）
  * GET  /procurements                列表
  * GET  /procurements/{case_id}      查询
  * POST /procurements/{case_id}/approve  人工审批（可带附件元数据）
  * GET  /audit                       审计日志（auditor/admin）
  * GET  /health                      健康检查（公开）

运行（uvicorn）：
    python -m uvicorn procurement_agents.web_api:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import Header
from pydantic import BaseModel, Field

from procurement_agents.access import (
    Actor,
    AuthorizationError,
    Permission,
    load_actor,
    require,
)
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
    """提交采购需求请求体（发起人身份来自 X-Actor-Id）。"""

    request_text: str = Field(..., min_length=1, description="采购需求自由文本")
    auto_approve: bool = Field(False, description="True=仲裁 hold 自动放行；False=挂起等待人工审批")
    case_id: Optional[str] = Field(None, description="自定义案例号（缺省自动生成）")
    buyer: Optional[str] = Field(None, description="买方显示名（缺省取身份显示名）")


class AttachmentMeta(BaseModel):
    """审批附件元数据（文件本体外部存储，仅元数据 + 哈希入库审计）。"""

    filename: str = Field(..., description="附件文件名")
    content_type: str = Field("", description="MIME 类型")
    size: int = Field(0, ge=0, description="字节数")
    sha256: str = Field("", description="文件内容哈希（防篡改核对）")
    note: str = Field("", description="附件说明（可选）")


class ApproveRequest(BaseModel):
    """人工审批决策请求体（审批人身份取自已认证主体，不可伪造）。"""

    approved: bool = Field(..., description="True=批准放行；False=拒绝（流程阻断）")
    comment: str = Field("", description="审批意见")
    attachments: List[AttachmentMeta] = Field(default_factory=list,
                                              description="审批附件元数据（含 sha256）")


class BatchItem(BaseModel):
    """批量计划中的单条采购需求。"""

    request_text: str = Field(..., min_length=1, description="采购需求自由文本")
    case_id: Optional[str] = Field(None, description="自定义案例号（缺省自动生成）")


class BatchRequest(BaseModel):
    """批量/计划性采购提交请求体（P2-F）。"""

    items: List[BatchItem] = Field(..., min_length=1, description="计划内的多条需求")
    auto_approve: bool = Field(False, description="hold 自动放行/挂起等待")
    plan_id: Optional[str] = Field(None, description="计划号（缺省自动生成 PLAN-…）")


def _ok(data: Any, code: int = 200) -> Dict[str, Any]:
    return {"code": code, "data": data}


def _err(message: str, code: int = 400) -> Dict[str, Any]:
    return {"code": code, "error": message}


def _actor_of(x_actor_id: Optional[str], permission: Permission) -> Actor:
    """从身份头解析并授权；无身份 401 / 未收录或无权限 403。"""
    if not x_actor_id or not str(x_actor_id).strip():
        raise AuthorizationError("", f"缺少身份头 X-Actor-Id（需 {permission.value}）")
    actor = load_actor(x_actor_id)
    if actor is None:
        raise AuthorizationError(x_actor_id, permission.value)
    return require(actor, permission)


def submit_procurement(body: SubmitRequest, x_actor_id: Optional[str]) -> Dict[str, Any]:
    """POST /procurements：提交并执行（发起人=请求身份）。"""
    actor = _actor_of(x_actor_id, Permission.PROCUREMENT_SUBMIT)
    svc = get_service()
    view = svc.submit(
        request_text=body.request_text,
        case_id=body.case_id,
        auto_approve=body.auto_approve,
        buyer=body.buyer or actor.display_name,
        buyer_id=actor.user_id,
        actor=actor.user_id,
    )
    return _ok(view)


def get_procurement(case_id: str, x_actor_id: Optional[str]) -> Dict[str, Any]:
    """GET /procurements/{case_id}：查询 case（需登录）。"""
    _actor_of(x_actor_id, Permission.PROCUREMENT_VIEW)
    svc = get_service()
    try:
        view = svc.view(case_id)
    except KeyError:
        return _err(f"case 不存在: {case_id}", code=404)
    return _ok(view)


def approve_procurement(case_id: str, body: ApproveRequest,
                        x_actor_id: Optional[str]) -> Dict[str, Any]:
    """POST /procurements/{case_id}/approve：审批决策（审批人=请求身份）。"""
    actor = _actor_of(x_actor_id, Permission.PROCUREMENT_APPROVE)
    svc = get_service()
    try:
        svc.view(case_id)  # case 不存在 -> KeyError(404)
    except KeyError:
        return _err(f"case 不存在: {case_id}", code=404)
    try:
        view = svc.resume(
            case_id=case_id,
            approved=body.approved,
            approver=actor.display_name or actor.user_id,
            approver_id=actor.user_id,
            comment=body.comment,
            attachments=[a.model_dump(mode="json") for a in body.attachments],
            actor=actor.user_id,
        )
    except ValueError as exc:
        return _err(str(exc), code=409)
    return _ok(view)


def list_procurements(limit: int, x_actor_id: Optional[str]) -> Dict[str, Any]:
    """GET /procurements：case 列表（需登录）。"""
    _actor_of(x_actor_id, Permission.PROCUREMENT_VIEW)
    return _ok(get_service().list_cases(limit=max(1, min(limit, 200))))


def batch_procurements(body: BatchRequest, x_actor_id: Optional[str]) -> Dict[str, Any]:
    """POST /procurements/batch：批量/计划性采购（P2-F）。"""
    _actor_of(x_actor_id, Permission.PROCUREMENT_SUBMIT)
    svc = get_service()
    plan = svc.submit_many(
        requests=[{"request_text": it.request_text, "case_id": it.case_id}
                  for it in body.items],
        plan_id=body.plan_id,
        auto_approve=body.auto_approve,
        actor=x_actor_id,
    )
    return _ok(plan)


def get_plan(plan_id: str, x_actor_id: Optional[str]) -> Dict[str, Any]:
    """GET /procurements/plans/{plan_id}：批量计划汇总（P2-F）。"""
    _actor_of(x_actor_id, Permission.PROCUREMENT_VIEW)
    return _ok(get_service().list_plan(plan_id))


def list_audit(limit: int, x_actor_id: Optional[str]) -> Dict[str, Any]:
    """GET /audit：操作审计日志（需 audit.view，auditor/admin）。"""
    _actor_of(x_actor_id, Permission.AUDIT_VIEW)
    rows = get_service().store.list_audit(limit=max(1, min(limit, 500)))
    return _ok(rows)


def health() -> Dict[str, Any]:
    return _ok({"service": "procurement-agents", "ok": True})


def create_app() -> Any:
    """构建 FastAPI 应用（路由层极薄，逻辑在 service 中便于单测）。

    身份头 X-Actor-Id 经 Header 依赖注入；AuthorizationError 统一转
    401(无身份)/403(无权限) 并留 denied 审计。
    """
    from fastapi import FastAPI

    app = FastAPI(title="采购多Agent流程服务", version="0.3.0")

    def _wrap(actor_id: Optional[str], exc: AuthorizationError) -> Dict[str, Any]:
        try:  # 拒绝动作同样留审计（who/what/when）
            get_service().store.log_action(
                actor_id or "(匿名)", "access.denied", "",
                "denied", {"message": str(exc), "permission": getattr(exc, "permission", "")},
            )
        except Exception:
            pass
        if "身份头" in str(exc):
            return _err(str(exc), code=401)
        return _err(str(exc), code=403)

    @app.get("/health")
    def _health() -> Dict[str, Any]:
        return health()

    @app.post("/procurements")
    def _submit(body: SubmitRequest, x_actor_id: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        try:
            return submit_procurement(body, x_actor_id)
        except AuthorizationError as exc:
            return _wrap(x_actor_id, exc)

    @app.get("/procurements")
    def _list(limit: int = 50, x_actor_id: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        try:
            return list_procurements(limit, x_actor_id)
        except AuthorizationError as exc:
            return _wrap(x_actor_id, exc)

    @app.get("/procurements/{case_id}")
    def _get(case_id: str, x_actor_id: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        try:
            return get_procurement(case_id, x_actor_id)
        except AuthorizationError as exc:
            return _wrap(x_actor_id, exc)

    @app.post("/procurements/{case_id}/approve")
    def _approve(case_id: str, body: ApproveRequest,
                 x_actor_id: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        try:
            return approve_procurement(case_id, body, x_actor_id)
        except AuthorizationError as exc:
            return _wrap(x_actor_id, exc)

    @app.post("/procurements/batch")
    def _batch(body: BatchRequest,
               x_actor_id: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        try:
            return batch_procurements(body, x_actor_id)
        except AuthorizationError as exc:
            return _wrap(x_actor_id, exc)

    @app.get("/procurements/plans/{plan_id}")
    def _get_plan(plan_id: str,
                  x_actor_id: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        try:
            return get_plan(plan_id, x_actor_id)
        except AuthorizationError as exc:
            return _wrap(x_actor_id, exc)

    @app.get("/audit")
    def _audit(limit: int = 100, x_actor_id: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        try:
            return list_audit(limit, x_actor_id)
        except AuthorizationError as exc:
            return _wrap(x_actor_id, exc)

    return app


app = create_app()
