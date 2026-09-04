"""演示案例：华东三厂『数字化产线服务器与网络设备』采购。

流程（价格与供应商介绍同源，无询价/报价环节）：
    需求理解 -> 采购策略判定 -> 供应商推荐(库内价目)
  -> 比价分析 -> 合规审查 -> PO/合同草稿生成，全程由仲裁 Agent 监控。

剧本要点：
  1. 需求为紧急采购（预算 280 万，25 天内交付）-> 策略判定为『邀请招标』；
  2. 供应商库(suppliers.csv)推荐 3 家：华云数据 / 中科智算 / 蓝海云联，价目随候选给出；
  3. 比价按 需求行×库内价目 计总价排名；蓝海虽总价最低但库内交期 45 天远超需求，
     被合规审查(C3)否决 -> 合同自动落定合规放行中比价名次最高的华云数据；
  4. 仲裁全程监控：价目完整性、非最低价定标理由、定标切换、预算红线等均有留痕。
"""
from __future__ import annotations

from pathlib import Path

CASE_ID = "PC-20250601-001"

# ---- 需求文本：由项目从 txt 文件读取（单一数据源 examples/input/request.txt）----
_REQUEST_TXT = Path(__file__).resolve().parent / "input" / "request.txt"


def load_request_text(path: Path | str | None = None) -> str:
    """从 txt 读取采购需求文本（UTF-8，容忍 BOM）。"""
    p = Path(path) if path else _REQUEST_TXT
    if not p.exists():
        raise FileNotFoundError(f"需求输入文件缺失: {p}")
    return p.read_text(encoding="utf-8-sig").strip()


DEMO_REQUEST_TEXT = load_request_text()

# 预期结果（供测试/演示校验）
EXPECTED_WINNER = "华云数据技术有限公司"
EXPECTED_STRATEGY = "邀请招标"
