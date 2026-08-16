# -*- coding: utf-8 -*-
"""番茄免费小说 多书每日任务.

每本书流程(按配置列表循环):
  1. 从「书城」搜索框进入搜索页, 搜索书名
  2. 找到与书名最匹配的结果; 匹配不上则跳过该书
  3. 进书 -> 阅读器内右->左滑动一页一页自然翻页(绝不点「下一章」跳章, 滑动不点进广告), 读到最新章(书末页)
  4. 出现「催更」图标 -> 点一次(同一天同一本书只点一次, state.json 记录)
  5. 该书开启送礼物时 -> 送 N 次免费礼物(用爱发电)
  6. 换下一本书; 全部完成后退出

可单独命令行运行(执行一轮后退出), 也可被 app.py(网页控制台)以线程方式调度。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import logging
import random
import re
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

try:
    import uiautomator2 as u2
    import adbutils
except ImportError:
    sys.exit("缺少依赖: pip install -r requirements.txt")
try:
    import yaml
except ImportError:
    sys.exit("缺少依赖: pip install -r requirements.txt")

APP_PACKAGE = "com.dragon.read"  # 番茄免费小说
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
BOOKS_FILE = BASE_DIR / "books.json"  # 旧版书籍清单(首次启动自动迁移到 devices.json)
DEVICES_FILE = BASE_DIR / "devices.json"  # 网页端按设备管理的书籍清单 {serial: {enabled, books:[]}}
_STATE_LOCK = threading.Lock()  # state.json 读写锁(多设备并发)

# 章末/书籍主页进入阅读器的入口按钮
READER_ENTRY_TEXTS = ("继续阅读", "开始阅读", "下一章", "进入下一章")
# 弹窗/广告上的关闭性文字(按顺序尝试)
CLOSE_TEXTS = ("跳过", "关闭", "取消", "我知道了", "下次再说", "暂不开启", "暂不需要", "暂不加入", "下次再看", "稍后再说")
# 带倒计时的关闭按钮兜底正则(如「跳过 5」「关闭广告」)
CLOSE_PATTERNS = (r".*跳过.*", r".*关闭.*", r".*暂不.*", r".*稍后.*")
URGE_TEXT = "催更"
GIFT_ENTRY_TEXT = "送礼物"
FREE_GIFT_TEXT = "用爱发电"
GIFT_SEND_BTN_ID = "com.dragon.read:id/fs_"  # 礼物面板右下角赠送按钮
GIFT_PANEL_MARKER = "送礼记录"
SEARCH_INPUT_ID = "com.dragon.read:id/hfy"  # 搜索页输入框
SEARCH_RESULT_TITLE_ID = "com.dragon.read:id/ale"  # 搜索结果书名
SEARCH_ACTIVITY = "SearchActivity"
MATCH_RATIO_MIN = 0.9  # 搜索书名匹配阈值, 低于则跳过
MAX_FLIP_PER_BOOK = 200  # 单本书每轮最多翻页数, 防止卡死

# 看完后自动发的书评文案池: 番茄读者最真实自然的感观语言(随机选一条)
REVIEW_TEXTS = [
    "这本书太好看了，一口气追到最新章，根本停不下来！作者大大加油更新呀～",
    "剧情真的很吸引人，人物也很有特点，每天等更新等到心痒痒，好看！",
    "没想到这么好看，熬夜看完的，强烈推荐！就是更新太慢了，催更催更！",
    "文笔流畅，情节紧凑，伏笔埋得也好，越看越上头，支持！",
    "这本书我已经追了好久了，真心不错，希望作者继续保持这个节奏，冲鸭！",
    "太好看了吧！看得我眼泪都出来了，作者文笔太细腻了，必须给个好评！",
    "设定挺新颖的，剧情不拖沓，一口气看到最新章，坐等更新～",
    "本来随便看看，结果一看就停不下来了，质量很高，五星好评！",
    "很喜欢这本书的风格，轻松又有趣，看得很开心，期待后面的剧情！",
    "从第一章就开始追，越看越喜欢，作者加油，我们一直都在！",
    "情节跌宕起伏，每一章都有惊喜，好久没看到这么好看的书了，推荐！",
    "这本书值得反复看，细节很多，每次看都有新发现，太棒了！",
    "追更的日子虽然难熬，但值得！这本书真的很对我的胃口，好看好看！",
    "作者的脑洞太大了，剧情完全猜不到，看得太过瘾了，支持支持！",
    "无意中点进来，结果被圈粉了，故事很有温度，已经推荐给朋友了！",
]


@dataclass
class BookTask:
    name: str
    enabled: bool = True    # 启用开关(网页端管理)
    gift: bool = False      # 是否给这本书送免费礼物
    gift_count: int = 3
    urge: bool = True       # 是否点催更(自己的书不能给自己催更, 网页端按设备勾选)
    review: bool = True     # 看完后自动发一条书评(读者自然语言)
    completed: bool = False  # 已读完最后一章并跑完流程, 后续轮次直接跳过
    update_time: str = ""   # 作者更新时间(如 "18:00"); 设置后每天到「该时间+1分钟」才跑这本书(读完也重跑, 读新章节)


@dataclass
class Config:
    device_serial: str = ""
    scan_ips: list = field(default_factory=list)  # 网页「扫描设备」要重连的 adb 地址列表(如 ["192.168.1.10:5555"])
    books: list = field(default_factory=list)
    page_mode: str = "swipe"  # swipe | scroll | tap
    interval_range: list = field(default_factory=lambda: [8, 15])
    auto_close_ads: bool = True
    extra_close_texts: list = field(default_factory=list)
    task_timeout_minutes: int = 60
    log_level: str = "INFO"
    log_file: str = "fanqie.log"
    review_star: int = 4  # 「点评此书」评分弹窗默认星级 1-5
    ai_enabled: bool = True   # 用 AI(OpenAI 兼容接口)生成书评/发帖文案; 未配置 key 或失败时自动用预设池
    ai_base_url: str = ""     # OpenAI 兼容接口地址(中转/代理), 如 https://api.openai.com/v1 ; 留空=不启用
    ai_api_key: str = ""      # API Key
    ai_model: str = "gpt-4o-mini"
    ai_timeout: int = 20      # AI 请求超时(秒)


def load_books_from_json(path: Path = BOOKS_FILE) -> list:
    """读取网页端管理的书籍清单(books.json); 不存在或为空时返回 []。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    books: list = []
    for b in raw or []:
        if not isinstance(b, dict) or not str(b.get("name", "")).strip():
            continue
        books.append(
            BookTask(
                name=str(b["name"]).strip(),
                enabled=bool(b.get("enabled", True)),
                gift=bool(b.get("gift", False)),
                gift_count=int(b.get("gift_count", 3)),
                urge=bool(b.get("urge", True)),
                review=bool(b.get("review", True)),
                update_time=str(b.get("update_time", "")).strip(),
            )
        )
    return books


