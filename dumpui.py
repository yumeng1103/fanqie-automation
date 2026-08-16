# -*- coding: utf-8 -*-
"""Dump 指定设备当前界面所有带文本的节点(含 bounds), 输出到 UTF8 文件避免管道乱码"""
import re
import sys
import uiautomator2 as u2

serial = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:5555"
out = sys.argv[2] if len(sys.argv) > 2 else "dumpui_out.txt"
d = u2.connect(serial)
buf = []


def log(s):
    buf.append(str(s))
    try:
        print(s, flush=True)
    except Exception:
        pass


try:
    cur = d.app_current()
    log("ACTIVITY: %s %s" % (cur.get("package"), cur.get("activity")))
except Exception as exc:
    log("app_current 失败: %s" % exc)
try:
    xml = d.dump_hierarchy()
except Exception as exc:
    log("dump 失败: %s" % exc)
    xml = ""
seen = set()
for m in re.finditer(r'text="([^"]*)"[^>]*?bounds="(\[[^\]]*\]\[[^\]]*\])"', xml):
    t = m.group(1).strip()
    if t and t not in seen:
        seen.add(t)
        log("T: %s @ %s" % (t, m.group(2)))
for m in re.finditer(r'content-desc="([^"]*)"[^>]*?bounds="(\[[^\]]*\]\[[^\]]*\])"', xml):
    t = m.group(1).strip()
    if t and t not in seen:
        seen.add(t)
        log("[desc] %s @ %s" % (t, m.group(2)))
log("TOTAL: %d" % len(seen))
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(buf))
log("已写入 %s" % out)
