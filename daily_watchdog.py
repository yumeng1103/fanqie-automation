# -*- coding: utf-8 -*-
"""每日任务看门狗: 每天 00:01 后自动执行一次 fanqie_reader.py.

配合「启动」文件夹里的 fanqie_watchdog.bat 开机自启, 常驻后台。
每天只跑一次(按日期记录 last_run.txt), 机器睡了/晚了会在恢复后补跑当天任务。

用法:
    python daily_watchdog.py        # 常驻循环
    python daily_watchdog.py --now  # 立即执行一次后退出(测试用)
"""

from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PYTHON = BASE_DIR / ".venv" / "Scripts" / "python.exe"
TASK_SCRIPT = BASE_DIR / "fanqie_reader.py"
LOG_FILE = BASE_DIR / "watchdog.log"
LAST_RUN_FILE = BASE_DIR / "last_run.txt"
RUN_TIMEOUT_SECONDS = 45 * 60  # 单次任务最长 45 分钟
POLL_SECONDS = 30


def log(msg: str) -> None:
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def last_run_date() -> str:
    try:
        return LAST_RUN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def mark_run(date_str: str) -> None:
    try:
        LAST_RUN_FILE.write_text(date_str, encoding="utf-8")
    except OSError as exc:
        log(f"记录运行日期失败: {exc}")


def run_task_once() -> None:
    log("===== 开始执行每日任务 =====")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run(
            [str(PYTHON), str(TASK_SCRIPT)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUN_TIMEOUT_SECONDS,
            env=env,
        )
        out = (proc.stdout or "")[-3000:]
        err = (proc.stderr or "")[-1500:]
        log(f"任务退出码: {proc.returncode}")
        if out:
            log(f"--- 输出 ---\n{out}")
        if err:
            log(f"--- stderr ---\n{err}")
    except subprocess.TimeoutExpired:
        log("任务超时被杀(45 分钟)")
    except Exception as exc:
        log(f"任务执行异常: {exc!r}")
    log("===== 每日任务结束 =====")


def main() -> int:
    parser = argparse.ArgumentParser(description="番茄每日任务看门狗")
    parser.add_argument("--now", action="store_true", help="立即执行一次后退出")
    args = parser.parse_args()

    if args.now:
        run_task_once()
        return 0

    log(f"看门狗启动 (pid {__import__('os').getpid()}), 每天 00:01 后执行一次")
    while True:
        try:
            now = datetime.datetime.now()
            today = now.date().isoformat()
            minutes_of_day = now.hour * 60 + now.minute
            if minutes_of_day >= 1 and last_run_date() != today:
                run_task_once()
                mark_run(today)
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            log("看门狗停止")
            return 0
        except Exception as exc:
            log(f"看门狗异常(10 秒后重试): {exc!r}")
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
