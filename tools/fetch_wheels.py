"""依赖引导脚本：绕过本机 pip 网络卡死问题。

背景：部分 Windows 沙箱环境中 pip 的 HTTP 客户端在解析索引页后会挂起，
但标准库 urllib 访问 PyPI 正常。本脚本改用 PyPI JSON API 递归解析
依赖树并直接下载匹配平台的 wheel，随后可用
    python -m pip install --no-index --find-links=.wheels <包名...>
离线安装。

用法：python tools/fetch_wheels.py [包名...]   （默认读 requirements.txt 顶层包）
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

INDEX = "https://pypi.org/pypi/{name}/json"
TOP_LEVEL = ["langgraph", "pydantic", "PyYAML", "python-dotenv", "openai", "pytest"]
WHEEL_DIR = Path(__file__).resolve().parents[1] / ".wheels"

# 版本钉扎：为规避新一代 httpx2/h11 依赖宇宙与本机 pip 网络问题，
# 统一采用成熟稳定的 0.6 世代组合（langgraph 0.6.x + langchain-core 0.3.x）
PINS: Dict[str, str] = {
    "langgraph": "0.6.9",
    "langchain-core": "0.3.86",
    "langsmith": "0.3.45",
    "langgraph-checkpoint": "2.1.2",
    "langgraph-prebuilt": "0.6.5",
    "langgraph-sdk": "0.2.15",
    "pydantic": "2.13.5",
    "pydantic-core": "2.46.5",   # pydantic 精确配对要求 ==2.46.5
    "openai": "1.109.1",          # 1.x 老栈(httpx<1)，与 langsmith 0.3 世代兼容
}

# 平台判定：优先本机 cp 版本精确匹配，其次 abi3 通用二进制，再次纯 Python
PY_TAG = "cp{s}{m}".format(s=sys.version_info.major, m=sys.version_info.minor)  # 如 cp312


def wheel_score(url: str) -> Optional[Tuple[int, int]]:
    fn = url.split("/")[-1].split("#")[0]
    if not fn.endswith(".whl"):
        return None
    tags = fn.split("-")
    if len(tags) < 5:
        return None
    py, abi, plat = tags[2], tags[3], tags[4].replace(".whl", "")
    if "win_amd64" in plat:
        if py == PY_TAG and abi in (PY_TAG, "abi3"):
            return (0, 0)                      # 本机精确平台 + 本机 ABI
        if abi == "abi3" and py.startswith("cp"):
            return (0, 1)                      # abi3 向后兼容（cp37-abi3 可用于 3.12）
        return None                            # cp310-cp310 等在 cp312 上不可用
    if py in ("py3", "py2.py3") and abi == "none" and plat == "any":
        return (1, 0)                          # 通用纯 Python
    return None


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pick_wheel(name: str) -> Tuple[str, str]:
    """返回 (版本, 最优 wheel url)。"""
    info = get_json(INDEX.format(name=name))
    pinned = PINS.get(name.lower().replace("_", "-"))
    version = pinned or info["info"]["version"]
    releases = info["releases"].get(version, [])
    if not releases:
        raise RuntimeError(f"{name}=={version} 无发布产物")
    best, best_score = None, (9, 9)
    for item in releases:
        u = item["url"]
        if not u.endswith(".whl"):
            continue
        s = wheel_score(u)
        if s and s < best_score:
            best, best_score = u, s
    if best is None:
        raise RuntimeError(f"{name}=={version} 无可用 wheel")
    return version, best


def download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        return
    with urllib.request.urlopen(url, timeout=60) as resp, open(dest, "wb") as f:
        f.write(resp.read())


def main() -> int:
    WHEEL_DIR.mkdir(parents=True, exist_ok=True)
    tops = sys.argv[1:] or TOP_LEVEL
    queue: List[str] = list(tops)
    done: Set[str] = set()
    wanted: Dict[str, str] = {}   # name(lower) -> 版本
    while queue:
        name = queue.pop(0)
        key = name.lower().replace("_", "-")
        if key in done:
            continue
        done.add(key)
        try:
            data = get_json(INDEX.format(name=name))
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] {name}: {exc}")
            continue
        version = data["info"]["version"]
        print(f"[resolve] {name}=={version}")
        for req in data["info"].get("requires_dist") or []:
            # 跳过 extras(可选特性)与带环境标记但非 base 的依赖
            if "extra" in req:
                continue
            req_clean = re.split(r"[;[]", req)[0].strip()
            m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(?:[<>=!~].*)?$", req_clean)
            if m and not req_clean.startswith(("git+", "http")):
                dep = m.group(1).lower().replace("_", "-")
                if dep not in done:
                    queue.append(dep)
        _, url = pick_wheel(name)
        fname = url.split("/")[-1].split("#")[0]
        wanted[key] = version
        download(url, WHEEL_DIR / fname)
    print(f"\n共 {len(wanted)} 个包，wheel 目录：{WHEEL_DIR}")
    print("安装命令：")
    print("  python -m pip install --no-index --find-links=.wheels " + " ".join(wanted))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
