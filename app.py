# -*- coding: utf-8 -*-
"""番茄多书每日任务 - 网页控制台服务(多设备版).

- 常驻后台: 每天 00:01 后自动为所有「已接入」设备各执行一轮任务(当天错过会补跑)
- 每台设备独立配置书籍/送礼物/已完成标记(devices.json)
- 网页控制台: 多设备连接 / 每台设备独立画面输出 / 按设备编辑书籍
- 访问: http://127.0.0.1:8899
"""

from __future__ import annotations

import io
import json
import logging
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"
HTML_FILE = BASE_DIR / "dashboard.html"
LAST_RUN_FILE = BASE_DIR / "last_run.txt"
SERVICE_LOG = BASE_DIR / "service.log"

sys.path.insert(0, str(BASE_DIR))
from fanqie_reader import (  # noqa: E402
    Config,
    FanqieBot,
    RuntimeStatus,
    connect_device,
    load_books_for_serial,
    load_config,
    load_devices,
    save_devices,
    update_due,
)

HOST = "0.0.0.0"  # 监听所有网卡: 本机 http://127.0.0.1:8899, 局域网 http://<本机IP>:8899
PORT = 8899

# ---------- 运行健康度采集(纯标准库, Windows) ----------
APP_START_TIME = time.time()
_CPU_SAMPLE = None  # (time, idle_seconds, total_seconds)


def _health_metrics() -> dict:
    """采集 CPU/内存/磁盘/运行时长/应用内存等健康指标(失败项兜底为 0)."""
    uptime_os = 0
    mem_load = 0
    mem_total_gb = 0
    disk = disk_total = disk_free = 0
    app_mem = 0
    try:
        import ctypes
        import shutil
        from ctypes import wintypes

        # 系统运行时长(GetTickCount64 -> 秒)
        try:
            uptime_os = round(ctypes.windll.kernel32.GetTickCount64() / 1000)
        except Exception:
            pass

        # 内存占用(GlobalMemoryStatusEx.dwMemoryLoad 直接是百分比)
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            _GlobalMemoryStatusEx = ctypes.windll.kernel32.GlobalMemoryStatusEx
            _GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
            _GlobalMemoryStatusEx.restype = wintypes.BOOL
            if _GlobalMemoryStatusEx(ctypes.byref(m)):
                mem_load = int(m.dwMemoryLoad)
                mem_total_gb = round(m.ullTotalPhys / 2 ** 30, 1)
        except Exception:
            pass

        # 磁盘占用
        try:
            du = shutil.disk_usage(BASE_DIR)
            disk = round(du.used / du.total * 100, 1)
            disk_total = round(du.total / 2 ** 30, 1)
            disk_free = round(du.free / 2 ** 30, 1)
        except Exception:
            pass

        # 应用内存(本进程 WorkingSetSize -> MB)
        try:
            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            _GetProcessMemoryInfo = ctypes.windll.psapi.GetProcessMemoryInfo
            _GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
            _GetProcessMemoryInfo.restype = wintypes.BOOL
            if _GetProcessMemoryInfo(
                    ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), ctypes.sizeof(pmc)):
                app_mem = round(pmc.WorkingSetSize / 1024 / 1024, 1)
        except Exception:
            pass
    except Exception:
        pass
    return {
        "uptime_os": uptime_os,
        "uptime_srv": round(time.time() - APP_START_TIME),
        "cpu": _cpu_percent(),
        "mem": mem_load,
        "disk": disk,
        "app_mem_mb": app_mem,
        "mem_total_gb": mem_total_gb,
        "disk_total_gb": disk_total,
        "disk_free_gb": disk_free,
    }


def _cpu_percent() -> float:
    """系统 CPU 占用率(GetSystemTimes 两次采样差值, 平滑/兜底为 0)."""
    global _CPU_SAMPLE
    try:
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return 0.0
        idle_s = (idle.dwHighDateTime << 32 | idle.dwLowDateTime) / 1e7
        total_s = ((kernel.dwHighDateTime << 32 | kernel.dwLowDateTime)
                   + (user.dwHighDateTime << 32 | user.dwLowDateTime)) / 1e7
        now = time.time()
        if _CPU_SAMPLE:
            dt = now - _CPU_SAMPLE[0]
            d_idle = idle_s - _CPU_SAMPLE[1]
            d_total = total_s - _CPU_SAMPLE[2]
            if dt > 0 and d_total > 0:
                _CPU_SAMPLE = (now, idle_s, total_s)
                return round(max(0.0, min(100.0, 100.0 * (1 - d_idle / d_total))), 1)
        _CPU_SAMPLE = (now, idle_s, total_s)
        return 0.0
    except Exception:
        return 0.0

