"""LLM 录制/回放设施（P2-E 契约测试）。

用途：
  * RecordingProvider 包住任意真实 Provider（DeepSeek / OpenAI 兼容），
    记录每次调用的 request+response 为 JSON 契约夹具；
  * ReplayProvider 从夹具回放 —— 离线、确定地重放『当时真实模型』的输出，
    供归一化/契约测试断言：解析逻辑不因模型更新回退；
  * 夹具随仓库提交（tests/fixtures/llm/*.json），CI 无需 API Key；
    换新模型后可用 tools/record_provider.py 重新录制并人工评审差异。

夹具结构：
  {
    "meta": {"provider": "openai_compat"|"mock", "model": "...", "recorded_at": "..."},
    "records": [
      {"request": {"task": "...", "text": "...", "hints": {...}},
       "response": {"data": {...}, "provider": "...", "raw_text": "...", "ok": true}}
    ]
  }
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from procurement_agents.llm.base import (
    TASK_PARSE_REQUIREMENT,
    LLMProvider,
    LLMRequest,
    LLMResponse,
)


def _dump_request(request: LLMRequest) -> Dict[str, Any]:
    return {"task": request.task, "text": request.text,
            "hints": request.hints or {}, "expect_schema": request.expect_schema}


def _load_request(record: Dict[str, Any]) -> LLMRequest:
    req = record.get("request") or {}
    return LLMRequest(
        task=str(req.get("task") or TASK_PARSE_REQUIREMENT),
        text=str(req.get("text") or ""),
        hints=dict(req.get("hints") or {}),
        expect_schema=str(req.get("expect_schema") or ""),
    )


class RecordingProvider(LLMProvider):
    """把底层 Provider 的每次调用记录下来（不改变其行为）。"""

    name = "recording"

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self._records: List[Dict[str, Any]] = []

    @property
    def records(self) -> List[Dict[str, Any]]:
        return self._records

    def complete(self, request: LLMRequest) -> LLMResponse:
        resp = self._inner.complete(request)
        self._records.append({
            "request": _dump_request(request),
            "response": {
                "data": resp.data,
                "provider": resp.provider,
                "raw_text": resp.raw_text,
                "ok": resp.ok,
                "error": resp.error,
            },
        })
        return resp

    def save(self, path: str | Path, model: str = "") -> Path:
        """把记录写入 JSON 夹具文件（覆盖式），返回路径。"""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": {
                "provider": self._inner.name,
                "model": model or getattr(self._inner, "_model", ""),
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
                "count": len(self._records),
            },
            "records": self._records,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return out


class ReplayProvider(LLMProvider):
    """从夹具回放录制输出（离线契约测试用）。"""

    name = "replay"

    def __init__(self, fixture_path: str | Path) -> None:
        self._path = Path(fixture_path)
        self._meta: Dict[str, Any] = {}
        self._records: List[Dict[str, Any]] = []
        self._by_key: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(f"契约夹具缺失: {self._path}（先用 tools/record_provider.py 录制）")
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        self._meta = payload.get("meta") or {}
        self._records = payload.get("records") or []
        for rec in self._records:
            req = _load_request(rec)
            key = self._key(req)
            self._by_key[key] = rec  # 同输入后者覆盖（幂等录制语义）

    @property
    def meta(self) -> Dict[str, Any]:
        return self._meta

    @property
    def records(self) -> List[Dict[str, Any]]:
        return self._records

    @staticmethod
    def _key(request: LLMRequest) -> str:
        text = " ".join(str(request.text).split())  # 规整空白：跨换行命中
        return f"{request.task}\n{text}"

    def complete(self, request: LLMRequest) -> LLMResponse:
        rec = self._by_key.get(self._key(request))
        if rec is None:
            return LLMResponse.failed(
                self.name,
                f"夹具未命中该输入(task={request.task}, text 前 40 字: {request.text[:40]})",
            )
        resp = rec.get("response") or {}
        return LLMResponse(
            data=dict(resp.get("data") or {}),
            provider=str(resp.get("provider") or self.name),
            raw_text=str(resp.get("raw_text") or ""),
            ok=bool(resp.get("ok", True)),
            error=str(resp.get("error") or ""),
        )


def record_to_fixture(provider: LLMProvider, texts: List[str],
                      out_path: str | Path, model: str = "") -> Path:
    """对一批需求文本调用 provider 并把响应录制为夹具（便捷入口）。"""
    rec = RecordingProvider(provider)
    for text in texts:
        rec.complete(LLMRequest(task=TASK_PARSE_REQUIREMENT, text=text))
    return rec.save(out_path, model=model)
