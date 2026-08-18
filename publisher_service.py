# -*- coding: utf-8 -*-
"""番茄作家后台发布服务。

Playwright 只在工作线程中按需导入，网页请求只负责启动任务和读取状态。
账号 storage state、路径配置和发布稿件全部保存在 publisher_data 下。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


WRITER_URL = "https://fanqienovel.com/main/writer/?enter_from=author_zone"
BOOK_MANAGE_URL = "https://fanqienovel.com/main/writer/book-manage"
DASHBOARD_KEYWORDS = ("作品管理", "章节管理", "创建新书", "新建作品", "作家中心")
CHAPTER_RE = re.compile(r"第\s*([0-9零〇一二三四五六七八九十百千万两]+)\s*章")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
RUNNING_STATES = {"logging_in", "running", "stopping"}


def _cn_number(value: str) -> int:
    """把常见中文章节号转换成整数。"""
    value = value.strip()
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = section = number = 0
    for char in value:
        if char in digits:
            number = digits[char]
        elif char in units:
            unit = units[char]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = number = 0
            else:
                section += (number or 1) * unit
                number = 0
        else:
            raise ValueError(f"无法识别章节号: {value}")
    result = total + section + number
    if result <= 0:
        raise ValueError(f"无效章节号: {value}")
    return result


def _chapter_sort_key(path: Path) -> tuple[int, str]:
    match = CHAPTER_RE.search(path.stem)
    if match:
        try:
            return _cn_number(match.group(1)), path.name.casefold()
        except ValueError:
            pass
    fallback = re.search(r"\d+", path.stem)
    return (int(fallback.group()) if fallback else 10**12, path.name.casefold())


def _redact(message: object) -> str:
    text = str(message)
    text = re.sub(r"(?i)(authorization|api[-_ ]?key|cookie|token)\s*[:=]\s*[^\s,;]+", r"\1=[已隐藏]", text)
    text = re.sub(r"(?i)bearer\s+[a-z0-9._~+/=-]+", "Bearer [已隐藏]", text)
    return text


@dataclass(frozen=True)
class Chapter:
    path: Path
    number: int
    title: str
    content: str

    @property
    def label(self) -> str:
        return f"第{self.number}章 {self.title}".strip()


def parse_chapter(path: Path) -> Chapter:
    """从文件名和第一行提取章节号、标题和正文。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="gb18030")
    lines = text.splitlines()
    first = lines[0].strip() if lines else ""
    match = CHAPTER_RE.search(path.stem) or CHAPTER_RE.search(first)
    if not match:
        raise ValueError("文件名或首行缺少“第X章”")
    number = _cn_number(match.group(1))

    title = ""
    first_match = CHAPTER_RE.search(first)
    if first_match:
        title = first[first_match.end():].lstrip(" ：:_-—").strip()
        body_lines = lines[1:]
    else:
        stem_match = CHAPTER_RE.search(path.stem)
        title = path.stem[stem_match.end():].lstrip(" ：:_-—").strip() if stem_match else ""
        body_lines = lines
    if not title:
        stem_match = CHAPTER_RE.search(path.stem)
        if stem_match:
            title = path.stem[stem_match.end():].lstrip(" ：:_-—").strip()
    content = "\n".join(body_lines).strip()
    if not title:
        raise ValueError("章节标题为空")
    if not content:
        raise ValueError("章节正文为空")
    return Chapter(path=path, number=number, title=title, content=content)