# ---------- 按设备管理的书籍清单(devices.json) ----------
def _migrate_books_to_devices(serial: str) -> None:
    """旧版 books.json(单设备清单) -> devices.json(按设备分组); 幂等。"""
    if not serial or DEVICES_FILE.exists():
        return
    try:
        raw = json.loads(BOOKS_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, list) or not raw:
        return
    books = []
    for b in raw:
        if not isinstance(b, dict) or not str(b.get("name", "")).strip():
            continue
        books.append({
            "name": str(b["name"]).strip(),
            "enabled": bool(b.get("enabled", True)),
            "gift": bool(b.get("gift", False)),
            "gift_count": int(b.get("gift_count", 3)),
            "urge": bool(b.get("urge", True)),
            "review": bool(b.get("review", True)),
            "completed": False,
        })
    devs = {serial: {"enabled": True, "books": books}}
    try:
        DEVICES_FILE.write_text(json.dumps(devs, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return
    print(f"[迁移] 已将 books.json 迁移为 devices.json(设备 {serial})")


def load_devices() -> dict:
    """读取 devices.json: {serial: {"enabled": bool, "books": [...]}}。"""
    try:
        devs = json.loads(DEVICES_FILE.read_text(encoding="utf-8-sig"))
        return devs if isinstance(devs, dict) else {}
    except (OSError, ValueError):
        return {}


def save_devices(devs: dict) -> None:
    DEVICES_FILE.write_text(json.dumps(devs, ensure_ascii=False, indent=2), encoding="utf-8")


def get_device_config(serial: str) -> dict:
    """读取(必要时初始化)某设备的配置节 {enabled, books}。"""
    devs = load_devices()
    cfg = devs.get(serial)
    if not isinstance(cfg, dict):
        cfg = devs[serial] = {"enabled": True, "books": []}
        save_devices(devs)
    return cfg


def load_books_for_serial(serial: str) -> list:
    """按设备读取书籍清单(devices.json); 缺失时先尝试从旧 books.json 迁移。"""
    _migrate_books_to_devices(serial)
    devs = load_devices()
    if not devs:
        return load_books_from_json()
    cfg = devs.get(serial) or {}
    books: list = []
    for b in cfg.get("books") or []:
        if not isinstance(b, dict) or not str(b.get("name", "")).strip():
            continue
        books.append(
            BookTask(
                name=str(b["name"]).strip(),
                enabled=bool(b.get("enabled", True)),
                gift=bool(b.get("gift", False)),
                gift_count=int(b.get("gift_count", 3)),
                urge=bool(b.get("urge", True)),
                review=bool(b.get("review", True)),
                completed=bool(b.get("completed", False)),
                update_time=str(b.get("update_time", "")).strip(),
            )
        )
    return books


def set_book_completed(serial: str, name: str, completed: bool = True) -> None:
    """持久化某设备上一本书的完成标记(devices.json)。"""
    devs = load_devices()
    cfg = devs.get(serial)
    if not isinstance(cfg, dict):
        return
    for b in cfg.get("books") or []:
        if isinstance(b, dict) and str(b.get("name", "")) == name:
            b["completed"] = bool(completed)
            save_devices(devs)
            return


def update_due(update_time: str) -> bool:
    """当前时间是否已到「作者更新时间+1分钟」(比作者晚一分钟跑)。

    格式 "HH:MM"; 空/无效值视为不限时(总是到点)。跨天按当天分钟数比较:
    例如设置 "00:30" 时, 当天 00:31 后到点, 深夜 23:59 视为今天已过到点时间。
    """
    if not update_time or not str(update_time).strip():
        return True
    try:
        h, m = str(update_time).strip().split(":")
        # +1 分钟 = 比作者晚一分钟; %1440 处理 "23:59" 这类跨午夜边界(次日 00:00 到点)
        target = (int(h) * 60 + int(m) + 1) % 1440
    except (ValueError, AttributeError):
        return True
    now = datetime.now()
    return now.hour * 60 + now.minute >= target


def load_config(path: Path, serial: str | None = None) -> Config:
    """读取配置; serial 指定时强制使用该设备(及其按设备的书籍清单)。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    device = raw.get("device", {}) or {}
    reader = raw.get("reader", {}) or {}
    task = raw.get("task", {}) or {}
    log = raw.get("log", {}) or {}
    device_serial = str(device.get("serial", "") or "")
    if serial:
        device_serial = serial
    books = load_books_for_serial(device_serial)
    if not books:  # 兜底: 从 config.yaml 读 books: / book.name
        for b in raw.get("books", []) or []:
            if not b.get("name"):
                continue
            books.append(
                BookTask(
                    name=str(b["name"]).strip(),
                    enabled=bool(b.get("enabled", True)),
                    gift=bool(b.get("gift", False)),
                    gift_count=int(b.get("gift_count", 3)),
                    urge=bool(b.get("urge", True)),
                    review=bool(b.get("review", True)),
                )
            )
        if not books:  # 兼容旧配置: 单本书
            legacy = raw.get("book", {}) or {}
            if legacy.get("name"):
                books.append(BookTask(name=str(legacy["name"]).strip(), gift=True, gift_count=3))
    timeout = task.get("timeout_minutes", 60)
    review = raw.get("review", {}) or {}
    try:
        review_star = int(review.get("star", 4))
    except (TypeError, ValueError):
        review_star = 4
    review_star = min(5, max(1, review_star))
    ai = raw.get("ai", {}) or {}
    try:
        ai_timeout = max(5, int(ai.get("timeout", 20)))
    except (TypeError, ValueError):
        ai_timeout = 20
    return Config(
        device_serial=device_serial,
        scan_ips=[str(x).strip() for x in (device.get("scan_ips") or []) if str(x).strip()],
        books=books,
        page_mode=str(reader.get("page_mode", "swipe")),
        interval_range=[int(x) for x in reader.get("interval_range", [8, 15])],
        auto_close_ads=bool(reader.get("auto_close_ads", True)),
        extra_close_texts=[str(t) for t in reader.get("extra_close_texts", [])],
        task_timeout_minutes=int(timeout),
        log_level=str(log.get("level", "INFO")),
        log_file=str(log.get("file", "fanqie.log")),
        review_star=review_star,
        ai_enabled=bool(ai.get("enabled", True)),
        ai_base_url=str(ai.get("base_url", "") or "").strip().rstrip("/"),
        ai_api_key=str(ai.get("api_key", "") or "").strip(),
        ai_model=str(ai.get("model", "gpt-4o-mini") or "gpt-4o-mini"),
        ai_timeout=ai_timeout,
    )


class RuntimeStatus:
    """线程安全的运行状态, 供网页控制台读取。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = {
            "running": False,
            "stop_requested": False,
            "current_book": "",
            "step": "空闲",
            "pages": 0,
            "started_at": None,
            "finished_at": None,
            "last_result": "",
            "books": [],  # [{"name","gift","urged","gifts","gift_want","status","detail"}]
        }

    def update(self, **kw) -> None:
        with self._lock:
            self._data.update(kw)

    def set_book(self, idx: int, **kw) -> None:
        with self._lock:
            while len(self._data["books"]) <= idx:
                self._data["books"].append({})
            self._data["books"][idx].update(kw)

    def reset_books(self, books: list) -> None:
        with self._lock:
            self._data["books"] = [
                {"name": b.name, "enabled": b.enabled, "gift": b.gift, "gift_want": b.gift_count,
                 "urge": b.urge, "review": b.review, "completed": b.completed, "urged": False,
                 "gifts": 0, "status": "待处理", "detail": "",
                 "reviewed": False, "review_status": "待发"}
                for b in books
            ]

    def get(self) -> dict:
        with self._lock:
            import copy
            return copy.deepcopy(self._data)


RUNTIME = RuntimeStatus()


class _DeviceFilter(logging.Filter):
    """给日志记录注入设备序列号, 多设备并发时日志可区分。"""

    def __init__(self, device: str) -> None:
        super().__init__()
        self._device = device

    def filter(self, record: logging.LogRecord) -> bool:
        record.device = self._device
        return True


class _DeviceFormatter(logging.Formatter):
    """格式化时给没有设备标识的记录补默认值, 避免 KeyError。"""

    def format(self, record: logging.LogRecord) -> str:
        record.device = getattr(record, "device", "-")
        return super().format(record)


class FanqieBot:
    """多书每日任务机器人。"""

    def __init__(self, cfg: Config, d: u2.Device, rt: RuntimeStatus | None = None) -> None:
        self.cfg = cfg
        self.d = d
        self.w, self.h = d.window_size()
        self.pages = 0
        self.rt = rt if rt is not None else RUNTIME  # 多设备时每台设备独立运行状态
        self._bookshelf_done = False   # 每本书只点一次「加入书架」
        self._reached_end = False      # 本轮是否读到书末页(催更卡片出现)
        self._state_full: dict = {}
        self.state: dict = self._load_state()
        self._setup_logging()

    # ---------- 状态 ----------
    def _load_state(self) -> dict:
        """读取按设备分组的 state.json; 旧格式(顶层 urged/gifted)自动迁移到当前设备。"""
        with _STATE_LOCK:
            try:
                full = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                full = {}
            if not isinstance(full, dict):
                full = {}
            migrated = False
            for key in ("urged", "gifted", "search_miss", "reviewed"):
                if key in full:
                    full.setdefault(self.cfg.device_serial, {}).setdefault(key, {}).update(full.pop(key))
                    migrated = True
            self._state_full = full
            if migrated:
                try:
                    STATE_FILE.write_text(
                        json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except OSError:
                    pass
            return full.get(self.cfg.device_serial, {})

    def _save_state(self) -> None:
        with _STATE_LOCK:
            self._state_full[self.cfg.device_serial] = self.state
            try:
                STATE_FILE.write_text(
                    json.dumps(self._state_full, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError:
                pass

    def _setup_logging(self) -> None:
        parent = logging.getLogger("fanqie")
        if not parent.handlers:
            parent.setLevel(getattr(logging, self.cfg.log_level.upper(), logging.INFO))
            fmt = _DeviceFormatter("%(asctime)s [%(levelname)s] [设备%(device)s] %(message)s")
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            parent.addHandler(sh)
            if self.cfg.log_file:
                fh = logging.FileHandler(BASE_DIR / self.cfg.log_file, encoding="utf-8")
                fh.setFormatter(fmt)
                parent.addHandler(fh)
            parent.propagate = False
        # 每台设备用独立子 logger, 通过 filter 注入设备标识(多设备日志可区分)
        self.log = logging.getLogger(f"fanqie.{self.cfg.device_serial}")
        self.log.setLevel(getattr(logging, self.cfg.log_level.upper(), logging.INFO))
        for flt in self.log.filters:
            if getattr(flt, "_device", None) == self.cfg.device_serial:
                break
        else:
            self.log.addFilter(_DeviceFilter(self.cfg.device_serial))

    # ---------- 基础动作 ----------
    def sleep_human(self, lo: float, hi: float) -> None:
        time.sleep(random.uniform(lo, hi))

    def click_text(self, text: str, timeout: float = 1.0, fuzzy: bool = False) -> bool:
        """按文字(或内容描述)查找并点击, 支持模糊匹配。"""
        d = self.d
        selectors = [d(text=text), d(description=text)]
        if fuzzy:
            selectors += [d(textContains=text), d(descriptionContains=text)]
        for sel in selectors:
            try:
                if sel.click(timeout):
                    self.log.info("点击「%s」", text)
                    return True
            except Exception as exc:
                self.log.debug("click_text(%r) 失败: %s", text, exc)
        return False

    def _click_urge(self) -> bool:
        """等催更卡片入场动画稳定后点击; 服务端可能拒绝(toast「催更失败」), 点击本身成功即可。

        Compose 绘制的按钮 text 精确匹配可能失败, 依次尝试 精确/模糊/描述 匹配。
        """
        sels = [
            self.d(text=URGE_TEXT),
            self.d(textContains=URGE_TEXT),
            self.d(descriptionContains=URGE_TEXT),
        ]
        for sel in sels:
            if not sel.exists(1.2):
                continue
            last = None
            for _ in range(3):  # 书末页卡片有滑入动画, 等 bounds 稳定再点
                try:
                    cur = sel.info.get("bounds")
                except Exception:
                    cur = None
                if cur and cur == last:
                    break
                last = cur
                self.sleep_human(0.4, 0.6)
            try:
                sel.click(timeout=1.0)
                self.log.info("点击「%s」", URGE_TEXT)
                return True
            except Exception as exc:
                self.log.debug("点击催更失败: %s", exc)
        self.log.warning("催更按钮存在但无法点击(Compose文本匹配失败?)")
        return False

    def _click_urge_coord(self) -> bool:
        """书末卡片页(全书最后一页)的橙色「催更」按钮是 Compose 绘制,
        dump/uiautomator 检测不到文本(实测 480/480 页只有「本章讨论」「1次」两个文本),
        必须用坐标点击: 橙色催更按钮中心实测约 (366,468) = (0.508,0.366),
        礼物图标约 (658,373) = (0.914,0.291)。
        调用前提: 已通过页码判据(全书最后一页 N==M + 「本章讨论」)确认在书末卡片页。
        """
        try:
            self.d.click(0.508, 0.366)
        except Exception as exc:
            self.log.warning("坐标点击催更失败: %s", exc)
            return False
        self.sleep_human(1.5, 2.5)
        # 点击后可能弹「今日已催更」提示或按钮变为「已催更」; Toast 短暂, 抓不到也视为成功
        try:
            xml = self.d.dump_hierarchy()
            if any(k in xml for k in ("已催更", "今日已催更", "催更成功", "今天已经催更")):
                self.log.info("催更成功(坐标点击 + 已催更提示确认)")
                return True
        except Exception:
            pass
        self.log.info("催更按钮坐标点击完成(无显式提示, 视为已催更)")
        return True

    def turn_page(self) -> None:
        """按配置的翻页方式翻一页, 坐标与时长随机化模拟真人。"""
        d = self.d
        mode = self.cfg.page_mode
        if mode == "tap":
            # 右侧区域点按: 一页一页翻(不点下一章按钮)
            d.click(random.uniform(0.85, 0.95), random.uniform(0.35, 0.65))
            return
        if mode == "scroll":
            y0 = random.uniform(0.72, 0.85)
            y1 = random.uniform(0.18, 0.30)
            d.swipe(0.5, y0, 0.5, y1, duration=random.uniform(0.3, 0.6))
            return
        # swipe(默认): 右->左水平滑动自然翻页; 滑动是拖动不是点按, 不会点进广告/按钮
        x0 = random.uniform(0.85, 0.93)
        x1 = random.uniform(0.12, 0.22)
        y = random.uniform(0.38, 0.52)   # 中上区域, 避开底部广告卡/推广按钮
        d.swipe(x0, y, x1, y, duration=random.uniform(0.2, 0.4))

    def turn_back(self) -> None:
        """多翻了一页后左滑回上一页(与翻页方向相反: 从左往右滑)。

        注意: 识别到书末黄色「催更」按钮后绝不能再往右往左滑(会离开阅读器退回搜索页);
        如果不小心多划了一下, 就用本方法左滑一页回到催更卡片。
        """
        d = self.d
        x0 = random.uniform(0.12, 0.22)
        x1 = random.uniform(0.85, 0.93)
        y = random.uniform(0.38, 0.52)
        try:
            d.swipe(x0, y, x1, y, duration=random.uniform(0.2, 0.4))
            self.log.info("已左滑回到上一页(多翻一页纠正)")
        except Exception as exc:
            self.log.debug("turn_back 失败: %s", exc)

    def _urge_button_exists(self, timeout: float = 1.2) -> bool:
        """检测书末黄色「催更」按钮。

        新版本客户端书末卡片可能是 Compose 绘制的, uiautomator 的 text 匹配
        有时抓不到, 所以依次尝试 text / textContains / descriptionContains /
        dump_hierarchy 全文包含 四种方式。
        """
        try:
            if self.d(text=URGE_TEXT).exists(timeout):
                return True
            if self.d(textContains=URGE_TEXT).exists(timeout):
                return True
            if self.d(descriptionContains=URGE_TEXT).exists(timeout):
                return True
            xml = self.d.dump_hierarchy()
            if URGE_TEXT in xml:
                return True
        except Exception as exc:
            self.log.debug("_urge_button_exists 失败: %s", exc)
        return False

    def _page_hash(self) -> str:
        """当前屏幕内容指纹, 用于判断翻页是否真的前进了。"""
        try:
            img = self.d.screenshot().convert("L").resize((48, 85))
            return hashlib.md5(img.tobytes()).hexdigest()
        except Exception:
            return ""

    # ---------- 弹窗 / 章末处理 ----------
    def handle_interruptions(self, quick: bool = False) -> bool:
        if not self.cfg.auto_close_ads:
            return False
        timeout = 0.4 if quick else 0.8
        # 0) 日夜模式引导: 新书第一次进入阅读器时会弹出「切换日夜模式」弹窗
        #    (只有 日间/夜间 选项, 没有跳过/关闭按钮, 不处理会一直挡在阅读器上)
        #    确认弹窗存在后点「日间模式」关闭(夜间模式亮度暗, 影响翻页识别)
        try:
            if (self.d(textContains="切换日夜模式").exists(timeout)
                    or self.d(textContains="日夜模式").exists(timeout)):
                self.log.info("检测到日夜模式引导弹窗, 点击日间模式关闭")
                for t in ("日间模式", "日间阅读", "日间", "夜间模式", "夜间"):
                    if self.click_text(t, 1.0):
                        self.sleep_human(0.8, 1.5)
                        return True
                self.log.warning("日夜模式弹窗存在但未找到日间/夜间按钮")
        except Exception:
            pass
        # 1) 加入书架: 每本书只点一次(书籍详情页按钮/阅读器菜单/「加入书架?」弹窗)
        if not self._bookshelf_done and self.click_text("加入书架", timeout):
            self._bookshelf_done = True
            self.sleep_human(0.8, 1.5)
            return True
        if quick:
            # 快速模式(阅读翻页中): 只查最常见的弹窗关闭, 减少网络调用提高翻页速度
            for pattern in CLOSE_PATTERNS:
                try:
                    if self.d(textMatches=pattern).click(timeout):
                        self.log.info("点击关闭按钮(正则 %s)", pattern)
                        self.sleep_human(0.8, 1.5)
                        return True
                except Exception as exc:
                    self.log.debug("textMatches(%s) 失败: %s", pattern, exc)
            return False
        # 2) 弹窗关闭(注意: 不检查/不点击「下一章」「进入下一章」等阅读入口,
        #    阅读必须一页一页自然翻页, 绝不直接跳章)
        for pattern in CLOSE_PATTERNS:
            try:
                if self.d(textMatches=pattern).click(timeout):
                    self.log.info("点击关闭按钮(正则 %s)", pattern)
                    self.sleep_human(0.8, 1.5)
                    return True
            except Exception as exc:
                self.log.debug("textMatches(%s) 失败: %s", pattern, exc)
        for text in list(CLOSE_TEXTS) + [t for t in self.cfg.extra_close_texts if t]:
            if self.click_text(text, timeout):
                self.sleep_human(0.8, 1.5)
                return True
        return False

    # ---------- 催更 / 礼物状态 ----------
    def _can_urge_today(self, book: str) -> bool:
        return self.state.get("urged", {}).get(book) != date.today().isoformat()

    def _mark_urged(self, book: str) -> None:
        self.state.setdefault("urged", {})[book] = date.today().isoformat()
        self._save_state()
        self.log.info("《%s》今日已催更", book)

    def _gifted_today(self, book: str) -> int:
        rec = self.state.get("gifted", {}).get(book) or {}
        if rec.get("date") != date.today().isoformat():
            return 0
        return int(rec.get("count", 0))

    def _mark_gifted(self, book: str, count: int) -> None:
        rec = self.state.setdefault("gifted", {}).setdefault(book, {})
        today = date.today().isoformat()
        if rec.get("date") != today:
            # 新的一天: 重置计数(不继承前一天的数量)
            rec["date"] = today
            rec["count"] = count
        else:
            rec["count"] = max(int(rec.get("count", 0)), count)
        self._save_state()

    # ---------- 书评(看完后自动发一条) ----------
    def _reviewed(self, book: str) -> bool:
        """这本书是否已发过书评(任意日期, 每本书只发一条)。"""
        rec = self.state.get("reviewed", {}).get(book) or {}
        return bool(rec.get("posted"))

    def _mark_reviewed(self, book: str) -> None:
        self.state.setdefault("reviewed", {})[book] = {
            "date": date.today().isoformat(), "posted": True}
        self._save_state()
        self.log.info("《%s》已记录书评(不重复发)", book)

    # ---------- AI 文案生成(OpenAI 兼容接口; 未配置/失败自动降级预设池) ----------
    def _ai_complete(self, system: str, user: str) -> str | None:
        """调用 OpenAI 兼容的 chat/completions 接口, 返回正文; 任何失败返回 None。"""
        cfg = self.cfg
        if not (cfg.ai_enabled and cfg.ai_api_key and cfg.ai_base_url):
            return None
        payload = {
            "model": cfg.ai_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.9,
            "max_tokens": 120,
        }
        req = urllib.request.Request(
            cfg.ai_base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + cfg.ai_api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=cfg.ai_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = (data["choices"][0]["message"]["content"] or "").strip()
            if not text:
                return None
            return text.strip('"“”\n ')
        except Exception as exc:
            self.log.warning("AI 文案生成失败(降级预设文案): %s", exc)
            return None

    def _ai_review_text(self, book: str) -> str | None:
        """按书名生成一条自然读者口吻的书评; 未配置 AI 时返回 None(调用方降级)。"""
        if not (self.cfg.ai_enabled and self.cfg.ai_api_key and self.cfg.ai_base_url):
            return None
        system = "你是一个番茄小说读者, 正在给刚看完的小说写书评。只输出书评内容本身, 不要引号、不要任何多余说明。"
        user = (
            f"请为小说《{book}》写一条书评, 要求: "
            "1) 60字以内, 第一人称, 像真实读者的自然口吻; "
            "2) 语气热情但不夸张, 可提追更/等更新/催更; "
            "3) 不剧透具体情节; "
            "4) 只输出书评正文。"
        )
        return self._ai_complete(system, user)

    def _ai_discussion_text(self, book: str) -> str | None:
        """按书名生成一条讨论区发帖文案(预测剧情/角色讨论等); 未配置 AI 时返回 None。"""
        if not (self.cfg.ai_enabled and self.cfg.ai_api_key and self.cfg.ai_base_url):
            return None
        system = "你是一个番茄小说读者, 正在小说讨论区发讨论帖。只输出帖子内容本身, 不要引号、不要多余说明。"
        user = (
            f"请为小说《{book}》写一条讨论帖, 要求: "
            "1) 40字以内, 口语化, 吸引书友互动; "
            "2) 可预测剧情/讨论角色/人物关系; "
            "3) 不剧透; "
            "4) 只输出帖子正文。"
        )
        return self._ai_complete(system, user)

    # ---------- 作者更新时间: 等待 / 今日已启动 ----------
    def _update_wait_marked(self, book: str) -> bool:
        return self.state.get("update_wait", {}).get(book) == date.today().isoformat()

    def _mark_update_wait(self, book: str) -> None:
        self.state.setdefault("update_wait", {})[book] = date.today().isoformat()
        self._save_state()

    def _started_today(self, book: str) -> bool:
        return self.state.get("started", {}).get(book) == date.today().isoformat()

    def _mark_started(self, book: str) -> None:
        self.state.setdefault("started", {})[book] = date.today().isoformat()
        self._save_state()

    # ---------- 搜索不到的书: 当天标记完成跳过, 次日再搜一次 ----------
    def _search_missed_today(self, book: str) -> bool:
        return self.state.get("search_miss", {}).get(book) == date.today().isoformat()

    def _mark_search_miss(self, book: str) -> None:
        self.state.setdefault("search_miss", {})[book] = date.today().isoformat()
        self._save_state()
        self.log.info("《%s》标记今日完成(搜索不到), 明日再搜", book)

    def _clear_search_miss(self, book: str) -> None:
        if "search_miss" in self.state and book in self.state["search_miss"]:
            del self.state["search_miss"][book]
            self._save_state()

    # ---------- 免费礼物 ----------
    def _gift_send_button(self):
        """读取礼物面板右下角赠送按钮, 返回 (文字, 中心坐标) 或 None。"""
        d = self.d
        try:
            sel = d(resourceId=GIFT_SEND_BTN_ID)
            if sel.exists(1.5):
                info = sel.info
                l, t, r, b = info["bounds"]
                txt = info.get("text") or ""
                return txt, ((l + r) // 2, (t + b) // 2)
        except Exception as exc:
            self.log.debug("读赠送按钮失败: %s", exc)
        try:
            xml = d.dump_hierarchy()
            for m in re.finditer(
                r'<node[^>]*text="([^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*/>',
                xml,
            ):
                txt, l, t, r, b = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
                if txt.strip() and l >= 400 and 1160 <= t <= 1185 and r == 688 and 1240 <= b <= 1264:
                    return txt, ((l + r) // 2, (t + b) // 2)
        except Exception as exc:
            self.log.debug("扫控件树读按钮失败: %s", exc)
        return None

    def _watch_ad_gift(self) -> bool:
        """看广告送礼物(用户指定流程, 手动验证通过):

        点「看广告支持作者」后进广告 -> 等广告播完(右上角倒计时结束显示「领取成功」,
        实测约20秒) -> 点右上角 × 退出(实测位置约 (0.94, 0.107) = (677,137))
        -> 出现「感谢你赠送的用爱发电」提示(礼物送出) -> 点「好的」-> 回礼物面板。
        返回是否成功送出(回到面板/送出提示)。
        """
        d = self.d
        # 阶段1: 等广告播完(倒计时约20-50秒; dump 检测不到「领取成功」(自绘),
        # 只能等足够长时间覆盖最长广告, 同时检测 activity 变化提前退出)
        ad_done = False
        for _try in range(10):
            if d(text=GIFT_PANEL_MARKER).exists(1.0):
                return True  # 广告瞬间结束已回面板
            try:
                act = self._current_activity()
            except Exception:
                act = ""
            if not re.search(r"(webview|\.live\.|video|exciting)", act, re.I):
                # 已离开广告页(可能自动关闭回到阅读器/面板)
                ad_done = True
                break
            xml = ""
            try:
                xml = d.dump_hierarchy()
            except Exception:
                pass
            if "感谢你赠送" in xml:
                ad_done = True
                break
            self.sleep_human(6.0, 7.0)
        # 阶段2: 点右上角 × 关闭广告(实测不同广告 × 位置不同: 677,137 / 636,28 等),
        # 用右上角区域多点网格尝试, 每次点击后检查是否已回面板/离开广告页
        ad_close_points = [
            (0.94, 0.107), (0.88, 0.08), (0.92, 0.05),
            (0.86, 0.14), (0.95, 0.06), (0.90, 0.11),
        ]
        for _try in range(2):
            if d(text=GIFT_PANEL_MARKER).exists(1.0):
                return True
            for (px, py) in ad_close_points:
                try:
                    d.click(px, py)
                except Exception:
                    pass
                self.sleep_human(1.5, 2.0)
                # 回面板 或 出现「感谢你赠送」(礼物送出) 都算成功
                if d(text=GIFT_PANEL_MARKER).exists(1.2):
                    return True
                xml = ""
                try:
                    xml = d.dump_hierarchy()
                except Exception:
                    pass
                if "感谢你赠送" in xml:
                    # 礼物已送出: 点「好的」关闭提示回面板
                    self.click_text("好的", 2.0)
                    self.sleep_human(1.5, 2.5)
                    return True
                try:
                    act = self._current_activity()
                except Exception:
                    act = ""
                if not re.search(r"(webview|\.live\.|video|exciting)", act, re.I):
                    return True  # 已离开广告页(可能自动关闭回阅读器/面板)
            self.sleep_human(9.5, 10.5)
        # 阶段3: 广告页仍关不掉: 先返回, 再强制重启番茄 app 兜底
        try:
            d.press("back")
        except Exception:
            pass
        self.sleep_human(1.0, 1.5)
        if d(text=GIFT_PANEL_MARKER).exists(1.5):
            return True
        try:
            d.app_stop(APP_PACKAGE)
            self.sleep_human(2.0, 3.0)
            d.app_start(APP_PACKAGE)
            self.sleep_human(4.0, 6.0)
            self.log.warning("广告页无法关闭, 已强制重启番茄 app")
        except Exception as exc:
            self.log.warning("强制重启番茄 app 失败: %s", exc)
        return d(text=GIFT_PANEL_MARKER).exists(1.5)

    def _send_free_gifts(self, want: int):
        """打开礼物面板, 按用户指定流程送免费礼物:

        点「送礼物」进入面板(默认免费礼物「用爱发电」已选中) -> 点「看广告支持作者」
        -> 看广告15秒 -> 点右上角按钮退出 -> 回到面板算送一次; 一天一个账号可送3次。
        返回 (实际送出次数, 额度是否已用完)。
        """
        d = self.d
        opened = False
        for _ in range(3):
            if d(text=GIFT_PANEL_MARKER).exists(1.2):
                opened = True
                break
            if self.click_text(GIFT_ENTRY_TEXT, 1.5):
                self.sleep_human(1.5, 2.5)
                opened = d(text=GIFT_PANEL_MARKER).exists(2.0)
                break
            # 书末卡片页的礼物入口是礼物图标(Compose 绘制, 无「送礼物」文本):
            # 实测位置约 (658,373) = (0.914,0.291); 非书末页点到正文无反应, 无害
            try:
                d.click(0.914, 0.291)
            except Exception:
                pass
            self.sleep_human(1.5, 2.5)
            if d(text=GIFT_PANEL_MARKER).exists(2.0):
                opened = True
                break
            self.sleep_human(1.0, 1.5)
        if not opened:
            self.log.warning("礼物面板未打开")
            return 0, False
        # 面板内容加载: 新版面板打开后先显示「登录后送礼物支持作者吧」占位,
        # 点击占位文本区域可触发内容加载; 等「看广告支持作者」按钮出现(最多约12秒)
        panel_placeholder_seen = False
        for _wait in range(12):
            if d(text="看广告支持作者").exists(1.0):
                break
            if d(text="登录后送礼物支持作者吧").exists(0.5):
                # 占位提示: 点击触发面板内容加载(手动验证: 点击后礼物列表出现)
                panel_placeholder_seen = True
                try:
                    d(text="登录后送礼物支持作者吧").click(timeout=0.5)
                except Exception:
                    pass
                self.sleep_human(1.0, 1.2)
                continue
            self.sleep_human(1.0, 1.2)
        if not d(text="看广告支持作者").exists(1.0):
            # 诊断: 面板内容未加载, 打印面板实际文本
            xml = ""
            try:
                xml = d.dump_hierarchy()
            except Exception:
                pass
            texts = [t for t in re.findall(r'text="([^"]*)"', xml) if t.strip()][:10]
            self.log.warning("礼物面板加载后仍无「看广告支持作者」(占位提示=%s), 面板文本=%s",
                             panel_placeholder_seen, texts)
        sent = 0
        exhausted = False
        for _ in range(want):
            # 1) 优先按用户流程: 点「看广告支持作者」看广告送一次
            if not self.click_text("看广告支持作者", 2.0):
                # Compose 渲染按钮 text 匹配/点击不稳定(文本在但点击无效):
                # 用坐标兜底(实测面板底部「看广告支持作者」约在 (562,1148) ≈ (0.78,0.897))
                try:
                    d.click(0.78, 0.897)
                except Exception:
                    pass
                self.sleep_human(1.0, 1.5)
            if self._watch_ad_gift():
                sent += 1
                self.log.info("看广告送礼物 %d/%d 完成", sent, want)
                continue
            self.log.warning("看广告后未回到礼物面板, 送礼物环节结束")
            exhausted = True
            break
            # 2) 兜底: 免费礼物「用爱发电」直接送(如有免费次数)
            sel = d(text=FREE_GIFT_TEXT)
            if sel.exists(1.5):
                try:
                    sel.click(timeout=1.0)
                except Exception as exc:
                    self.log.warning("选择礼物失败: %s", exc)
                    exhausted = True
                    break
                self.sleep_human(0.8, 1.2)
                btn = self._gift_send_button()
                if not btn:
                    self.log.warning("未读到赠送按钮")
                    exhausted = True
                    break
                txt, (cx, cy) = btn
                if "已用完" in txt:
                    self.log.info("今日免费礼物次数已用完")
                    exhausted = True
                    break
                if "赠送" in txt and "¥" not in txt:
                    d.click(cx, cy)
                    sent += 1
                    self.log.info("已送免费礼物 %d/%d", sent, want)
                    self.sleep_human(1.5, 2.5)
                    continue
                if "看广告" in txt or "广告" in txt:
                    d.click(cx, cy)
                    self.log.info("赠送按钮为看广告, 已点击, 等待广告结束")
                    if self._watch_ad_gift():
                        sent += 1
                        self.log.info("看广告送礼物 %d/%d", sent, want)
                        continue
                    self.log.warning("看广告后未回到礼物面板, 送礼物环节结束")
                    exhausted = True
                    break
                self.log.warning("赠送按钮状态异常: %r, 送礼物环节结束", txt)
                exhausted = True
                break
            # 3) 都没有: 今日免费礼物额度已用完
            self.log.warning("礼物面板无「看广告支持作者」也无「用爱发电」, 今日免费礼物跳过")
            exhausted = True
            break
        for _ in range(2):
            try:
                d.press("back")
            except Exception:
                break
            self.sleep_human(0.8, 1.2)
            if not d(text=GIFT_PANEL_MARKER).exists(1.0):
                break
        self.sleep_human(0.5, 1.0)
        return sent, exhausted

    # ---------- 搜索进书 ----------
    def _is_in_reader(self) -> bool:
        try:
            info = self.d.app_current()
            act = (info.get("activity") or "") if isinstance(info, dict) else ""
        except Exception:
            return False
        return "reader" in act.lower()

    def _current_activity(self) -> str:
        try:
            info = self.d.app_current()
            return (info.get("activity") or "") if isinstance(info, dict) else ""
        except Exception:
            return ""

    def _click_search_box(self) -> bool:
        """检测并点击书城首页顶部的搜索框; 兼容两种布局: ComposeView(旧) / 原生可点击通栏(新)。"""
        try:
            xml = self.d.dump_hierarchy()
            candidates = []
            for m in re.finditer(r"<node\b[^>]*>", xml):
                tag = m.group(0)
                cls = re.search(r'class="([^"]*)"', tag)
                bnd = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
                if not (cls and bnd):
                    continue
                l, t, r, b = map(int, bnd.groups())
                if t < 200 and r - l > 300:  # 顶部通栏
                    click = re.search(r'clickable="(true|false)"', tag)
                    candidates.append((cls.group(1), l, t, r, b, click.group(1) if click else "false"))
            # 优先 ComposeView(旧布局)
            for cls, l, t, r, b, _ in candidates:
                if "ComposeView" in cls and l < 100:
                    self.log.info("点击搜索框(ComposeView @ %d,%d)", (l + r) // 2, (t + b) // 2)
                    self.d.click((l + r) // 2, (t + b) // 2)
                    return True
            # 其次顶部可点击通栏(新版布局, 如 FrameLayout 搜索框)
            for cls, l, t, r, b, clk in candidates:
                if clk == "true" and l < 100:
                    self.log.info("点击搜索框(%s @ %d,%d)", cls.split(".")[-1], (l + r) // 2, (t + b) // 2)
                    self.d.click((l + r) // 2, (t + b) // 2)
                    return True
            self.log.info("未发现可点击的搜索框候选(顶部通栏=%d)", len(candidates))
        except Exception as exc:
            self.log.debug("检测搜索框失败: %s", exc)
        return False

    def _goto_search_page(self) -> bool:
        """确保停在搜索页(SearchActivity)。"""
        d = self.d
        for i in range(6):
            act = self._current_activity()
            self.log.info("搜索页检查[%d/6]: activity=%s", i + 1, act)
            if SEARCH_ACTIVITY in act:
                return True
            if "MainFragmentActivity" in act:
                # 主界面: 先切到书城 tab, 再点顶部搜索框
                self.click_text("书城", 1.5)
                self.sleep_human(0.8, 1.5)
                if self._click_search_box():
                    self.sleep_human(1.5, 2.5)
                    continue
            # 其余页面(阅读器/详情/面板): 返回键逐级退出
            try:
                d.press("back")
            except Exception:
                pass
            self.sleep_human(1.0, 1.5)
        return SEARCH_ACTIVITY in self._current_activity()

    def _best_result(self, name: str):
        """在搜索结果里找与书名最匹配的一条, 返回 (标题, bounds) 或 None。"""
        xml = self.d.dump_hierarchy()
        cands = []
        # 优先按资源 id 收集标题(旧版 App: com.dragon.read:id/ale)
        for m in re.finditer(
            r'<node[^>]*text="([^"]+)"[^>]*resource-id="%s"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
            % SEARCH_RESULT_TITLE_ID,
            xml,
        ):
            t, l, tp, r, b = m.groups()
            t = t.strip()
            if t:
                cands.append((t, (int(l), int(tp), int(r), int(b))))
        if not cands:
            # 兼容新版 App(资源 id 不同): 扫描全部文本节点, 按相似度过滤
            min_len = max(4, int(len(name) * 0.6))
            for m in re.finditer(
                r'<node[^>]*text="([^"]+)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml
            ):
                t, l, tp, r, b = m.groups()
                t = t.strip()
                if "EditText" in m.group(0):  # 排除输入框自身(其文本=书名, 点击无导航)
                    continue
                if len(t) >= min_len:
                    cands.append((t, (int(l), int(tp), int(r), int(b))))
        if not cands:
            return None

        def norm(s: str) -> str:
            return re.sub(r"[\s\u3000]+", "", s)

        scored = [
            (difflib.SequenceMatcher(None, norm(t), norm(name)).ratio(), t, bnd)
            for t, bnd in cands
        ]
        scored.sort(reverse=True)
        ratio, title, bnd = scored[0]
        self.log.info("搜索结果最匹配: 《%s》(相似度 %.2f)", title, ratio)
        return title, ratio, bnd

    def _enter_via_search(self, name: str) -> bool:
        """从书城搜索进入目标书; 结果与书名对不上则跳过。"""
        d = self.d
        if not self._goto_search_page():
            self.log.warning("无法进入搜索页")
            return False
        # 输入书名并搜索
        for _ in range(2):
            ed = None
            sel = self.d(resourceId=SEARCH_INPUT_ID)
            if sel.exists(2.0):
                ed = sel
            else:
                # 兼容不同版本 App 资源 id 差异: 搜索页只有一个输入框, 退回按类名找
                ed = self.d(className="android.widget.EditText")
                if not ed.exists(2.0):
                    ed = None
            if ed is not None:
                try:
                    ed.click(timeout=1.0)
                    self.sleep_human(0.5, 1.0)
                    ed.set_text(name)
                    self.sleep_human(0.5, 1.0)
                    break
                except Exception as exc:
                    self.log.warning("输入搜索词失败: %s", exc)
            self.sleep_human(1.0, 1.5)
        else:
            return False
        if not self.click_text("搜索", 2.0):
            try:
                d.press("enter")
            except Exception:
                pass
        self.sleep_human(2.5, 3.5)
        res = self._best_result(name)
        if not res:
            self.log.warning("搜索无结果, 跳过《%s》", name)
            return False
        title, ratio, bnd = res
        if ratio < MATCH_RATIO_MIN:
            self.log.warning("搜索结果「%s」与书名不匹配(%.2f), 跳过", title, ratio)
            return False
        # 点进书籍
        l, t, r, b = bnd

        def _tap_result_reentry():
            try:
                d.click((l + r) // 2, (t + b) // 2)
            except Exception as exc:
                self.log.warning("点击搜索结果失败: %s", exc)
                return False
            self.sleep_human(2.0, 3.0)
            self.handle_interruptions()
            return True

        _tap_result_reentry()
        self._click_reader_entry()
        # 若进书后落在书末信息流页(「书末页」+「分享」, 上次退出阅读器时停在信息流导致
        # resume 被保存为信息流): 信息流页无页码、水平滑动失效, 翻页会在那里卡死;
        # 按返回退出阅读器后番茄会把 resume 回退到书末卡片页(全书最后一页),
        # 重新点结果进书即可触发书末卡片页识别(本章讨论+1次)。
        try:
            _xml = self.d.dump_hierarchy()
            if "书末页" in _xml and "分享" in _xml:
                self.log.info("进书后落在书末信息流页(resume 被保存为信息流), 返回退出后重新进书")
                try:
                    self.d.press("back")
                except Exception:
                    pass
                self.sleep_human(1.5, 2.5)
                _tap_result_reentry()
                self._click_reader_entry()
        except Exception:
            pass
        return True

    def _click_reader_entry(self) -> bool:
        # 调试: 进书后必打一次 dump 特征, 定位 resume 落在什么页面
        # (简介界面/书末卡片/信息流/正文/转圈加载页)
        try:
            _dbg0 = self.d.dump_hierarchy()
            _dbg0_t = re.findall(r'text="([^"]*)"', _dbg0)
            self.log.info("进书后dump: 简介=%s 本章讨论=%s 书末页=%s 开始阅读=%s 继续阅读=%s 左滑开始阅读=%s 短文本=%s",
                          "简介" in _dbg0, "本章讨论" in _dbg0, "书末页" in _dbg0,
                          "开始阅读" in _dbg0, "继续阅读" in _dbg0, "左滑开始阅读" in _dbg0,
                          [t for t in _dbg0_t if t and len(t) < 8][:12])
        except Exception as exc:
            self.log.warning("进书后dump调试失败: %s", exc)
        # 0) 先检测是否落在书籍简介界面(进书后常见, 展示 简介/作者/标签, 无正文无页码,
        #    有「左滑开始阅读」手势引导): 必须从右到左滑动一下(手指从屏幕右侧滑向左侧,
        #    即 580->120, 起点必须离开屏幕右边缘否则会触发 Android 返回手势退出书籍)
        #    才会进入正文开始阅读(点击「开始阅读」文字/按钮可能无效)。「简介」是详情页
        #    特征, 正文/书末卡片/信息流页均无此文本; 页面加载时「简介」可能延迟出现,
        #    故重试 2 次(每次间隔等待加载)。
        for _retry in range(2):
            try:
                _d_xml0 = self.d.dump_hierarchy()
                if ("简介" in _d_xml0 and "本章讨论" not in _d_xml0) or "左滑开始阅读" in _d_xml0:
                    self.log.info("进书后落在书籍简介界面, 从右到左滑动进入正文")
                    try:
                        self.d.swipe(580, 640, 120, 640, duration=0.5)
                    except Exception:
                        pass
                    self.sleep_human(2.0, 3.0)
                    self.handle_interruptions()
                    # 滑动后验证仍在阅读器(若被识别为返回手势退出, 交由上层恢复重进)
                    try:
                        if not self._is_in_reader():
                            self.log.warning("简介界面滑动后离开阅读器(可能触发返回手势), 交由上层恢复")
                            return False
                    except Exception:
                        pass
                    # 滑动后重新检测书末卡片页(可能直接进入书末卡片页, 绝不左滑翻页)
                    try:
                        _after0 = self.d.dump_hierarchy()
                        if "本章讨论" in _after0 and "去圈子" not in _after0:
                            self.log.info("滑动进入后已在书末卡片页(本章讨论), 不做翻页动作")
                            return True
                    except Exception:
                        pass
                    return True
            except Exception:
                pass
            self.sleep_human(1.5, 2.5)
        for text in READER_ENTRY_TEXTS[:2]:  # 继续阅读 / 开始阅读
            if self.click_text(text, 1.2):
                self.sleep_human(2.0, 3.0)
                self.handle_interruptions()
                # 点「开始阅读/继续阅读」后可能仍在简介界面(点击无效, 需从右到左滑动)
                # 或已进入正文; 若仍检测到「简介」(页面此时应已加载完)则滑动进入正文
                try:
                    _after_click = self.d.dump_hierarchy()
                    if ("简介" in _after_click and "本章讨论" not in _after_click) \
                            or "左滑开始阅读" in _after_click:
                        self.log.info("点击开始阅读后仍在简介界面, 从右到左滑动进入正文")
                        try:
                            self.d.swipe(580, 640, 120, 640, duration=0.5)
                        except Exception:
                            pass
                        self.sleep_human(2.0, 3.0)
                        self.handle_interruptions()
                        # 滑动后重新检测书末卡片页
                        try:
                            _after2 = self.d.dump_hierarchy()
                            if "本章讨论" in _after2 and "去圈子" not in _after2:
                                self.log.info("滑动进入后已在书末卡片页(本章讨论), 不做翻页动作")
                                return True
                        except Exception:
                            pass
                    return True
                except Exception:
                    pass
                return True
        # 已在书末卡片页(最新章末, resume 直接落在这里): dump 可见「本章讨论」标题
        # (正文页没有此文本, 书末卡片页专属) -> 绝不能左滑! (左滑=翻页, 会把书末卡片
        # 翻到书末信息流, 导致看不到催更按钮而卡死), 直接返回让翻页循环处理书末流程。
        # 注: 催更按钮下方小字(如「1次」/「2万」)是动态文本, 不可作为判据。
        try:
            _entry_xml = self.d.dump_hierarchy()
            if "本章讨论" in _entry_xml and "去圈子" not in _entry_xml:
                self.log.info("进书后已在书末卡片页(本章讨论), 不做翻页动作")
                return True
        except Exception:
            pass
        # 部分版本详情页入口文字为「左滑开始阅读」
        if self.click_text("左滑开始阅读", 1.2, fuzzy=True):
            self.sleep_human(2.0, 3.0)
            self.handle_interruptions()
            return True
        return False

    def _post_book_review(self, bt: BookTask) -> str:
        """看完后在书末页直接发一条自然读者风格的书评(实测完整流程):

        书末页「写点感悟」-> AI文案页输入评论 -> 下一步 -> 选择封面 -> 下一步
        -> 发讨论发布页(正文/关联书自动带出) -> 点「发表」-> 发布成功跳转讨论页。
        注意: 发布页顶部「发讨论」只是标题, 不是按钮; 发布按钮是底部「发表」。
        """
        d = self.d
        name = bt.name
        self.log.info("《%s》读完, 开始自动发书评", name)
        # 书读完可能弹出「点评此书」星级评分引导弹窗(喜欢本书吗？轻按星星给作者打气 +
        # 取消) —— 它本身也是发书评(点评)的入口: 点星星评分后通常会出现写文字评论的
        # 输入框/「下一步」, 可以直接输入书评发表。优先走这个入口(星级 config.review_star,
        # 默认4, 网页控制台可改), 失败再点「取消」关掉弹窗, 回退到书末卡片页的
        # 写点感悟/本章讨论 入口。
        try:
            _rv0 = self.d.dump_hierarchy()
            if "点评此书" in _rv0 or "喜欢本书吗" in _rv0:
                _star = min(5, max(1, int(getattr(self.cfg, "review_star", 4) or 4)))
                _sx = {1: 230, 2: 300, 3: 370, 4: 440, 5: 510}.get(_star, 440)
                self.log.info("书评: 检测到「点评此书」评分引导弹窗, 点第 %d 颗星", _star)
                self.d.click(_sx, 990)  # 星星位于提示文字下方、取消按钮上方
                self.sleep_human(1.5, 2.5)
                self.handle_interruptions(quick=True)
                # 点星后可能有输入框(直接写评论)或「下一步」(进入文字编辑页)
                _ed2 = self.d(className="android.widget.EditText")
                if _ed2.exists(3.0):
                    _text = self._ai_review_text(name) or random.choice(REVIEW_TEXTS)
                    try:
                        _ed2.click(timeout=1.0)
                        self.sleep_human(0.5, 1.0)
                        _ed2.set_text(_text)
                        self.log.info("书评: 已通过「点评此书」输入: %s", _text)
                    except Exception as exc:
                        self.log.warning("书评: 点评输入失败 %s", exc)
                    self.sleep_human(0.8, 1.5)
                    if self.click_text("下一步", 2.0):
                        self.sleep_human(2.0, 3.0)
                        self.handle_interruptions(quick=True)
                    for _b in ("发表", "发布作品", "发布", "提交", "确定", "完成"):
                        if self.click_text(_b, 2.0):
                            self.sleep_human(2.0, 3.0)
                            self.log.info("《%s》书评已发布(点评入口): %s", name, _text)
                            return "ok"
                    self.log.warning("书评: 点评输入后未找到发表按钮")
                    self.d.press("back")
                    self.sleep_human(1.0, 1.5)
                elif self.click_text("下一步", 2.0):
                    self.log.info("书评: 点评弹窗点星后出现「下一步」, 进入文字编辑页")
                    self.sleep_human(2.0, 3.0)
                    self.handle_interruptions(quick=True)
                    _ed3 = self.d(className="android.widget.EditText")
                    if _ed3.exists(3.0):
                        _text = self._ai_review_text(name) or random.choice(REVIEW_TEXTS)
                        try:
                            _ed3.click(timeout=1.0)
                            self.sleep_human(0.5, 1.0)
                            _ed3.set_text(_text)
                        except Exception as exc:
                            self.log.warning("书评: 点评编辑输入失败 %s", exc)
                        self.sleep_human(0.8, 1.5)
                        for _b in ("发表", "发布作品", "发布", "提交", "确定", "完成"):
                            if self.click_text(_b, 2.0):
                                self.sleep_human(2.0, 3.0)
                                self.log.info("《%s》书评已发布(点评-下一步入口): %s", name, _text)
                                return "ok"
                        self.log.warning("书评: 点评编辑后未找到发表按钮")
                # 点星后无文字输入能力: 关掉弹窗, 回退到书末卡片页入口
                self.log.info("书评: 点评弹窗无文字输入能力, 点取消回退")
                if self.click_text("取消", 2.0):
                    self.sleep_human(1.0, 1.8)
        except Exception as exc:
            self.log.warning("书评: 点评弹窗处理异常 %s", exc)
        # 0) 书末卡片页可能处于工具栏展开状态(上一章/下一章/目录 可见, 遮挡页码与
        #    书评入口按钮)。实测 resume 后点「本章讨论」会被工具栏拦截(只收起工具栏,
        #    不进入讨论页, 导致后续 EditText 找不到而发书评失败), 先点屏幕中部收起
        #    工具栏(0.5,0.5 在书末卡片页中部, 不落在按钮区域, 安全)
        try:
            _bx0 = self.d.dump_hierarchy()
            if any(t in _bx0 for t in ("上一章", "下一章", "目录")) and any(
                    t in _bx0 for t in ("本章讨论", "写点感悟", "发讨论")):
                self.log.info("书评: 书末卡片页工具栏展开, 先收起工具栏再点入口")
                self.d.click(0.5, 0.5)
                self.sleep_human(0.8, 1.5)
        except Exception:
            pass
        # 1) 书末页点「写点感悟」(评论入口; 截图确认书末页有 写点感悟/发讨论/送礼物 一排;
        #    部分版本书末卡片页没有「写点感悟」, 但一定有「本章讨论」按钮(正文页无此文本),
        #    点它进入讨论页后同样可以发书评)
        _entry_kind = "review"  # review=写点感悟/点评入口; discussion=本章讨论/发讨论入口
        entered = (self.click_text("写点感悟", 2.0) or self.click_text("发评论", 2.0)
                   or self.click_text("写评论", 2.0))
        if not entered:
            self.log.info("书评: 未找到「写点感悟」, 尝试「本章讨论」入口")
            # 书末卡片页的「本章讨论」按钮(正文页无此文本); 催更后页面可能短暂
            # 重绘导致节点暂时消失, 重试 3 次(每次间隔等待恢复); 点击后确认进入了
            # 讨论页(趣评/去圈子/发帖/我要发言/EditText 出现)才算成功, 否则可能是
            # 被工具栏拦截(仍停在书末卡片页), 收起工具栏后重试
            for _retry in range(3):
                if self.click_text("本章讨论", 2.0) or self.click_text("发讨论", 2.0):
                    self.sleep_human(1.0, 1.5)
                    try:
                        _ax = self.d.dump_hierarchy()
                        if any(t in _ax for t in ("趣评", "去圈子", "发帖", "我要发言", "预测剧情", "下一步")) or "EditText" in _ax:
                            entered = True
                            _entry_kind = "discussion"
                            break
                        # 点击后被工具栏拦截(仍停在书末卡片页): 收起工具栏后重试
                        self.log.info("书评: 点「本章讨论」后未进入讨论页, 收起工具栏重试")
                        self.d.click(0.5, 0.5)
                        self.sleep_human(0.8, 1.5)
                    except Exception:
                        entered = True
                        _entry_kind = "discussion"
                        break
                self.sleep_human(1.0, 1.5)
        if not entered:
            self.log.warning("书评: 书末页未找到「写点感悟」评论入口")
            return "fail"
        self.sleep_human(2.0, 3.0)
        self.handle_interruptions(quick=True)
        # 2) AI文案页: 输入评论(支持中文 set_text); 若点「本章讨论」进入的是章节讨论页
        #    (空讨论区, 提示「期待你的第一条讨论」), 需再点底部引导「趣评千万条，你也来一条」
        #    或右上「去圈子」进入发帖编辑页(实测该页有 EditText+发表); 讨论列表页则找
        #    发帖入口(发讨论/发帖/写点感悟/我要发言)再进入编辑页
        ed = self.d(className="android.widget.EditText")
        if not ed.exists(3.0):
            if not (self.click_text("趣评千万条，你也来一条", 2.0, fuzzy=True)
                    or self.click_text("去圈子", 2.0)
                    or self.click_text("发讨论", 2.0) or self.click_text("发帖", 2.0)
                    or self.click_text("写点感悟", 2.0) or self.click_text("我要发言", 2.0)):
                self.log.warning("书评: 未找到评论输入框/发帖入口")
                return "fail"
            self.sleep_human(2.0, 3.0)
            self.handle_interruptions(quick=True)
            ed = self.d(className="android.widget.EditText")
            if not ed.exists(3.0):
                self.log.warning("书评: 仍未找到评论输入框")
                return "fail"
        if _entry_kind == "discussion":
            _ai = self._ai_discussion_text(name)
        else:
            _ai = self._ai_review_text(name)
        text = _ai if _ai else random.choice(REVIEW_TEXTS)
        self.log.info("书评: 文案来源=%s(入口=%s)", "AI生成" if _ai else "预设池", _entry_kind)
        try:
            ed.click(timeout=1.0)
            self.sleep_human(0.5, 1.0)
            ed.set_text(text)
        except Exception as exc:
            # set_text 失败(输入法切换问题): 尝试 fastinput 快速输入法
            try:
                self.d.set_fastinput_ime(True)
                self.sleep_human(0.5, 1.0)
                ed.click(timeout=1.0)
                self.sleep_human(0.5, 1.0)
                self.d.send_keys(text)
            except Exception as exc2:
                self.log.warning("书评: 输入失败 %s / %s", exc, exc2)
                return "fail"
        self.log.info("书评: 已输入内容: %s", text)
        self.sleep_human(0.8, 1.5)
        # 3) 下一步(AI生成) -> 下一步(选择配图) -> 下一步(发布页); 实测番茄新流程
        #    需要点 3 次下一步(多一个「选择配图」页), 循环点击直到发布按钮出现
        _pub_found = False
        for _step in range(3):
            if self.click_text("下一步", 2.0):
                self.sleep_human(2.5, 3.5)
                self.handle_interruptions(quick=True)
                try:
                    _px = self.d.dump_hierarchy()
                    if any(t in _px for t in ("发表", "发布作品")):
                        _pub_found = True
                        break
                except Exception:
                    pass
            else:
                break
        if not _pub_found:
            try:
                _px = self.d.dump_hierarchy()
                if any(t in _px for t in ("发表", "发布作品")):
                    _pub_found = True
            except Exception:
                pass
        # 4) 发布页: 点「发表」(实测按钮名; 兜底 发布/发布作品)
        if not (self.click_text("发表", 3.0) or self.click_text("发布", 3.0)
                or self.click_text("发布作品", 3.0)):
            self.log.warning("书评: 未找到「发表」按钮")
            return "fail"
        self.sleep_human(2.0, 3.0)
        # 5) 发布成功页(实测「发布成功！讨论已发布到评论区」+ 好的/查看 按钮): 点「好的」关闭
        try:
            if self.click_text("好的", 2.0):
                self.sleep_human(1.0, 2.0)
        except Exception:
            pass
        self.log.info("《%s》书评已发布: %s", name, text)
        return "ok"

    def _recover_to_reader(self, book_name: str = "") -> bool:
        """应用离开阅读器时尝试恢复。"""
        d = self.d
        for rnd in range(3):
            if self._is_in_reader():
                return True
            act = self._current_activity()
            try:
                xml = d.dump_hierarchy()
                texts = [t for t in re.findall(r'text="([^"]*)"', xml) if t.strip()][:12]
            except Exception:
                texts = []
            self.log.info("恢复[%d/3]: activity=%s texts=%s", rnd + 1, act, texts)
            if "RewardActivity" in act:
                # 礼物面板: 先按返回关闭(回阅读器); 关不掉才强杀重启
                try:
                    d.press("back")
                except Exception:
                    pass
                self.sleep_human(1.0, 1.5)
                if self._is_in_reader():
                    return True
                self.log.warning("礼物面板无法关闭, 强杀番茄后重启")
                d.app_stop(APP_PACKAGE)
                self.sleep_human(2.0, 3.0)
                d.app_start(APP_PACKAGE)
                self.sleep_human(4.0, 6.0)
                self.handle_interruptions()
                continue
            if re.search(r"(webview|\.live\.|video|exciting)", act, re.I):
                # 碰到广告/直播页: 先等10秒(可能自动关闭); 若还在广告里,
                # 再等50秒(凑够60秒, 覆盖最长约51秒广告)确保播完(右上角显示「领取成功」),
                # 此时点右上角 × 才能正常退出(提前点×会弹出「继续观看/坚持退出」卡住)
                self.log.info("碰到广告/直播页(%s), 等10秒不点进去", act)
                self.sleep_human(9.5, 10.5)
                if not self._is_in_reader() and re.search(
                    r"(webview|\.live\.|video|exciting)", self._current_activity(), re.I
                ):
                    self.log.info("还在广告里, 等广告播完(约50秒)后从右上角关闭退出")
                    self.sleep_human(49.5, 50.5)
                    # 不同广告 × 位置不同(实测 677,137 / 636,28 等): 右上角区域多点网格尝试
                    ad_close_points = [
                        (0.94, 0.107), (0.88, 0.08), (0.92, 0.05),
                        (0.86, 0.14), (0.95, 0.06), (0.90, 0.11),
                    ]
                    for _try in range(2):
                        if self._is_in_reader():
                            break
                        for (px, py) in ad_close_points:
                            if self._is_in_reader():
                                break
                            try:
                                d.click(px, py)
                            except Exception:
                                pass
                            self.sleep_human(1.5, 2.0)
                            if not re.search(
                                r"(webview|\.live\.|video|exciting)", self._current_activity(), re.I
                            ):
                                break
                        if not re.search(
                            r"(webview|\.live\.|video|exciting)", self._current_activity(), re.I
                        ):
                            break
                        # 还在广告(可能弹窗/倒计时未走完): 再等10秒重试
                        self.sleep_human(9.5, 10.5)
                if not self._is_in_reader():
                    # 广告页仍关不掉: 强制重启番茄 app(广告页是番茄进程内的 Activity)
                    self.log.warning("广告页无法关闭, 强制重启番茄 app")
                    try:
                        d.app_stop(APP_PACKAGE)
                        self.sleep_human(2.0, 3.0)
                        d.app_start(APP_PACKAGE)
                        self.sleep_human(4.0, 6.0)
                    except Exception as exc:
                        self.log.warning("强制重启番茄 app 失败: %s", exc)
                    self.sleep_human(1.0, 1.5)
                continue
            if "SearchActivity" in act:
                # 搜索页: 连续按返回(关键盘/清输入)退到首页
                for _ in range(3):
                    try:
                        d.press("back")
                    except Exception:
                        pass
                    self.sleep_human(0.8, 1.2)
                continue
            if "MainFragmentActivity" in act or "MainActivity" in act:
                # 可能在首页或「我的」页: 书架/我的 里没有「继续阅读」入口,
                # 先退回首页, 再用书城搜索重新进书(和进入书流程一致)
                if any(t in texts for t in ("关注", "粉丝", "获赞", "我的订单")):
                    # 在「我的」页: 先返回首页
                    try:
                        d.press("back")
                    except Exception:
                        pass
                    self.sleep_human(0.8, 1.2)
                if book_name and self._enter_via_search(book_name):
                    return True
                # 无书名或搜索失败: 退回旧逻辑(书架/我的)
                if self.click_text("书架", 1.5):
                    self.sleep_human(1.0, 2.0)
                    if self._click_reader_entry():
                        return True
                    try:
                        d.press("back")
                    except Exception:
                        pass
                    self.sleep_human(0.8, 1.5)
                if self.click_text("我的", 1.5):
                    self.sleep_human(1.0, 2.0)
                    if self._click_reader_entry():
                        return True
                continue
            try:
                d.press("back")
            except Exception:
                pass
            self.sleep_human(0.8, 1.5)
            if self._is_in_reader():
                return True
            if self._click_reader_entry():
                return True
            if self.click_text("我的", 1.5):
                self.sleep_human(1.0, 2.0)
                if self._click_reader_entry():
                    return True
            self.sleep_human(1.0, 2.0)
        return self._is_in_reader()

    # ---------- 单本书一轮 ----------
    def run_book(self, idx: int, bt: BookTask) -> str:
        """处理一本书; 返回 done / skipped / failed。"""
        name = bt.name
        want_gifts = bt.gift_count if bt.gift else 0
        urged = not self._can_urge_today(name)
        gifts_sent = min(self._gifted_today(name), want_gifts) if bt.gift else want_gifts
        self._bookshelf_done = False
        self._reached_end = False
        self.rt.update(current_book=name, step="搜索进书", pages=0)
        self.rt.set_book(idx, status="搜索进书", urged=urged, gifts=gifts_sent)
        self.log.info("===== 开始处理《%s》(送礼物=%s×%d) =====", name, bt.gift, bt.gift_count)
        # 搜索不到的书: 当天标记完成跳过, 次日自动再搜一次; 搜到了就正常跑
        if self._search_missed_today(name):
            self.log.info("《%s》今日已标记完成(搜索不到), 跳过, 明日再试", name)
            self.rt.set_book(idx, status="跳过", detail="搜索不到, 今日完成, 明日重试")
            return "skipped"
        if not self._enter_via_search(name):
            self._mark_search_miss(name)
            self.log.warning("《%s》搜索不到, 标记今日完成, 明日再搜", name)
            self.rt.set_book(idx, status="跳过", detail="搜索不到, 今日完成, 明日重试")
            return "skipped"
        self._clear_search_miss(name)
        # 阅读器内推进到书末页: 右侧点按一页一页翻, 不直接点下一章
        lo, hi = self.cfg.interval_range
        flips = 0
        stuck_taps = 0
        shelf_tries = 0
        turned_back_at_end = False  # 书末页按钮识别不到时已往前滑一页(下次再确认书末)
        book_total = None  # 阅读器页码总数, 用于识别是否跑错书
        while flips < MAX_FLIP_PER_BOOK:
            if self.rt.get()["stop_requested"]:
                self.log.info("收到停止请求")
                raise SystemExit(0)
            if not self._is_in_reader():
                self.log.warning("离开阅读器, 尝试恢复")
                if not self._recover_to_reader(name):
                    self.log.error("无法回到阅读器, 该书处理失败")
                    self.rt.set_book(idx, status="失败", detail="无法回到阅读器")
                    return "failed"
                continue
            # 加入书架: 首次进阅读器且还没加到(如搜索结果直达阅读器无详情页),
            # 打开阅读器菜单点顶栏「加入书架」; 已加时按钮显示「已加书架」, 视为已加入
            if not self._bookshelf_done and shelf_tries < 3:
                shelf_tries += 1
                self.d.click(0.5, 0.5)  # 屏幕中间唤出阅读器菜单
                self.sleep_human(1.0, 1.8)
                # 先检测菜单最上面是「加入书架」还是「已加书架」
                if self.d(text="已加书架").exists(0.8):
                    # 已在书架: 收起菜单, 直接开始阅读
                    self._bookshelf_done = True
                    self.log.info("该书已在书架(已加书架), 直接开始阅读")
                elif self.click_text("加入书架", 1.2):
                    self.log.info("已在阅读器菜单中点击「加入书架」")
                    self._bookshelf_done = True
                    self.sleep_human(0.8, 1.5)
                self.d.click(0.5, 0.5)  # 收起菜单
                self.sleep_human(0.8, 1.5)
                continue
            if self.handle_interruptions(quick=flips % 5 != 0):
                continue
            # 书末卡片页识别: 最新章末卡片页 = 「本章讨论」标题(正文页没有此文本, 书末卡片页
            # 专属) 或 页码 N==M(全书最后一页)。催更按钮下方小字(如「1次」/「2万」)是动态
            # 文本, 实测在「1次」与「2万」之间变化, 不可作为判据。
            # 此页的橙色「催更」/礼物图标是 Compose 绘制, dump 检测不到文本, 需坐标点击。
            # 注意: 页码是全书累计(如 480/480), N==M 只发生在全书最后一页, 不会误判中间章末;
            # 「本章讨论」判据不受工具栏遮挡页码影响(工具栏显示时页码被遮挡)。
            try:
                _end_xml = self.d.dump_hierarchy()
                _end_texts = re.findall(r'text="([^"]*)"', _end_xml)
            except Exception:
                _end_texts = []
            _end_m = re.search(r"(\d+)/(\d+)", " ".join(_end_texts))
            _end_has_discuss = any("本章讨论" in t for t in _end_texts) and not any("去圈子" in t for t in _end_texts)
            # 页码可能被展开的工具栏遮挡(dump 无页码), 书末卡片页/章节末卡片无法确认
            # N==M; 若看到「本章讨论」但无页码, 点屏幕收起工具栏再判一次(点 0.5,0.5 在
            # 书末卡片页中部, 不落在 本章讨论/催更/礼物 按钮区域, 安全)
            if _end_has_discuss and not _end_m:
                try:
                    self.d.click(0.5, 0.5)
                    self.sleep_human(0.8, 1.5)
                    _end_xml = self.d.dump_hierarchy()
                    _end_texts = re.findall(r'text="([^"]*)"', _end_xml)
                    _end_m = re.search(r"(\d+)/(\d+)", " ".join(_end_texts))
                except Exception:
                    pass
            # 只有「页码 N==M(全书最后一页) + 本章讨论」才是全书末卡片页。
            # 注意: 中间章节的章末卡片也有「本章讨论」+ 黄色催更按钮(实测 .107 第1章末
            # 10/490、.108 444/583 都因此被误判完成), 只有 N==M 才触发完成流程。
            # 页码可能被展开的工具栏遮挡, 此时无页码不触发, 交由下方 turn_back 确认逻辑。
            if _end_has_discuss and _end_m and _end_m.group(1) == _end_m.group(2):
                if _end_m:
                    self.log.info("识别到书末卡片页(全书最后一页 %s/%s + 本章讨论), 停止翻页",
                                  _end_m.group(1), _end_m.group(2))
                else:
                    self.log.info("识别到书末卡片页(本章讨论+1次), 停止翻页")
                self._reached_end = True
                if not urged:
                    if bt.urge:
                        if self._click_urge_coord():
                            self._mark_urged(name)
                            urged = True
                            self.rt.set_book(idx, urged=True)
                    else:
                        # 自己的书不能给自己点催更: 到书末即视为完成, 不点催更
                        self.log.info("该书为本人作品(不催更), 到书末视为完成")
                        urged = True
                        self.rt.set_book(idx, urged=True)
                    self.sleep_human(1.0, 2.0)
                # 书末礼物(在书末卡片页直接送)
                if bt.gift and gifts_sent < want_gifts:
                    self.rt.update(step="送礼物")
                    n, exhausted = self._send_free_gifts(want_gifts - gifts_sent)
                    gifts_sent += n
                    if n:
                        self._mark_gifted(name, gifts_sent)
                        self.rt.set_book(idx, gifts=gifts_sent)
                    if exhausted:
                        self.log.info("《%s》免费礼物环节结束(实际送出 %d 次)", name, gifts_sent)
                        self._mark_gifted(name, gifts_sent)
                        gifts_sent = want_gifts
                        self.rt.set_book(idx, gifts=want_gifts)
                # 已催更且礼物满足: 标记完成, 绝不翻页
                if (urged or not bt.urge) and gifts_sent >= want_gifts:
                    set_book_completed(self.cfg.device_serial, name, True)
                    self.log.info("《%s》已读完最后一章并跑完流程, 标记完成", name)
                    self.rt.set_book(idx, status="完成", urged=urged, gifts=gifts_sent, completed=True)
                    if bt.review and not self._reviewed(name):
                        if self._post_book_review(bt) == "ok":
                            self._mark_reviewed(name)
                            self.rt.set_book(idx, reviewed=True, review_status="已发")
                        else:
                            self.rt.set_book(idx, review_status="失败")
                    return "done"
                # 已到书末但流程未走完: 本轮结束, 绝不翻页
                self.log.warning("《%s》已到书末(全书末页卡片)但流程未走完, 本轮结束(下轮继续)", name)
                self.rt.set_book(idx, status="未完成", detail="书末流程未走完")
                return "done"
            # 催更(书末页): 识别到黄色的催更按钮后绝不再往后翻页!
            # (再往右往左滑一下就会离开阅读器退回搜索页, 导致卡点看不到催更)
            if self._urge_button_exists(1.5):
                # 需确认是全书末(N==M)才走完成流程: 中间章节的章末卡片(N<M)也有
                # 黄色催更按钮+「本章讨论」(实测 .107 第1章末 10/490、.108 444/583
                # 都被误判成书末), 章节末只催更(每天一次)后继续翻页读下一章, 不标记完成。
                try:
                    _uxml = self.d.dump_hierarchy()
                    _utexts = re.findall(r'text="([^"]*)"', _uxml)
                    _um = re.search(r"(\d+)/(\d+)", " ".join(_utexts))
                except Exception:
                    _um = None
                if _um and _um.group(1) == _um.group(2):
                    self._reached_end = True  # 全书末页: 出现催更卡片 = 已读到最新章
                    if not urged:
                        if bt.urge:
                            if self._click_urge():
                                self._mark_urged(name)
                                urged = True
                                self.rt.set_book(idx, urged=True)
                        else:
                            # 自己的书不能给自己点催更: 到书末即视为完成, 不点催更
                            self.log.info("该书为本人作品(不催更), 到书末视为完成")
                            urged = True
                            self.rt.set_book(idx, urged=True)
                        self.sleep_human(1.0, 2.0)
                    # 书末礼物(在催更卡片上直接送)
                    if bt.gift and gifts_sent < want_gifts:
                        self.rt.update(step="送礼物")
                        n, exhausted = self._send_free_gifts(want_gifts - gifts_sent)
                        gifts_sent += n
                        if n:
                            self._mark_gifted(name, gifts_sent)
                            self.rt.set_book(idx, gifts=gifts_sent)
                        if exhausted:
                            self.log.info("《%s》免费礼物环节结束(实际送出 %d 次)", name, gifts_sent)
                            self._mark_gifted(name, gifts_sent)
                            gifts_sent = want_gifts
                            self.rt.set_book(idx, gifts=want_gifts)
                    # 已催更且礼物满足: 标记完成, 绝不翻页
                    if (urged or not bt.urge) and gifts_sent >= want_gifts:
                        # 真·读完最后一章并跑完流程 -> 标记完成, 后续轮次跳过
                        set_book_completed(self.cfg.device_serial, name, True)
                        self.log.info("《%s》已读完最后一章并跑完流程, 标记完成", name)
                        self.rt.set_book(idx, status="完成", urged=urged, gifts=gifts_sent, completed=True)
                        if bt.review and not self._reviewed(name):
                            if self._post_book_review(bt) == "ok":
                                self._mark_reviewed(name)
                                self.rt.set_book(idx, reviewed=True, review_status="已发")
                            else:
                                self.rt.set_book(idx, review_status="失败")
                        return "done"
                    # 已到书末但流程未走完(如礼物没送成): 本轮结束, 绝不翻页
                    self.log.warning("《%s》已到书末(催更卡片)但流程未走完, 本轮结束(下轮继续)", name)
                    self.rt.set_book(idx, status="未完成", detail="书末流程未走完")
                    return "done"
                # 章节末卡片(页码 N<M 或页码不可见): 不是全书末, 只催更(每天一次),
                # 不设 _reached_end(避免下方完成判定误判), 继续翻页读下一章
                if not urged and bt.urge:
                    if self._click_urge_coord():
                        self._mark_urged(name)
                        urged = True
                        self.rt.set_book(idx, urged=True)
                    self.sleep_human(1.0, 2.0)
                # 不 return: 落入 fallback(有页码则跳过)后继续翻页
            # 书末 fallback: 新版客户端「催更」按钮可能识别不到(Compose渲染),
            # 且书末页的按钮(催更/发讨论/送礼物)在当前屏可能不显示;
            # 用户指定: 书末页找不到按钮时往前滑一页(turn_back)再回来确认,
            # 绝不能继续往右往左滑(会退出阅读器退回搜索页)
            if not self._reached_end:
                try:
                    _xml = self.d.dump_hierarchy()
                    _texts = re.findall(r'text="([^"]*)"', _xml)
                except Exception:
                    _texts = []
                if not re.search(r"(\d+)/(\d+)", " ".join(_texts)):
                    if self._is_in_reader():
                        # 工具栏展开会遮挡页码(实测书末卡片页 resume 时工具栏展开,
                        # dump 无页码, 点屏幕中部收起后页码才出现), 先收起工具栏再判一次
                        if any(t in _texts for t in ("上一章", "下一章", "目录")):
                            self.d.click(0.5, 0.5)
                            self.sleep_human(0.8, 1.5)
                            try:
                                _xml = self.d.dump_hierarchy()
                                _texts = re.findall(r'text="([^"]*)"', _xml)
                            except Exception:
                                _texts = []
                        if not re.search(r"(\d+)/(\d+)", " ".join(_texts)):
                            if not any(t in _texts for t in ("下一章", "进入下一章", "继续阅读")):
                                if any("去圈子" in t for t in _texts):
                                    # 章节讨论页(帖子列表, 有「去圈子」): 不是书末, back 退回正文继续
                                    self.log.info("检测到章节讨论页(去圈子), 非书末, back 退回正文")
                                    self.d.press("back")
                                    self.sleep_human(1.0, 2.0)
                                    continue
                                # 无页码且无书末卡片特征(本章讨论/写点感悟/发讨论/送礼物/催更)
                                # 的页面是加载过渡页/信息流/异常页, 不是书末, 绝不能确认完成
                                # (实测误判: 章节末卡片翻页的过渡页无页码无下一章, 被当成书末
                                # 标记完成, 导致书其实没读完 resume 停在章节末)
                                if not any(t in _texts for t in
                                           ("本章讨论", "写点感悟", "发讨论", "送礼物", "催更")):
                                    self.log.info("无页码且无书末卡片按钮(过渡页/加载页), 非书末, 继续")
                                    self.sleep_human(1.0, 2.0)
                                    continue
                                if turned_back_at_end:
                                    # 往前滑一页后按钮仍未出现: 确认真在书末(最新章), 停止翻页
                                    self.log.info("书末页: 往前滑一页后仍无页码且无「下一章」, 确认已读到书末(最新章), 停止翻页")
                                    self._reached_end = True
                                    continue
                                # 书末页按钮可能未显示: 往前滑一页(左滑回上一页, 安全方向)再确认
                                turned_back_at_end = True
                                self.log.info("书末页无页码, 往前滑一页重新确认(多翻一页纠正)")
                                self.turn_back()
                                self.sleep_human(1.0, 2.0)
                                continue
                            self.log.info("阅读器内无页码(章节末卡片), 继续翻页")
                        else:
                            # 收起工具栏后页码出现: 正常阅读页, 交给下方翻页逻辑
                            pass
                    else:
                        self.log.warning("未检测到阅读页码且不在阅读器, 交由外层恢复")
                # 注意: turned_back_at_end 一旦置 True 不再重置(避免翻页循环反复往前滑),
                # 直到 run_book 结束; 仅在真正翻回正文继续阅读的路径之外保持
            # 免费礼物
            if bt.gift and gifts_sent < want_gifts and self.d(text=GIFT_ENTRY_TEXT).exists(1.0):
                self.rt.update(step="送礼物")
                n, exhausted = self._send_free_gifts(want_gifts - gifts_sent)
                gifts_sent += n
                if n:
                    self._mark_gifted(name, gifts_sent)
                    self.rt.set_book(idx, gifts=gifts_sent)
                if exhausted:
                    self.log.info("《%s》免费礼物环节结束(实际送出 %d 次)", name, gifts_sent)
                    self._mark_gifted(name, gifts_sent)
                    gifts_sent = want_gifts
                    self.rt.set_book(idx, gifts=want_gifts)
                self.sleep_human(1.0, 2.0)
                continue
            # 完成判定: 必须读到书末(催更卡片识别到) 且 (已催更或自己的书不催更) 且礼物满足。
            # 自己的书(urge=False)也要读完最后一章才完成, 绝不能进书即完成
            # (之前 not bt.urge 恒真导致第一次迭代就静默 done, 整本书根本没读)
            if self._reached_end and (urged or not bt.urge) and gifts_sent >= want_gifts:
                # 真·读完最后一章并跑完流程 -> 标记完成, 后续轮次跳过
                set_book_completed(self.cfg.device_serial, name, True)
                self.log.info("《%s》已读完最后一章并跑完流程, 标记完成", name)
                self.rt.set_book(idx, status="完成", urged=urged, gifts=gifts_sent, completed=True)
                # 看完后自动发一条书评(读者自然语言); 失败不影响完成
                if bt.review and not self._reviewed(name):
                    if self._post_book_review(bt) == "ok":
                        self._mark_reviewed(name)
                        self.rt.set_book(idx, reviewed=True, review_status="已发")
                    else:
                        self.rt.set_book(idx, review_status="失败")
                return "done"
            # 周期性校验仍在正确的书本阅读页(每 5 页一次):
            # 无页码 -> 详情页/广告页, 按返回回到阅读; 页码总数异常 -> 跑错书, 重新搜索进书
            if flips % 5 == 0 and flips > 0:
                try:
                    xml = self.d.dump_hierarchy()
                    texts = re.findall(r'text="([^"]*)"', xml)
                except Exception:
                    texts = []
                if URGE_TEXT in texts:
                    continue  # 书末页催更卡片, 交给上面的催更处理
                m = re.search(r"(\d+)/(\d+)", " ".join(texts))
                if not m:
                    if self._is_in_reader():
                        # 还在阅读器内但没有页码: 章节末/书末卡片页;
                        # 再确认一次催更按钮(书末=最新章, 识别到就停, 绝不能翻页!)
                        if self._urge_button_exists(1.5):
                            self.log.info("书末页: 识别到催更按钮, 停止翻页, 进入完成判定")
                            continue
                        self.log.info("书末页/章节末卡片无页码, 当前文本: %s",
                                      " | ".join(texts[:20]))
                        # 没有催更按钮: 可能是章节末卡片(有「下一章」)可继续翻页;
                        # 也可能是从书末催更卡片多划了一页滑过去了(没有「下一章」),
                        # 此时左滑回上一页纠正, 绝不能继续往右往左滑(会退回搜索页)
                        if not any(t in texts for t in ("下一章", "进入下一章", "继续阅读")):
                            self.turn_back()
                            self.sleep_human(1.0, 2.0)
                            continue
                        # 章节末卡片: 直接继续翻页(绝不按返回, 会把阅读器退出去)
                        self.log.info("阅读器内无页码(章节末/书末卡片), 继续翻页")
                        continue
                    # 不在阅读器内: 绝不盲目按返回(会把阅读器退到搜索页), 交由循环顶部检查+恢复逻辑
                    self.log.warning("未检测到阅读页码且不在阅读器, 交由外层恢复")
                    continue
                total = int(m.group(2))
                if book_total is None:
                    book_total = total
                elif abs(total - book_total) > book_total * 0.15:
                    self.log.warning("页数异常(%d -> %d), 可能跑错书, 重新搜索进书", book_total, total)
                    if self._enter_via_search(name):
                        book_total = None
                        self.sleep_human(1.0, 2.0)
                    else:
                        self.rt.set_book(idx, status="失败", detail="重新进书失败")
                        return "failed"
                    continue
            # 推进: 右侧一页一页翻 (识别到书末催更按钮后绝不再翻页!)
            if self._reached_end:
                self.log.warning("《%s》已到书末(催更卡片)但流程未走完, 本轮结束(下轮继续), 绝不翻页", name)
                self.rt.set_book(idx, status="未完成", detail="书末流程未走完")
                return "done"
            self.rt.update(step="阅读中")
            h0 = self._page_hash()
            self.turn_page()
            flips += 1
            self.pages += 1
            self.rt.update(pages=self.pages)
            self.sleep_human(float(lo), float(hi))
            if flips % 10 == 0:
                self.log.info("《%s》已翻页 %d 次", name, flips)
            # 卡住检测: 翻页后画面没变(弹窗遮挡等) -> 关弹窗后重试点按翻页,
            # 绝不点「下一章」直接跳章
            if h0 and self._page_hash() == h0:
                stuck_taps += 1
                if stuck_taps >= 2:
                    stuck_taps = 0
                    self.log.info("翻页无变化, 关闭弹窗后重试点按(不点下一章)")
                    if self.handle_interruptions(quick=False):
                        continue
                    self.turn_page()
                    self.sleep_human(2.0, 3.0)
                    if h0 and self._page_hash() == h0:
                        if not bt.urge:
                            # 自己的书: 翻到最后一页后画面不再变化 = 已读到最新章
                            self.log.info("本人的书翻页到末尾不再变化, 视为读完")
                            self._reached_end = True
                            continue
                        self.log.warning("重试后画面仍无变化, 该书标记未完成(下轮继续)")
                        self.rt.set_book(idx, status="未完成", detail="翻页卡住")
                        return "done"
            else:
                stuck_taps = 0
        self.log.warning("《%s》翻页达到上限 %d, 本轮结束", name, MAX_FLIP_PER_BOOK)
        self.rt.set_book(idx, status="未完成", detail="翻页达到上限")
        return "done"

    # ---------- 总流程 ----------
    def run(self) -> None:
        if not self.cfg.books:
            self.log.error("config.yaml 未配置任何书籍")
            raise SystemExit(1)
        self.rt.reset_books(self.cfg.books)
        # 恢复持久化的发书讨论状态(重启/重跑后仍显示「已发」)
        for i, b in enumerate(self.cfg.books):
            if b.review and self._reviewed(b.name):
                self.rt.set_book(i, reviewed=True, review_status="已发")
        self.rt.update(running=True, stop_requested=False, started_at=datetime.now().isoformat(timespec="seconds"),
                       finished_at=None, current_book="", step="冷启动应用", pages=0)
        try:
            d = self.d
            try:
                d.screen_on()
                d.unlock()
            except Exception:
                pass
            self.log.info("冷启动番茄免费小说")
            try:
                d.app_stop(APP_PACKAGE)
            except Exception:
                pass
            self.sleep_human(1.0, 2.0)
            d.app_start(APP_PACKAGE)
            self.sleep_human(4.0, 6.0)
            self.handle_interruptions()
            deadline = time.time() + self.cfg.task_timeout_minutes * 60
            done = skipped = failed = stopped = done_skip = waiting = 0
            for idx, bt in enumerate(self.cfg.books):
                if bt.update_time and not update_due(bt.update_time):
                    # 作者更新时间未到: 本轮标记等待, 由调度器到点(更新+1分钟)后单独启动
                    self.log.info("《%s》作者更新时间为 %s, 未到 %s+1分钟, 本轮等待",
                                  bt.name, bt.update_time, bt.update_time)
                    self.rt.set_book(idx, status="等待作者更新", detail=f"{bt.update_time}后1分钟运行")
                    self._mark_update_wait(bt.name)
                    waiting += 1
                    continue
                if bt.completed and not bt.update_time:
                    # 无更新时间且已完成: 跳过(设置更新时间的书每天到点重跑, 读新章节)
                    self.log.info("《%s》已完成(读过最后一章并跑完流程), 本轮跳过", bt.name)
                    self.rt.set_book(idx, status="已完成", detail="已跑完流程")
                    done_skip += 1
                    continue
                if not bt.enabled:
                    self.log.info("《%s》已停用, 跳过", bt.name)
                    self.rt.set_book(idx, status="已停用", detail="开关关闭")
                    stopped += 1
                    continue
                if time.time() >= deadline:
                    self.log.warning("达到总超时, 提前结束")
                    self.rt.set_book(idx, status="未处理", detail="超时")
                    continue
                if bt.update_time:
                    # 作者更新时间已到点: 记录今天已启动(防调度器重复启动)
                    self._mark_started(bt.name)
                result = self.run_book(idx, bt)
                if result == "done":
                    done += 1
                elif result == "skipped":
                    skipped += 1
                else:
                    failed += 1
            self.log.info("===== 全部结束: 完成 %d, 跳过 %d, 失败 %d, 停用 %d, 已完成跳过 %d, 等待作者更新 %d =====",
                          done, skipped, failed, stopped, done_skip, waiting)
            self.rt.update(step="全部完成", current_book="",
                           last_result=f"完成{done} 跳过{skipped} 失败{failed} 停用{stopped} 已完成{done_skip} 等待{waiting}")
        finally:
            self.rt.update(running=False, finished_at=datetime.now().isoformat(timespec="seconds"), stop_requested=False)


def connect_device(serial: str) -> u2.Device:
    """连接设备; 网络设备先确保 adb connect 在线, 失败时给出排查提示。"""
    target = (serial or "").strip()
    if target and (":" in target or re.match(r"^\d+\.\d+\.\d+\.\d+$", target)):
        if ":" not in target:
            target = f"{target}:5555"
        for attempt in range(3):
            try:
                result = adbutils.adb.connect(target)
                print(f"[adb] connect {target} -> {result}")
            except Exception as exc:
                print(f"[adb] connect {target} 失败: {exc}")
            try:
                if any(dev.serial == target for dev in adbutils.adb.list()):
                    break
            except Exception:
                pass
            time.sleep(2)
    try:
        return u2.connect(serial) if serial else u2.connect()
    except Exception as exc:
        sys.exit(
            "无法连接设备: %s\n"
            "请确认: 1) 设备已开启 USB 调试\n"
            "        2) 无线模式需能 ping 通, 并在 config.yaml 填 device.serial" % exc
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="番茄免费小说 多书每日任务")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径(默认 config.yaml)")
    parser.add_argument("--serial", default=None, help="设备序列号或 IP(覆盖配置)")
    parser.add_argument("--timeout", type=int, default=None, help="任务兜底超时分钟数(覆盖配置)")
    args = parser.parse_args()

    cfg = load_config(BASE_DIR / args.config, serial=args.serial)
    if args.timeout is not None:
        cfg.task_timeout_minutes = args.timeout

    bot = FanqieBot(cfg, connect_device(cfg.device_serial))
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.log.info("手动中断")
        return 0
    except SystemExit as exc:
        return int(exc.code or 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
