"""OpenAI vision page extraction and chapter summarisation.

The reader deliberately keeps this module independent from uiautomator2.  A
caller supplies PNG bytes (normally a device screenshot) and receives plain
novel text.  Network errors are represented as :class:`VisionError` so the
existing automation can continue reading when AI is unavailable.
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


class VisionError(RuntimeError):
    """An expected vision API failure with a user-displayable message."""


def extract_model_ids(data: dict) -> list[str]:
    """Normalize OpenAI ``/models`` responses to sorted unique model IDs."""
    if not isinstance(data, dict):
        return []
    raw = data.get("data")
    if not isinstance(raw, list):
        raw = data.get("models") if isinstance(data.get("models"), list) else []
    ids = []
    for item in raw:
        if isinstance(item, str):
            model_id = item.strip()
        elif isinstance(item, dict):
            model_id = str(item.get("id", "") or "").strip()
        else:
            model_id = ""
        if model_id and model_id not in ids:
            ids.append(model_id)
    return sorted(ids, key=str.casefold)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_as_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "value", "content"):
            if key in value:
                return _as_text(value[key])
    return ""


def extract_response_text(data: dict) -> str:
    """Extract text from Responses API and Chat Completions responses.

    Compatible gateways sometimes return ``output_text`` while others expose
    the nested Responses shape.  Supporting both here keeps the rest of the
    reader provider-neutral.
    """
    direct = _as_text(data.get("output_text"))
    if direct.strip():
        return direct.strip()
    output = data.get("output") or []
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        chunks.append(_as_text(item.get("content")))
    text = "".join(chunks).strip()
    if text:
        return text
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        return _as_text((choices[0].get("message") or {}).get("content")).strip()
    return ""


def extract_chapter_label(ui_text: str, fallback: str = "") -> str:
    """Return a compact chapter heading from a UI dump or model text."""
    text = str(ui_text or "")
    match = re.search(
        r"第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章节回卷篇]"
        r"[^\r\n]{0,80}?"
        r"(?=\s+\d+\s*/\s*\d+|\s+\d{1,2}:\d{2}|\s+(?:本章讨论|上一章|下一章|目录)|$)",
        text,
    )
    if match:
        return re.sub(r"\s+", " ", match.group(0)).strip()[:80]
    return fallback or "本章"


def _chapter_key(label: str) -> str:
    """Return the stable chapter number/unit used for boundary detection."""
    match = re.search(
        r"第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章节回卷篇]",
        str(label or ""),
    )
    return re.sub(r"\s+", "", match.group(0)) if match else str(label or "")


@dataclass
class VisionSettings:
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = "gpt-4o-mini"
    summary_model: str = ""
    timeout: int = 30
    detail: str = "high"
    max_pages: int = 240
    max_chars_per_page: int = 5000
    summary_enabled: bool = True
    summary_max_chars: int = 14000
    concurrency: int = 2


class OpenAIVisionClient:
    """Small standard-library OpenAI client with injectable transport tests."""

    def __init__(
        self,
        settings: VisionSettings,
        *,
        request_fn: Callable[[str, bytes, int], dict] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.log = logger or logging.getLogger("fanqie.vision")
        self._request_fn = request_fn

    @property
    def available(self) -> bool:
        return bool(self.settings.enabled and self.settings.base_url and self.settings.api_key)

    def _post(self, endpoint: str, payload: dict) -> dict:
        if self._request_fn:
            try:
                data = self._request_fn(endpoint, json.dumps(payload).encode("utf-8"), self.settings.timeout)
            except VisionError:
                raise
            except Exception as exc:
                raise VisionError(f"视觉接口请求失败: {exc}") from exc
            if not isinstance(data, dict):
                raise VisionError("视觉接口返回格式无效")
            return data
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.settings.base_url.rstrip("/") + endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.settings.api_key,
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=max(5, self.settings.timeout)) as response:
                raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise VisionError(f"视觉接口 HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise VisionError(f"视觉接口请求失败: {exc}") from exc
        if not isinstance(data, dict):
            raise VisionError("视觉接口返回格式无效")
        return data

    def _responses(self, content: list[dict], *, max_tokens: int, model: str = "") -> str:
        payload = {
            "model": model or self.settings.model,
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": max(128, max_tokens),
        }
        data = self._post("/responses", payload)
        text = extract_response_text(data)
        if not text:
            raise VisionError("视觉接口没有返回文本")
        return text

    def _chat_compat(self, content: list[dict], *, max_tokens: int, model: str = "") -> str:
        # Fallback for existing OpenAI-compatible gateways that do not expose
        # the newer Responses endpoint.
        chat_content = []
        for item in content:
            if item.get("type") == "input_text":
                chat_content.append({"type": "text", "text": item.get("text", "")})
            elif item.get("type") == "input_image":
                # Some OpenAI-compatible gateways validate image detail as a
                # literal ``auto`` value and reject ``low``/``high``.  Keep
                # the user-facing setting, but normalize the wire payload.
                chat_content.append({
                    "type": "image_url",
                    "image_url": {"url": item.get("image_url", ""), "detail": "auto"},
                })
        payload = {
            "model": model or self.settings.model,
            "messages": [{"role": "user", "content": chat_content}],
            "max_tokens": max(128, max_tokens),
        }
        return extract_response_text(self._post("/chat/completions", payload))

    def _complete(self, content: list[dict], *, max_tokens: int, model: str = "") -> str:
        try:
            return self._responses(content, max_tokens=max_tokens, model=model)
        except VisionError as first:
            # A 4xx response from /responses often means an older compatible
            # proxy, so try chat completions once.  The original error remains
            # available when both endpoints fail.
            try:
                text = self._chat_compat(content, max_tokens=max_tokens, model=model)
                if text.strip():
                    return text.strip()
            except VisionError:
                pass
            raise first

    def extract_page(self, image_bytes: bytes, page_no: int | None = None) -> str:
        if not self.available:
            raise VisionError("视觉阅读未配置 API Key 或接口地址")
        encoded = base64.b64encode(image_bytes).decode("ascii")
        page_hint = f"这是第 {page_no} 页。" if page_no else ""
        content = [
            {
                "type": "input_text",
                "text": (
                    "你是小说阅读 OCR。只提取截图中小说正文的连续文字，保留段落顺序和中文标点；"
                    "忽略页码、按钮、广告、评论、导航和状态栏。若没有正文只输出 NO_TEXT。"
                    f"{page_hint}只输出正文，不要解释。"
                ),
            },
            {"type": "input_image", "image_url": f"data:image/png;base64,{encoded}", "detail": self.settings.detail},
        ]
        text = self._complete(content, max_tokens=max(256, self.settings.max_chars_per_page // 2))
        text = text.strip().strip("`")
        if text.upper() == "NO_TEXT":
            return ""
        return text[: self.settings.max_chars_per_page]

    def summarize_chapter(self, chapter: str, pages: list[str]) -> str:
        if not self.available:
            raise VisionError("视觉阅读未配置 API Key 或接口地址")
        joined = "\n\n".join(p.strip() for p in pages if p and p.strip())
        if not joined:
            return ""
        joined = joined[: self.settings.summary_max_chars]
        content = [{
            "type": "input_text",
            "text": (
                f"请总结小说{chapter}。以下是逐页提取的正文：\n\n{joined}\n\n"
                "只输出一段 80-180 字的中文摘要，概括主要人物、事件进展、冲突和悬念；"
                "不要编造未出现的内容，不要使用标题、引号或 Markdown。"
            ),
        }]
        return self._complete(
            content,
            max_tokens=500,
            model=self.settings.summary_model or self.settings.model,
        ).strip()[:1200]


class VisionReadingSession:
    """Asynchronously extracts pages and synchronously flushes a chapter."""

    def __init__(
        self,
        settings: VisionSettings,
        runtime: Any,
        *,
        book: str,
        serial: str,
        logger: logging.Logger | None = None,
        client: OpenAIVisionClient | None = None,
        persist_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.runtime = runtime
        self.book = book
        self.serial = serial
        self.log = logger or logging.getLogger("fanqie.vision")
        self.client = client or OpenAIVisionClient(settings, logger=self.log)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, settings.concurrency))
        self.futures: list[tuple[int, concurrent.futures.Future[str]]] = []
        self.chapter_texts: list[str] = []
        self.page_texts: list[str] = []
        self.submitted = 0
        self.extracted = 0
        self.failed = 0
        self.chapter_index = 0
        self.chapter_pages = 0
        self.current_chapter = ""
        self.current_chapter_key = ""
        self.last_hash = ""
        self.persist_path = persist_path
        self._lock = threading.Lock()
        self._closed = False
        self._set_state(status="ready" if self.client.available else "disabled", error="")

    def _set_state(self, **fields: Any) -> None:
        state = {
            "enabled": bool(self.client.available),
            "status": "idle",
            "book": self.book,
            "chapter": "",
            "chapter_index": self.chapter_index,
            "current_page": 0,
            "pages_submitted": self.submitted,
            "pages_extracted": self.extracted,
            "pages_failed": self.failed,
            "summary": "",
            "summaries": [],
            "error": "",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        if hasattr(self.runtime, "get"):
            try:
                old = self.runtime.get().get("vision") or {}
                state.update(old)
            except Exception:
                pass
        state.update(fields)
        # These values belong to the current session and must not be replaced
        # by stale runtime data from the previous book/task.
        state["enabled"] = bool(self.client.available)
        state["book"] = self.book
        state["chapter_index"] = self.chapter_index
        state["pages_submitted"] = self.submitted
        state["pages_extracted"] = self.extracted
        state["pages_failed"] = self.failed
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if hasattr(self.runtime, "update_vision"):
            self.runtime.update_vision(**state)
        elif hasattr(self.runtime, "update"):
            self.runtime.update(vision=state)

    def _drain_completed(self, *, wait: bool = False) -> None:
        """Collect finished OCR futures so counters update before chapter flush."""
        pending: list[tuple[int, concurrent.futures.Future[str]]] = []
        changed = False
        for sequence, future in self.futures:
            if not wait and not future.done():
                pending.append((sequence, future))
                continue
            try:
                text = future.result(timeout=max(5, self.settings.timeout) + 5 if wait else 0)
            except concurrent.futures.TimeoutError:
                pending.append((sequence, future))
                continue
            except Exception as exc:
                self.failed += 1
                changed = True
                self._set_state(status="error", error=str(exc)[:500])
                self.log.warning("《%s》视觉识别失败: %s", self.book, exc)
                continue
            self.extracted += 1
            changed = True
            if text:
                self.chapter_texts.append(text)
        self.futures = pending
        if changed:
            self._set_state()

    def _chapter_for_page(self, ui_text: str) -> str:
        """Flush the previous chapter when a page exposes a new chapter title."""
        fallback = self.current_chapter or f"第{self.chapter_index + 1}章"
        chapter = extract_chapter_label(ui_text, fallback)
        key = _chapter_key(chapter)
        if (self.current_chapter_key and key and key != self.current_chapter_key
                and (self.futures or self.chapter_texts or self.chapter_pages)):
            self.flush_chapter(self.current_chapter, final=False)
        self.current_chapter = chapter
        self.current_chapter_key = key
        return chapter

    def submit_page(self, image_bytes: bytes, *, page_no: int | None = None, ui_text: str = "") -> bool:
        if self._closed or not self.client.available or not image_bytes:
            return False
        if self.submitted >= max(1, self.settings.max_pages):
            self._set_state(status="limit", error=f"已达到视觉页数上限 {self.settings.max_pages}")
            return False
        self._drain_completed()
        chapter = self._chapter_for_page(ui_text)
        # Avoid duplicate OCR when a popup or a failed swipe leaves the same page
        # on screen for more than one polling cycle.
        fingerprint = str(hash(image_bytes[:4096]))
        if fingerprint == self.last_hash:
            return False
        self.last_hash = fingerprint
        self.submitted += 1
        self.chapter_pages += 1
        self._set_state(status="extracting", chapter=chapter, current_page=page_no or 0)
        future = self.executor.submit(self.client.extract_page, image_bytes, page_no)
        self.futures.append((self.submitted, future))
        return True

    def submit_page_content(self, page_text: str, *, page_no: int | None = None, ui_text: str = "") -> bool:
        """Accept already-extracted page content when a device exposes text.

        This is useful for accessibility/UI-dump integrations and gives callers
        a no-image fallback.  Chapter summarisation still runs through the same
        OpenAI session and status pipeline.
        """
        if self._closed or not self.client.available or not str(page_text or "").strip():
            return False
        if self.submitted >= max(1, self.settings.max_pages):
            self._set_state(status="limit", error=f"已达到视觉页数上限 {self.settings.max_pages}")
            return False
        self._drain_completed()
        chapter = self._chapter_for_page(ui_text)
        text = str(page_text).strip()[: self.settings.max_chars_per_page]
        fingerprint = str(hash(text))
        if fingerprint == self.last_hash:
            return False
        self.last_hash = fingerprint
        self.submitted += 1
        self.chapter_pages += 1
        self._set_state(status="extracting", chapter=chapter, current_page=page_no or 0)
        self.futures.append((self.submitted, self.executor.submit(lambda: text)))
        return True

    def flush_chapter(self, chapter: str = "", *, final: bool = False) -> str:
        if self._closed:
            return ""
        self._drain_completed(wait=True)
        page_texts = self.chapter_texts
        self.chapter_texts = []
        self.page_texts.extend(page_texts)
        chapter = chapter or self.current_chapter or f"第{self.chapter_index + 1}章"
        if not page_texts:
            self.chapter_pages = 0
            self._set_state(status="idle" if not final else "done", chapter=chapter, summary="")
            if self.chapter_pages or self.current_chapter:
                self.chapter_index += 1
            self.current_chapter = ""
            self.current_chapter_key = ""
            return ""
        summary = ""
        if self.settings.summary_enabled:
            self._set_state(status="summarizing", chapter=chapter, current_page=0, error="")
            try:
                summary = self.client.summarize_chapter(chapter, page_texts)
            except Exception as exc:
                self._set_state(status="error", chapter=chapter, error=str(exc)[:500])
                self.log.warning("《%s》章节总结失败: %s", self.book, exc)
        if summary:
            old_summaries = []
            try:
                old_summaries = list((self.runtime.get().get("vision") or {}).get("summaries") or [])
            except Exception:
                pass
            record = {"chapter": chapter, "summary": summary, "pages": len(page_texts), "at": datetime.now().isoformat(timespec="seconds")}
            summaries = (old_summaries + [record])[-30:]
            self._set_state(status="done" if final else "idle", chapter=chapter, summary=summary, summaries=summaries, error="")
            self._persist(record)
        else:
            self._set_state(status="done" if final else "idle", chapter=chapter, error="" if not self.failed else (self.runtime.get().get("vision") or {}).get("error", ""))
        self.chapter_index += 1
        self.chapter_pages = 0
        self.current_chapter = ""
        self.current_chapter_key = ""
        return summary

    def _persist(self, record: dict) -> None:
        if not self.persist_path:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                try:
                    data = json.loads(self.persist_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = {}
                key = f"{self.serial}:{self.book}"
                items = list(data.get(key) or [])
                items.append(record)
                data[key] = items[-30:]
                self.persist_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self.log.debug("视觉摘要持久化失败: %s", exc)

    def close(self) -> None:
        if self._closed:
            return
        if self.futures or self.chapter_texts or self.chapter_pages:
            try:
                self.flush_chapter(self.current_chapter, final=True)
            except Exception as exc:
                self.log.warning("《%s》关闭视觉阅读会话时总结失败: %s", self.book, exc)
        self._closed = True
        self.executor.shutdown(wait=False, cancel_futures=False)


def settings_from_config(ai: dict[str, Any] | None, env: dict[str, str] | None = None) -> VisionSettings:
    """Build settings from the existing ``ai:`` YAML section."""
    ai = ai or {}
    env = env or os.environ
    vision = ai.get("vision") if isinstance(ai.get("vision"), dict) else {}
    key = str(ai.get("api_key", "") or "").strip()
    if key.startswith("${") and key.endswith("}"):
        key = env.get(key[2:-1], "")
    key = key or env.get("OPENAI_API_KEY", "")
    def number(name: str, default: int) -> int:
        try:
            return int(vision.get(name, ai.get(name, default)) or default)
        except (TypeError, ValueError):
            return default
    return VisionSettings(
        enabled=bool(vision.get("enabled", ai.get("vision_enabled", False))) and bool(key),
        base_url=str(vision.get("base_url", ai.get("base_url", "")) or "").strip().rstrip("/"),
        api_key=key,
        model=str(vision.get("model", ai.get("vision_model", ai.get("model", "gpt-4o-mini"))) or "gpt-4o-mini"),
        summary_model=str(vision.get("summary_model", ai.get("model", "")) or ""),
        timeout=max(5, number("timeout", 30)),
        detail=str(vision.get("detail", "high") or "high"),
        max_pages=max(1, number("max_pages", 240)),
        max_chars_per_page=max(500, number("max_chars_per_page", 5000)),
        summary_enabled=bool(vision.get("summary_enabled", True)),
        summary_max_chars=max(2000, number("summary_max_chars", 14000)),
        concurrency=min(4, max(1, number("concurrency", 2))),
    )