log = logging.getLogger("service")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(SERVICE_LOG, encoding="utf-8"),
    ],
)

CFG_LOCK = threading.Lock()
CURRENT_CFG: Config = load_config(CONFIG_FILE)

_DEV_LOCK = threading.Lock()
_devices: dict = {}      # serial -> u2.Device(懒连接缓存)
_RT_LOCK = threading.Lock()
_runtimes: dict = {}     # serial -> RuntimeStatus(每设备独立运行状态)
_shots: dict = {}        # serial -> {"ts": float, "png": bytes|None}(截图缓存)
_fail_until: dict = {}   # serial -> timestamp(截图失败熔断)


def get_config() -> Config:
    with CFG_LOCK:
        return CURRENT_CFG


def reload_config() -> Config:
    global CURRENT_CFG
    with CFG_LOCK:
        CURRENT_CFG = load_config(CONFIG_FILE)
        return CURRENT_CFG


def get_runtime(serial: str) -> RuntimeStatus:
    with _RT_LOCK:
        rt = _runtimes.get(serial)
        if rt is None:
            rt = _runtimes[serial] = RuntimeStatus()
        return rt


def get_managed_serials() -> list:
    """所有勾选了「接入控制台」的设备序列号。"""
    devs = load_devices()
    return [s for s, d in devs.items() if isinstance(d, dict) and d.get("enabled")]


def get_device(serial: str):
    """懒连接设备; adb 抖动时自动重连。"""
    with _DEV_LOCK:
        dev = _devices.get(serial)
        if dev is None:
            try:
                dev = _devices[serial] = connect_device(serial)
                log.info("设备已连接: %s", serial)
            except SystemExit as exc:
                log.warning("设备连接失败(%s): %s", serial, exc)
                dev = None
        return dev


def reset_device(serial: str) -> None:
    with _DEV_LOCK:
        _devices.pop(serial, None)


