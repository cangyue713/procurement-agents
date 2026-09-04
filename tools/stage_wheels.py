"""离线解压安装引导：把 .wheels/*.whl 解压到 .pylibs 目录。

用途：沙箱/离线环境下 pip 无法写 site-packages 时的替代安装方式。
用法：python tools/stage_wheels.py
之后运行需带上环境变量：
    set PYTHONPATH=<项目根>\.pylibs;<项目根>\src
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WHEEL_DIR = ROOT / ".wheels"
TARGET = ROOT / ".pylibs"


def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    wheels = sorted(WHEEL_DIR.glob("*.whl"))
    if not wheels:
        print("未找到 wheel，请先运行 tools/fetch_wheels.py")
        return 1
    for whl in wheels:
        with zipfile.ZipFile(whl) as zf:
            zf.extractall(TARGET)
        print(f"[installed] {whl.name}")
    n = len(list(TARGET.iterdir()))
    print(f"\n共解压 {len(wheels)} 个 wheel -> {TARGET}（顶层 {n} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