class PublisherManager:
    """单账号、单工作线程的网页发布任务管理器。"""

    def __init__(self, base_dir: Path, automation_factory: Callable[..., Any] | None = None):
        self.base_dir = Path(base_dir).resolve()
        self.data_dir = self.base_dir / "publisher_data"
        self.config_file = self.data_dir / "config.json"
        self.state_file = self.data_dir / "account_state.json"
        self.pending_state_file = self.data_dir / "account_state.pending.json"
        self._automation_factory = automation_factory
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._logs: deque[dict[str, Any]] = deque(maxlen=500)
        self._log_seq = 0
        self._status: dict[str, Any] = {
            "state": "idle", "operation": "", "mode": "", "task_id": "",
            "message": "等待操作", "book": "", "chapter": "", "current": 0,
            "total": 0, "success": 0, "failed": 0, "skipped": 0,
            "started_at": "", "ended_at": "", "error": "",
        }
        self._ensure_data_dirs()

    def _ensure_data_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        cfg = self._load_config_raw()
        Path(cfg["source_dir"]).mkdir(parents=True, exist_ok=True)
        Path(cfg["archive_dir"]).mkdir(parents=True, exist_ok=True)

    def _defaults(self) -> dict[str, Any]:
        return {
            "source_dir": str(self.data_dir / "chapters"),
            "archive_dir": str(self.data_dir / "uploaded"),
            "visible_browser": True,
        }

    def _load_config_raw(self) -> dict[str, Any]:
        cfg = self._defaults()
        try:
            loaded = json.loads(self.config_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update({key: loaded[key] for key in cfg if key in loaded})
        except (OSError, ValueError, TypeError):
            pass
        for key in ("source_dir", "archive_dir"):
            value = Path(str(cfg[key])).expanduser()
            if not value.is_absolute():
                value = self.base_dir / value
            cfg[key] = str(value.resolve())
        cfg["visible_browser"] = bool(cfg.get("visible_browser", True))
        return cfg

    def get_config(self) -> dict[str, Any]:
        cfg = self._load_config_raw()
        cfg["login_state_exists"] = self.state_file.is_file()
        return cfg

    def save_config(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._status["state"] in RUNNING_STATES:
                raise RuntimeError("发布任务运行中，不能修改目录")
            current = self._load_config_raw()
            for key in ("source_dir", "archive_dir"):
                if key in data:
                    raw = str(data.get(key, "")).strip()
                    if not raw:
                        raise ValueError(f"{key} 不能为空")
                    path = Path(raw).expanduser()
                    if not path.is_absolute():
                        path = self.base_dir / path
                    current[key] = str(path.resolve())
            if "visible_browser" in data:
                current["visible_browser"] = bool(data["visible_browser"])

            source = Path(current["source_dir"])
            archive = Path(current["archive_dir"])
            if source == archive or source in archive.parents or archive in source.parents:
                raise ValueError("待发目录和归档目录不能相同，也不能互相包含")
            source.mkdir(parents=True, exist_ok=True)
            archive.mkdir(parents=True, exist_ok=True)
            payload = {key: current[key] for key in self._defaults()}
            tmp = self.config_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.config_file)
            return self.get_config()

    def list_books(self) -> list[dict[str, Any]]:
        cfg = self._load_config_raw()
        source = Path(cfg["source_dir"])
        if not source.is_dir():
            return []
        books = []
        for folder in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            chapters = sorted(folder.glob("*.txt"), key=_chapter_sort_key)
            if not chapters:
                continue
            valid = 0
            for path in chapters:
                try:
                    parse_chapter(path)
                    valid += 1
                except (OSError, ValueError):
                    pass
            books.append({"name": folder.name, "count": len(chapters), "valid": valid,
                          "invalid": len(chapters) - valid})
        return books

    def status(self, after: int = 0) -> dict[str, Any]:
        with self._lock:
            state = dict(self._status)
            state["logged_in"] = self.state_file.is_file()
            state["can_start"] = state["state"] not in RUNNING_STATES
            state["logs"] = [dict(item) for item in self._logs if item["id"] > max(0, int(after))]
            state["last_log_id"] = self._log_seq
            return state

    def _set_status(self, **changes: Any) -> None:
        with self._lock:
            self._status.update(changes)

    def _log(self, message: object, level: str = "info") -> None:
        safe = _redact(message)
        with self._lock:
            self._log_seq += 1
            self._logs.append({
                "id": self._log_seq,
                "time": dt.datetime.now().strftime("%H:%M:%S"),
                "level": level,
                "message": safe,
            })

    def _begin(self, operation: str, mode: str = "") -> str:
        with self._lock:
            if self._status["state"] in RUNNING_STATES or (self._thread and self._thread.is_alive()):
                raise RuntimeError("已有登录或发布任务正在运行")
            task_id = uuid.uuid4().hex
            self._stop.clear()
            self._status.update({
                "state": "logging_in" if operation == "login" else "running",
                "operation": operation, "mode": mode, "task_id": task_id,
                "message": "正在打开登录页面" if operation == "login" else "正在准备发布队列",
                "book": "", "chapter": "", "current": 0, "total": 0,
                "success": 0, "failed": 0, "skipped": 0,
                "started_at": dt.datetime.now().isoformat(timespec="seconds"),
                "ended_at": "", "error": "",
            })
            return task_id

    def start_login(self) -> str:
        task_id = self._begin("login")
        self._thread = threading.Thread(target=self._login_worker, daemon=True, name="publisher-login")
        self._thread.start()
        return task_id

    def start_publish(self, data: dict[str, Any]) -> str:
        mode = str(data.get("mode", "immediate")).strip().lower()
        if mode not in {"immediate", "scheduled", "republish"}:
            raise ValueError("不支持的发布模式")
        book = str(data.get("book", "")).strip()
        known = {item["name"] for item in self.list_books()}
        if not book or book not in known:
            raise ValueError("请选择待发目录中存在的小说")
        if not self.state_file.is_file():
            raise RuntimeError("尚未登录番茄作家后台")

        options = {
            "book": book,
            "mode": mode,
            "count": max(0, min(1000, int(data.get("count") or 0))),
            "volume": max(1, min(100, int(data.get("volume") or 1))),
            "ai_declare": "是" if bool(data.get("ai_declare", False)) else "否",
            "start_date": str(data.get("start_date", "")).strip(),
            "publish_time": str(data.get("publish_time", "00:01")).strip(),
            "per_day": max(1, min(20, int(data.get("per_day") or 2))),
            "chapter_spec": str(data.get("chapter_spec", "all")).strip() or "all",
        }
        if mode == "scheduled":
            if not options["start_date"]:
                options["start_date"] = (dt.date.today() + dt.timedelta(days=1)).isoformat()
            try:
                start_date = dt.date.fromisoformat(options["start_date"])
            except ValueError as exc:
                raise ValueError("定时日期必须是 YYYY-MM-DD") from exc
            if start_date < dt.date.today():
                raise ValueError("定时日期不能早于今天")
            if not TIME_RE.fullmatch(options["publish_time"]):
                raise ValueError("定时时间必须是 HH:MM")
        if mode == "republish":
            _parse_chapter_spec(options["chapter_spec"])

        task_id = self._begin("publish", mode)
        self._set_status(book=book)
        self._thread = threading.Thread(
            target=self._publish_worker, args=(options,), daemon=True, name="publisher-run")
        self._thread.start()
        return task_id

    def stop(self) -> str:
        with self._lock:
            if self._status["state"] not in RUNNING_STATES:
                return "当前没有发布任务"
            self._stop.set()
            self._status.update(state="stopping", message="已请求停止，将在当前动作结束后生效")
        self._log("收到停止请求，将保留尚未确认成功的源文件", "warning")
        return "已请求停止"

    def _sync_playwright(self):
        if self._automation_factory is not None:
            return self._automation_factory()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("尚未安装 Playwright，请先执行 pip install playwright") from exc
        return sync_playwright()

    @staticmethod
    def _launch_browser(playwright, visible: bool):
        errors = []
        headless = not visible
        local_appdata = Path(os.environ.get("LOCALAPPDATA", ""))
        roaming_appdata = Path(os.environ.get("APPDATA", ""))
        executable_candidates = [
            local_appdata / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            roaming_appdata / "Microsoft" / "Edge" / "App" / "msedge.exe",
            Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ]
        for root in (Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft" / "EdgeCore",
                     Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft" / "EdgeCore"):
            try:
                executable_candidates.extend(sorted(root.glob("*/msedge.exe"), reverse=True))
            except OSError:
                pass
        for executable in executable_candidates:
            if not executable.is_file():
                continue
            try:
                return playwright.chromium.launch(executable_path=str(executable), headless=headless)
            except Exception as exc:
                errors.append(str(exc))
        for options in (
            {"channel": "msedge", "headless": headless},
            {"channel": "chrome", "headless": headless},
            {"headless": headless},
        ):
            try:
                return playwright.chromium.launch(**options)
            except Exception as exc:
                errors.append(str(exc))
        raise RuntimeError("无法启动 Edge/Chrome/Chromium: " + _redact(errors[-1] if errors else "未知错误"))

    def _find_dashboard(self, context):
        for page in list(context.pages):
            try:
                if "fanqienovel.com" not in page.url:
                    continue
                body = page.locator("body").inner_text(timeout=2000)
                if any(keyword in body for keyword in DASHBOARD_KEYWORDS):
                    return page
            except Exception:
                continue
        return None

    def _login_worker(self) -> None:
        browser = None
        try:
            cfg = self._load_config_raw()
            self._log("正在打开 Edge 登录番茄作家后台")
            with self._sync_playwright() as playwright:
                browser = self._launch_browser(playwright, visible=cfg["visible_browser"])
                kwargs = {"storage_state": str(self.state_file)} if self.state_file.is_file() else {}
                context = browser.new_context(**kwargs)
                page = context.new_page()
                page.goto(WRITER_URL, timeout=60000, wait_until="domcontentloaded")
                self._set_status(message="请在弹出的浏览器中扫码登录")
                self._log("请在弹出的浏览器中完成扫码；检测到作家后台后会自动保存")
                deadline = time.time() + 1800
                while time.time() < deadline and not self._stop.is_set():
                    if self._find_dashboard(context) is not None:
                        time.sleep(2)
                        context.storage_state(path=str(self.pending_state_file))
                        os.replace(self.pending_state_file, self.state_file)
                        self._set_status(state="done", message="登录成功", ended_at=_now(), error="")
                        self._log("登录成功，凭证已仅保存在本机私有目录", "success")
                        return
                    time.sleep(2)
                if self._stop.is_set():
                    self._set_status(state="done", message="登录已取消", ended_at=_now())
                    self._log("登录已取消，原有凭证未被修改", "warning")
                else:
                    raise TimeoutError("等待扫码登录超时")
        except Exception as exc:
            self._set_status(state="error", message="登录失败", error=_redact(exc), ended_at=_now())
            self._log(f"登录失败: {exc}", "error")
        finally:
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            try:
                self.pending_state_file.unlink(missing_ok=True)
            except OSError:
                pass

    def _book_dir(self, book: str) -> Path:
        source = Path(self._load_config_raw()["source_dir"]).resolve()
        target = (source / book).resolve()
        if target.parent != source or not target.is_dir():
            raise ValueError("小说目录无效")
        return target

    def _load_chapters(self, options: dict[str, Any]) -> tuple[list[Chapter], list[tuple[Path, str]]]:
        paths = sorted(self._book_dir(options["book"]).glob("*.txt"), key=_chapter_sort_key)
        selected = _parse_chapter_spec(options["chapter_spec"]) if options["mode"] == "republish" else None
        parsed: list[Chapter] = []
        invalid: list[tuple[Path, str]] = []
        for path in paths:
            try:
                chapter = parse_chapter(path)
                if selected is None or chapter.number in selected:
                    parsed.append(chapter)
            except (OSError, ValueError) as exc:
                invalid.append((path, str(exc)))
        if options["count"]:
            parsed = parsed[:options["count"]]
        return parsed, invalid

    def _publish_worker(self, options: dict[str, Any]) -> None:
        browser = None
        try:
            chapters, invalid = self._load_chapters(options)
            for path, reason in invalid:
                self._log(f"跳过格式错误文件 {path.name}: {reason}", "error")
            if not chapters:
                raise RuntimeError("没有符合格式和筛选条件的章节")
            self._set_status(total=len(chapters), failed=len(invalid), message="正在启动自动化浏览器")
            self._log(f"《{options['book']}》已建立队列，共 {len(chapters)} 章，模式: {_mode_label(options['mode'])}")

            cfg = self._load_config_raw()
            with self._sync_playwright() as playwright:
                browser = self._launch_browser(playwright, visible=cfg["visible_browser"])
                context = browser.new_context(storage_state=str(self.state_file))
                root_page = context.new_page()
                root_page.goto(BOOK_MANAGE_URL, timeout=60000, wait_until="domcontentloaded")
                if not self._find_dashboard(context):
                    raise RuntimeError("登录状态已过期，请重新扫码登录")

                for index, chapter in enumerate(chapters, start=1):
                    if self._stop.is_set():
                        break
                    self._set_status(current=index, chapter=chapter.label,
                                     message=f"正在处理 {chapter.label}")
                    self._log(f"[{index}/{len(chapters)}] 开始处理 {chapter.label}")
                    try:
                        result = self._submit_chapter(root_page, context, chapter, options, index)
                        if result == "skipped":
                            self._increment("skipped")
                            continue
                        if not result:
                            raise RuntimeError("平台未确认章节提交成功")
                        try:
                            archived = self._archive_chapter(chapter, options)
                        except Exception as exc:
                            self._increment("failed")
                            self._log(f"平台已确认成功，但归档失败: {exc}。为避免重复发布，队列已停止", "error")
                            self._stop.set()
                            self._set_status(error=f"平台成功但归档失败: {_redact(exc)}")
                            break
                        self._increment("success")
                        self._log(f"平台已确认，原稿已归档: {archived}", "success")
                    except Exception as exc:
                        self._increment("failed")
                        self._log(f"{chapter.label} 失败，原文件保持不动: {exc}", "error")
                    self._close_extra_pages(context, root_page)

            stopped = self._stop.is_set()
            status = self.status()
            message = (f"已停止：成功 {status['success']}，失败 {status['failed']}，跳过 {status['skipped']}"
                       if stopped else
                       f"发布完成：成功 {status['success']}，失败 {status['failed']}，跳过 {status['skipped']}")
            self._set_status(state="done", message=message, ended_at=_now(), chapter="")
            self._log(message, "warning" if stopped else "success")
        except Exception as exc:
            self._set_status(state="error", message="发布任务失败", error=_redact(exc), ended_at=_now())
            self._log(f"发布任务失败: {exc}", "error")
        finally:
            try:
                if browser:
                    browser.close()
            except Exception:
                pass

    def _increment(self, key: str) -> None:
        with self._lock:
            self._status[key] = int(self._status.get(key, 0)) + 1

    @staticmethod
    def _close_extra_pages(context, keep) -> None:
        for page in list(context.pages):
            if page != keep:
                try:
                    page.close()
                except Exception:
                    pass

    def _submit_chapter(self, root_page, context, chapter: Chapter,
                        options: dict[str, Any], index: int) -> bool | str:
        manage = _open_chapter_manage(root_page, options["book"])
        row, row_text = _find_chapter_row(manage, chapter.number)
        mode = options["mode"]
        if mode != "republish" and row is not None and "已发布" in row_text:
            self._log(f"{chapter.label} 已在平台发布，跳过且保留本地原稿", "warning")
            return "skipped"
        if mode == "republish" and row is None:
            self._log(f"{chapter.label} 在平台不存在，跳过且保留本地原稿", "warning")
            return "skipped"

        editor = _open_editor(manage, context, row)
        if row is None:
            editor = _create_editor(manage, context)
        _dismiss_popups(editor)
        if options["volume"] > 1 and row is None:
            _select_volume(editor, options["volume"])
        _fill_editor(editor, chapter)
        modal = _open_final_panel(editor)
        if not _set_ai_declare(modal, options["ai_declare"]):
            self._log("平台当前页面没有可选的 AI 声明，沿用平台默认值", "warning")
        if mode == "scheduled":
            start = dt.date.fromisoformat(options["start_date"])
            target = start + dt.timedelta(days=(index - 1) // options["per_day"])
            _set_schedule(modal, target.isoformat(), options["publish_time"])
            self._log(f"已设置定时发布: {target.isoformat()} {options['publish_time']}")

        confirm = modal.get_by_role("button", name="确认发布").first
        confirm.click(force=True)
        if not _wait_modal_closed(editor, confirm, modal):
            detail = ""
            try:
                detail = modal.inner_text(timeout=2000)[:300].replace("\n", " | ")
            except Exception:
                pass
            raise RuntimeError("确认发布后面板未关闭" + (f": {detail}" if detail else ""))

        editor.wait_for_timeout(3500)
        verified = _verify_chapter(root_page, options["book"], chapter.number)
        if not verified:
            raise RuntimeError("提交面板已关闭，但章节管理列表中未找到该章节")
        return True

    def _archive_chapter(self, chapter: Chapter, options: dict[str, Any]) -> Path:
        cfg = self._load_config_raw()
        destination = Path(cfg["archive_dir"]) / options["book"]
        if options["mode"] == "republish":
            destination /= "重新提交"
        elif options["mode"] == "scheduled":
            destination /= "定时发布"
        else:
            destination /= _volume_name(options["volume"])
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / chapter.path.name
        if target.exists():
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            target = destination / f"{chapter.path.stem}_{stamp}{chapter.path.suffix}"
        shutil.move(str(chapter.path), str(target))
        return target


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _mode_label(mode: str) -> str:
    return {"immediate": "立即发布", "scheduled": "定时发布", "republish": "重新提交"}.get(mode, mode)


def _parse_chapter_spec(spec: str) -> set[int] | None:
    if spec.strip().lower() in {"", "all", "全部"}:
        return None
    numbers: set[int] = set()
    for part in re.split(r"[,，]", spec):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            start, end = int(left), int(right)
            if start <= 0 or end < start or end - start > 1000:
                raise ValueError("章节范围无效")
            numbers.update(range(start, end + 1))
        else:
            value = int(part)
            if value <= 0:
                raise ValueError("章节号必须大于 0")
            numbers.add(value)
    if not numbers:
        raise ValueError("章节范围为空")
    return numbers


def _dismiss_popups(page) -> None:
    for _ in range(3):
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    for text in ("我知道了", "知道了", "跳过", "完成", "关闭"):
        try:
            button = page.get_by_text(text, exact=True).first
            if button.is_visible(timeout=350):
                button.click(force=True)
                page.wait_for_timeout(250)
        except Exception:
            pass


def _open_chapter_manage(page, book: str):
    page.goto(BOOK_MANAGE_URL, timeout=60000, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    _dismiss_popups(page)
    clicked = False
    cards = page.locator("div, li, section, article").filter(has_text=book)
    for index in range(cards.count() - 1, -1, -1):
        try:
            card = cards.nth(index)
            if not card.is_visible():
                continue
            card.hover(timeout=2000)
            button = card.get_by_text("章节管理", exact=False).first
            if button.is_visible(timeout=800):
                button.click(force=True)
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        button = page.get_by_text("章节管理", exact=False).first
        button.click(force=True, timeout=8000)
    page.wait_for_timeout(3500)
    manage = page.context.pages[-1] if len(page.context.pages) > 1 else page
    _dismiss_popups(manage)
    return manage


def _find_chapter_row(page, number: int):
    pattern = re.compile(rf"第\s*{number}\s*章")
    for selector in ("tr", "li", ".chapter-item"):
        try:
            rows = page.locator(selector).all()
            for row in rows:
                try:
                    text = row.inner_text(timeout=1000)
                    if pattern.search(text):
                        return row, text
                except Exception:
                    continue
        except Exception:
            continue
    return None, ""


def _open_editor(manage, context, row):
    before = len(context.pages)
    try:
        link = row.locator('a[href*="publish"]').first
        if link.is_visible(timeout=800):
            link.click(force=True)
        else:
            row.click(force=True)
    except Exception:
        row.click(force=True)
    manage.wait_for_timeout(4000)
    return context.pages[-1] if len(context.pages) > before else manage


def _create_editor(manage, context):
    before = len(context.pages)
    button = manage.get_by_text("新建章节", exact=False).first
    button.click(force=True, timeout=8000)
    manage.wait_for_timeout(2500)
    try:
        second = manage.get_by_text("新建章节", exact=False).first
        if second.is_visible(timeout=800):
            second.click(force=True)
    except Exception:
        pass
    manage.wait_for_timeout(3500)
    return context.pages[-1] if len(context.pages) > before else manage


def _fill_editor(page, chapter: Chapter) -> None:
    _dismiss_popups(page)
    inputs = page.locator('input[type="text"]')
    number_input = inputs.first
    if number_input.is_visible(timeout=1500):
        number_input.fill(str(chapter.number), force=True)
    title = page.get_by_placeholder("请输入标题", exact=False).first
    if not title.is_visible(timeout=1000):
        title = page.get_by_placeholder("请输入章节名", exact=False).first
    if not title.is_visible(timeout=1000):
        title = inputs.last
    if not title.is_visible(timeout=1500):
        raise RuntimeError("找不到章节标题输入框")
    title.fill(chapter.title, force=True)

    editor = page.locator(".ql-editor").first
    if not editor.is_visible(timeout=1000):
        editor = page.locator(".ProseMirror").first
    if not editor.is_visible(timeout=1000):
        editor = page.locator('[contenteditable="true"]').first
    if not editor.is_visible(timeout=2000):
        raise RuntimeError("找不到正文编辑器")
    handle = editor.element_handle()
    page.evaluate("""([el, text]) => {
        el.focus(); el.innerText = text;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
    }""", [handle, chapter.content])
    editor.click(force=True)
    page.keyboard.press("End")
    page.keyboard.press("Space")
    page.keyboard.press("Backspace")
    page.wait_for_timeout(1200)


def _open_final_panel(page):
    button = page.locator(".publish-header-right").get_by_text("下一步", exact=True).first
    if button.count() == 0 or not button.is_visible(timeout=1000):
        button = page.get_by_text("下一步", exact=True).last
    button.click(force=True, timeout=10000)
    page.wait_for_timeout(2200)
    for text in ("提交", "继续发布", "我知道了"):
        try:
            popup = page.get_by_role("button", name=text).last
            if popup.is_visible(timeout=700):
                popup.click(force=True)
                page.wait_for_timeout(1200)
        except Exception:
            pass
    try:
        basic = page.get_by_text("仅基础检测", exact=False).first
        if basic.is_visible(timeout=1200):
            basic.click(force=True)
            page.wait_for_timeout(1200)
    except Exception:
        pass
    modal = page.locator(".arco-modal").last
    modal.get_by_role("button", name="确认发布").wait_for(state="visible", timeout=15000)
    return modal


def _set_ai_declare(modal, value: str) -> bool:
    input_value = "1" if value == "是" else "2"
    try:
        label = modal.locator("label.arco-radio").filter(has_text=value).first
        label.click(force=True)
        modal.page.wait_for_timeout(350)
        radio = modal.locator(f'input[type="radio"][value="{input_value}"]').first
        if not radio.is_checked():
            modal.page.evaluate("""el => {
                el.checked = true;
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('click', {bubbles: true}));
            }""", radio.element_handle())
        return True
    except Exception:
        return False


def _set_schedule(modal, date_value: str, time_value: str) -> None:
    switch = modal.locator('[role="switch"]').first
    if switch.is_visible(timeout=1500) and switch.get_attribute("aria-checked") != "true":
        switch.click(force=True)
        modal.page.wait_for_timeout(700)
    date_input = modal.locator('input[placeholder="请选择日期"]').first
    time_input = modal.locator('input[placeholder="请选择时间"]').first
    date_input.click(force=True)
    date_input.fill(date_value, force=True)
    modal.page.keyboard.press("Enter")
    modal.page.wait_for_timeout(500)
    time_input.click(force=True)
    time_input.fill(time_value, force=True)
    modal.page.keyboard.press("Enter")
    modal.page.wait_for_timeout(500)
    if date_input.input_value() != date_value or time_input.input_value() != time_value:
        raise RuntimeError("平台定时日期或时间没有正确保存")


def _select_volume(page, volume: int) -> None:
    target_names = (_volume_name(volume), f"第{volume}卷", f"卷{volume}")
    triggers = page.get_by_text(re.compile(r"第[一二三四五六七八九十百0-9]+卷"))
    opened = False
    for index in range(min(triggers.count(), 8)):
        try:
            trigger = triggers.nth(index)
            if trigger.is_visible(timeout=300):
                trigger.click(force=True)
                page.wait_for_timeout(500)
                if page.get_by_text("新建分卷", exact=False).first.is_visible(timeout=500):
                    opened = True
                    break
        except Exception:
            continue
    if not opened:
        raise RuntimeError(f"无法打开分卷选择器，请先在平台确认第{volume}卷存在")
    for name in target_names:
        try:
            item = page.get_by_text(name, exact=False).last
            if item.is_visible(timeout=500):
                item.click(force=True)
                confirm = page.get_by_role("button", name="确定").last
                if confirm.is_visible(timeout=800):
                    confirm.click(force=True)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue
    page.keyboard.press("Escape")
    raise RuntimeError(f"平台没有找到目标分卷: {_volume_name(volume)}")


def _wait_modal_closed(page, confirm, modal) -> bool:
    for _ in range(12):
        page.wait_for_timeout(1000)
        try:
            if not confirm.is_visible(timeout=500) or not modal.is_visible(timeout=500):
                return True
        except Exception:
            return True
        for text in ("提交", "继续发布", "确认", "确定"):
            try:
                popup = page.get_by_role("button", name=text).last
                if popup.is_visible(timeout=250) and popup != confirm:
                    popup.click(force=True)
                    break
            except Exception:
                pass
    return False


def _verify_chapter(root_page, book: str, number: int) -> bool:
    for _ in range(3):
        try:
            manage = _open_chapter_manage(root_page, book)
            row, _ = _find_chapter_row(manage, number)
            if row is not None:
                return True
        except Exception:
            pass
        root_page.wait_for_timeout(1800)
    return False


def _volume_name(number: int) -> str:
    chars = "零一二三四五六七八九"
    if number < 10:
        value = chars[number]
    elif number == 10:
        value = "十"
    elif number < 20:
        value = "十" + chars[number % 10]
    elif number < 100:
        value = chars[number // 10] + "十" + (chars[number % 10] if number % 10 else "")
    else:
        value = str(number)
    return f"第{value}卷"


PUBLISHER = PublisherManager(Path(__file__).resolve().parent)