def device_online(serial: str) -> bool:
    """检测设备是否在线(带超时, 不阻塞请求线程)。"""
    if not serial:
        return False
    target = serial if ":" in serial else f"{serial}:5555"
    try:
        out = subprocess.run(
            [str(BASE_DIR / "tools" / "platform-tools" / "adb.exe"), "devices"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,  # 禁止创建控制台窗口(否则每 2 秒闪一次 cmd)
        )
    except Exception:
        return False
    for line in (out.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == target and parts[1] == "device":
            return True
    return False


def task_worker(serial: str) -> None:
    """后台为某台设备执行一轮任务。"""
    try:
        rt = get_runtime(serial)
        dev = get_device(serial)
        if dev is None:
            rt.update(running=False, step="设备未连接", last_result="设备连接失败")
            log.warning("任务启动失败(设备 %s): 未连接", serial)
            return
        cfg = load_config(CONFIG_FILE, serial=serial)
        bot = FanqieBot(cfg, dev, rt=rt)
        log.info("===== 启动一轮任务(设备 %s) =====", serial)
        bot.run()
        log.info("===== 任务结束(设备 %s) =====", serial)
    except SystemExit as exc:
        log.warning("任务退出(设备 %s): %s", serial, exc)
        get_runtime(serial).update(running=False, step="已停止", last_result=f"退出({exc})")
    except Exception as exc:
        log.error("任务异常(设备 %s): %s\n%s", serial, exc, traceback.format_exc())
        get_runtime(serial).update(running=False, step="异常", last_result=str(exc))


def start_task() -> str:
    """为所有已接入设备各启动一轮任务。"""
    serials = get_managed_serials()
    started = []
    for s in serials:
        rt = get_runtime(s)
        if not rt.get()["running"]:
            rt.update(running=True, stop_requested=False, step="启动中", last_result="")
            threading.Thread(target=task_worker, args=(s,), daemon=True, name=f"task-{s}").start()
            started.append(s)
    if not serials:
        return "没有已接入的设备, 请先在「设备连接」勾选设备"
    return f"已为 {len(started)} 台设备启动任务" if started else "任务已在运行"


def start_task_for(serial: str) -> str:
    """为指定设备单独启动一轮任务(作者更新时间到点触发)。"""
    rt = get_runtime(serial)
    if rt.get()["running"]:
        return "任务已在运行"
    rt.update(running=True, stop_requested=False, step="启动中", last_result="")
    threading.Thread(target=task_worker, args=(serial,), daemon=True, name=f"task-{serial}").start()
    return f"已为设备 {serial} 启动任务"


def stop_task() -> str:
    serials = get_managed_serials() or ([get_config().device_serial] if get_config().device_serial else [])
    any_running = False
    for s in serials:
        rt = get_runtime(s)
        if rt.get()["running"]:
            any_running = True
            rt.update(stop_requested=True, step="停止中")
    return "已请求停止(将在下个动作点生效)" if any_running else "任务未在运行"


def started_today_in_state(serial: str, book: str) -> bool:
    """该设备这本书今天是否已真正启动过(作者更新时间到点启动过则不重复启动)。"""
    try:
        full = json.loads((BASE_DIR / "state.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return False
    return (full.get(serial) or {}).get("started", {}).get(book) == datetime.now().date().isoformat()


def scheduler_loop() -> None:
    """每天 00:01 后执行一轮; 另每 30 秒检查「作者更新时间」到点的书, 到点(更新+1分钟)单独启动该设备。"""
    while True:
        try:
            now = datetime.now()
            today = now.date().isoformat()
            last = ""
            try:
                last = LAST_RUN_FILE.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            if now.hour * 60 + now.minute >= 1 and last != today:
                log.info("调度触发: 执行今日任务")
                try:
                    LAST_RUN_FILE.write_text(today, encoding="utf-8")
                except OSError:
                    pass
                start_task()
            # 作者更新时间到点: 单独启动对应设备(比作者晚一分钟)
            for s in get_managed_serials():
                rt = get_runtime(s)
                if rt.get()["running"]:
                    continue
                for b in load_books_for_serial(s):
                    if not b.enabled or not b.update_time:
                        continue
                    if update_due(b.update_time) and not started_today_in_state(s, b.name):
                        log.info("调度触发: 《%s》作者更新时间到点(%s), 启动设备 %s", b.name, b.update_time, s)
                        start_task_for(s)
                        break
        except Exception as exc:
            log.warning("调度异常: %s", exc)
        time.sleep(30)


def take_screenshot(serial: str) -> bytes | None:
    now = time.time()
    shot = _shots.get(serial) or {}
    if shot.get("png") is not None and now - shot.get("ts", 0) < 3:
        return shot["png"]
    if now < _fail_until.get(serial, 0):
        return None  # 该设备刚失败过, 熔断 10 秒, 避免请求堆积
    dev = get_device(serial)
    if dev is None:
        return None
    result: dict = {}

    def _shoot() -> None:
        try:
            img = dev.screenshot()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            result["png"] = buf.getvalue()
        except Exception as exc:
            result["err"] = exc

    th = threading.Thread(target=_shoot, daemon=True)
    th.start()
    th.join(timeout=8)  # 截图最多等待 8 秒, 超时视为失败
    if "png" in result:
        _shots[serial] = {"ts": now, "png": result["png"]}
        return _shots[serial]["png"]
    _fail_until[serial] = time.time() + 10
    log.debug("截图失败/超时(%s): %s", serial, result.get("err"))
    reset_device(serial)
    return None


def list_devices() -> list:
    """列出 adb 可见设备: [{serial, model, state}]。"""
    adb = str(BASE_DIR / "tools" / "platform-tools" / "adb.exe")
    try:
        out = subprocess.run([adb, "devices", "-l"], capture_output=True, text=True, timeout=10,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        text = out.stdout or ""
    except Exception:
        text = ""
    devices = []
    for line in text.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = "未知"
        for p in parts[2:]:
            if p.startswith("model:"):
                model = p.split(":", 1)[1]
        devices.append({"serial": serial, "model": model, "state": state})
    return devices


def scan_adb_devices() -> list:
    """主动探测 adb: 重新连接 config.yaml device.scan_ips 里的网络设备, 再列出全部设备。"""
    adb = str(BASE_DIR / "tools" / "platform-tools" / "adb.exe")
    for target in getattr(CURRENT_CFG, "scan_ips", []) or []:
        try:
            subprocess.run([adb, "connect", target], capture_output=True, text=True, timeout=4,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass
    return list_devices()


def set_device_enabled(serial: str, enabled: bool) -> str:
    """把设备加入/移出控制台(devices.json)。"""
    devs = load_devices()
    cfg = devs.get(serial)
    if not isinstance(cfg, dict):
        cfg = devs[serial] = {"enabled": True, "books": []}
    cfg["enabled"] = bool(enabled)
    save_devices(devs)
    reload_config()
    log.info("设备 %s 已%s接入控制台", serial, "加入" if enabled else "移出")
    return f"设备 {serial} " + ("已接入控制台" if enabled else "已移出控制台")


def save_device_books(serial: str, books: list) -> str:
    """保存某设备的书籍清单(devices.json)。"""
    cleaned = []
    for b in books:
        if not isinstance(b, dict):
            continue
        name = str(b.get("name", "")).strip()
        if not name:
            continue
        cleaned.append({
            "name": name,
            "enabled": bool(b.get("enabled", True)),
            "gift": bool(b.get("gift", False)),
            "gift_count": max(1, int(b.get("gift_count", 3))),
            "urge": bool(b.get("urge", True)),
            "review": bool(b.get("review", True)),
            "add_shelf": bool(b.get("add_shelf", True)),
            "completed": bool(b.get("completed", False)),
            "update_time": str(b.get("update_time", "")).strip(),
        })
    devs = load_devices()
    cfg = devs.get(serial)
    if not isinstance(cfg, dict):
        cfg = devs[serial] = {"enabled": True, "books": []}
    cfg["books"] = cleaned
    save_devices(devs)
    reload_config()
    log.info("设备 %s 书籍已保存: %d 本", serial, len(cleaned))
    return f"已保存 {len(cleaned)} 本书(设备 {serial})"


def set_device_name(serial: str, name: str) -> str:
    """给设备设置备注名(devices.json 的 serial 下 name 字段, 透传保留其他字段)。"""
    if not serial:
        return "设备不能为空"
    name = str(name).strip()[:20]
    devs = load_devices()
    cfg = devs.get(serial)
    if not isinstance(cfg, dict):
        cfg = devs[serial] = {"enabled": True, "books": []}
    cfg["name"] = name
    save_devices(devs)
    reload_config()
    log.info("设备 %s 备注名已设置: %r", serial, name)
    return f"设备 {serial} 备注名已保存" + (f": {name}" if name else " (已清空)")


def get_device_names() -> dict:
    """返回已接入设备的备注名映射 {serial: name}。"""
    devs = load_devices()
    return {s: str(cfg.get("name", "")).strip() for s, cfg in devs.items()
            if isinstance(cfg, dict) and cfg.get("enabled")}


def _books_view(serial: str) -> list:
    return [
        {"name": b.name, "enabled": b.enabled, "gift": b.gift, "gift_count": b.gift_count,
         "urge": b.urge, "review": b.review, "add_shelf": b.add_shelf,
         "completed": b.completed, "update_time": b.update_time}
        for b in load_books_for_serial(serial)
    ]


class Handler(BaseHTTPRequestHandler):
    server_version = "FanqieDash/2.0"

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self) -> None:
        try:
            body = HTML_FILE.read_bytes()
        except OSError:
            body = b"<h1>dashboard.html not found</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # 防止浏览器缓存旧页面
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        try:
            self._handle_get()
        except Exception as exc:
            log.error("GET %s 异常: %s\n%s", self.path, exc, traceback.format_exc())
            try:
                self._json({"error": f"服务器内部错误: {exc}"}, 500)
            except Exception:
                pass

    def _handle_get(self) -> None:
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)
        if path == "/":
            self._html()
            return
        if path == "/api/status":
            serials = get_managed_serials()
            if not serials and get_config().device_serial:
                serials = [get_config().device_serial]
            # 读持久化 state, 用于未运行时展示已发的书评(发书讨论)状态
            try:
                _state_full = json.loads((BASE_DIR / "state.json").read_text(encoding="utf-8"))
            except Exception:
                _state_full = {}
            devs = []
            for s in serials:
                rt = get_runtime(s)
                st = rt.get()
                st["serial"] = s
                st["device_online"] = device_online(s)
                if not st["books"]:  # 任务尚未运行时, 用该设备配置的书单初始化展示
                    st["books"] = [
                        {"name": b.name, "enabled": b.enabled, "gift": b.gift, "gift_want": b.gift_count,
                         "urge": b.urge, "review": b.review, "completed": b.completed, "update_time": b.update_time,
                         "urged": False, "gifts": 0, "status": "待处理", "detail": "",
                         "reviewed": b.name in (_state_full.get(s, {}).get("reviewed") or {}),
                         "review_status": "已发" if b.name in (_state_full.get(s, {}).get("reviewed") or {}) else "待发"}
                        for b in load_books_for_serial(s)
                    ]
                devs.append(st)
            self._json({"devices": devs, "managed": serials,
                        "server_time": datetime.now().isoformat(timespec="seconds")})
            return
        if path == "/api/devices":
            self._json({"devices": list_devices(), "managed": get_managed_serials()})
            return
        if path == "/api/device-names":
            self._json({"names": get_device_names()})
            return
        if path == "/api/books":
            serial = (qs.get("serial") or [""])[0]
            if not serial:
                serial = get_config().device_serial
            reload_config()  # 每次从磁盘读, 保证与文件一致
            self._json({"serial": serial, "books": _books_view(serial)})
            return
        if path == "/api/screenshot":
            serial = (qs.get("serial") or [""])[0] or get_config().device_serial
            png = take_screenshot(serial)
            if png is None:
                self._json({"error": "no screenshot"}, 503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(png)
            return
        if path == "/api/log":
            try:
                text = (BASE_DIR / "fanqie.log").read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = "(暂无日志)"
            self._json({"log": text[-20000:]})
            return
        if path == "/api/config":
            try:
                text = CONFIG_FILE.read_text(encoding="utf-8")
            except OSError:
                text = ""
            self._json({"config": text})
            return
        if path == "/api/health":
            self._json(_health_metrics())
            return
        if path == "/api/star":
            try:
                parsed = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
                star = int((parsed.get("review") or {}).get("star", 4))
            except Exception:
                star = 4
            self._json({"star": min(5, max(1, star))})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        try:
            self._handle_post()
        except Exception as exc:
            log.error("POST %s 异常: %s\n%s", self.path, exc, traceback.format_exc())
            try:
                self._json({"error": f"服务器内部错误: {exc}"}, 500)
            except Exception:
                pass

    def _handle_post(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        if path == "/api/start":
            self._json({"result": start_task()})
            return
        if path == "/api/stop":
            self._json({"result": stop_task()})
            return
        if path == "/api/scan-devices":
            self._json({"devices": scan_adb_devices(), "managed": get_managed_serials()})
            return
        if path == "/api/manage":
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
                serial = str(data.get("serial", "")).strip()
                enabled = bool(data.get("enabled", True))
                if not serial:
                    self._json({"error": "设备不能为空"}, 400)
                    return
                self._json({"result": set_device_enabled(serial, enabled)})
            except Exception as exc:
                self._json({"error": f"接入失败: {exc}"}, 400)
            return
        if path == "/api/device-name":
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
                serial = str(data.get("serial", "")).strip()
                name = str(data.get("name", "")).strip()
                if not serial:
                    self._json({"error": "设备不能为空"}, 400)
                    return
                self._json({"result": set_device_name(serial, name)})
            except Exception as exc:
                self._json({"error": f"保存备注失败: {exc}"}, 400)
            return
        if path == "/api/books":
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
                serial = str(data.get("serial", "")).strip() or get_config().device_serial
                books = data.get("books", [])
                if not isinstance(books, list):
                    raise ValueError("books 必须是列表")
                self._json({"result": save_device_books(serial, books)})
            except Exception as exc:
                self._json({"error": f"保存失败: {exc}"}, 400)
            return
        if path == "/api/config":
            try:
                text = raw.decode("utf-8")
                # 校验 YAML 可解析后再落盘
                import yaml
                parsed = yaml.safe_load(text)
                if not isinstance(parsed, dict):
                    raise ValueError("配置必须是 YAML 映射")
                CONFIG_FILE.write_text(text, encoding="utf-8")
                reload_config()
                self._json({"result": "已保存"})
            except Exception as exc:
                self._json({"error": f"保存失败: {exc}"}, 400)
            return
        if path == "/api/star":
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
                star = int(data.get("star", 4))
                if not 1 <= star <= 5:
                    raise ValueError("星级必须是 1-5")
                text = CONFIG_FILE.read_text(encoding="utf-8")
                # 文本级更新 review 节下的 star 行(不破坏注释/格式)
                import re as _re
                if _re.search(r"^review:\s*$", text, _re.MULTILINE):
                    if _re.search(r"^\s+star:\s*\d+", text, _re.MULTILINE):
                        text = _re.sub(r"^(\s+star:\s*)\d+", rf"\g<1>{star}", text,
                                       count=1, flags=_re.MULTILINE)
                    else:
                        text = _re.sub(r"(^review:\s*$)", rf"\1\n  star: {star}", text,
                                       count=1, flags=_re.MULTILINE)
                else:
                    text = text.rstrip() + f"\n\nreview:\n  star: {star}\n"
                CONFIG_FILE.write_text(text, encoding="utf-8")
                reload_config()
                self._json({"result": f"已保存书评星级 {star} 星"})
            except Exception as exc:
                self._json({"error": f"保存失败: {exc}"}, 400)
            return
        self._json({"error": "not found"}, 404)


def main() -> int:
    log.info("番茄每日任务控制台启动(多设备): 本机 http://127.0.0.1:%d | 局域网 http://<本机IP>:%d", PORT, PORT)
    threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
