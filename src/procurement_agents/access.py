"""访问控制（P2-B RBAC）：谁可做什么 —— 采购/审批/改主数据/审计。

设计：
  * Actor：已认证主体（user_id + 显示名 + 角色集合）；
  * Role：角色枚举，含权限集合（RBAC 授权策略，代码内固定、可追溯）；
  * Permission：动作级权限名，服务层/Web 层用 require() 校验；
  * 用户目录：config/access.yaml（user_id -> 显示名 + 角色），
    load_actor() 解析请求身份；未知用户 -> 无法通过授权（拒绝）。

对齐财务/法务合规的语义：
  * 采购发起 = procurement.submit（buyer/approver/admin）；
  * 人工审批 = procurement.approve（approver/admin）；
  * 查看流程 = procurement.view（登录用户均可）；
  * 主数据/规则变更 = masterdata.change / rules.change（仅 admin，配合变更审批）；
  * 审计查看 = audit.view（auditor/admin）。

内部调用（脚本/测试/系统自动动作）可使用 Actor.system() —— 拥有全部权限，
但每次动作仍写入审计日志（who=system，便于与人工动作区分）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

import yaml

from procurement_agents.config import PROJECT_ROOT

ACCESS_PATH = PROJECT_ROOT / "config" / "access.yaml"


class AuthorizationError(PermissionError):
    """权限不足：主体缺少执行某动作所需的权限。"""

    def __init__(self, actor_id: str, permission: str) -> None:
        super().__init__(f"主体 {actor_id or '(匿名)'} 缺少权限: {permission}")
        self.actor_id = actor_id
        self.permission = permission


class Permission(str, Enum):
    """动作级权限（命名：资源.动词）。"""

    PROCUREMENT_SUBMIT = "procurement.submit"      # 触发采购流程
    PROCUREMENT_APPROVE = "procurement.approve"    # 人工审批
    PROCUREMENT_VIEW = "procurement.view"          # 查看 case
    MASTERDATA_CHANGE = "masterdata.change"        # 供应商主数据导入/变更
    RULES_CHANGE = "rules.change"                  # 合规/仲裁规则变更
    AUDIT_VIEW = "audit.view"                      # 审计日志查看


class Role(str, Enum):
    """业务角色。"""

    BUYER = "buyer"                 # 采购员：发起采购
    APPROVER = "approver"           # 审批人：人工审批
    COMPLIANCE = "compliance"       # 合规岗：查看流程/参与合规
    ADMIN = "admin"                 # 管理员：主数据/规则变更
    AUDITOR = "auditor"             # 审计：只读审计日志与流程


# 角色 -> 权限矩阵（RBAC 授权策略；变更需走规则版本化流程）
ROLE_PERMISSIONS: Dict[Role, List[Permission]] = {
    Role.BUYER: [Permission.PROCUREMENT_SUBMIT, Permission.PROCUREMENT_VIEW],
    Role.APPROVER: [Permission.PROCUREMENT_SUBMIT, Permission.PROCUREMENT_APPROVE,
                    Permission.PROCUREMENT_VIEW],
    Role.COMPLIANCE: [Permission.PROCUREMENT_VIEW],
    Role.ADMIN: [p for p in Permission],  # 管理员：全量
    Role.AUDITOR: [Permission.PROCUREMENT_VIEW, Permission.AUDIT_VIEW],
}


@dataclass
class Actor:
    """已认证主体。"""

    user_id: str
    display_name: str = ""
    roles: List[Role] = field(default_factory=list)

    @property
    def permissions(self) -> set[str]:
        perms: set[str] = set()
        for r in self.roles:
            perms.update(p.value for p in ROLE_PERMISSIONS.get(r, []))
        return perms

    def has(self, permission: Permission | str) -> bool:
        want = permission.value if isinstance(permission, Permission) else str(permission)
        return want in self.permissions

    @classmethod
    def system(cls) -> "Actor":
        """系统内部主体（拥有全权限，动作留痕可识别）。"""
        return cls(user_id="__system__", display_name="系统内部", roles=[Role.ADMIN])

    def to_dict(self) -> Dict[str, Any]:
        return {"user_id": self.user_id, "display_name": self.display_name,
                "roles": [r.value for r in self.roles]}


def require(actor: Actor | None, permission: Permission | str) -> Actor:
    """授权校验：无权限抛 AuthorizationError（返回规范化后的主体）。"""
    a = actor if actor is not None else Actor.system()
    if not a.has(permission):
        raise AuthorizationError(a.user_id,
                                 permission.value if isinstance(permission, Permission) else permission)
    return a


def _load_user_records() -> Dict[str, Dict[str, Any]]:
    """读取 config/access.yaml 用户目录（缺失时返回空）。"""
    if not ACCESS_PATH.exists():
        return {}
    with open(ACCESS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("users") or {}


def load_actor(user_id: str) -> Actor | None:
    """按 user_id 解析主体；未收录用户返回 None（匿名不可授权）。"""
    uid = str(user_id or "").strip()
    if not uid or uid == "__system__":
        return None
    rec = _load_user_records().get(uid)
    if rec is None:
        return None
    role_values = rec.get("role") or rec.get("roles") or []
    if isinstance(role_values, str):
        role_values = [role_values]
    roles: List[Role] = []
    for rv in role_values:
        try:
            roles.append(Role(str(rv)))
        except ValueError:
            continue  # 未知角色忽略（不授予权限）
    return Actor(user_id=uid, display_name=str(rec.get("name") or uid), roles=roles)
