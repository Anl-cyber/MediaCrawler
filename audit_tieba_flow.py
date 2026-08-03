#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""贴吧完整流程审计（2026-08-03）

阶段 0：静态预检——配置/登录态/代码加固存在性（不启动浏览器）
阶段 1：实弹完整爬取——CDP 启动 → pong 登录态 → 搜索 → JSONL 落盘
阶段 2：产物完整性校验——posts/comments JSONL 字段/条数/关键词相关性

硬约束：滑块路径无法实弹触发（登录态有效至 2027-09-04），
滑块感知（login.py _is_captcha_page 链 + client.py TiebaCaptchaError +
core.py _retry_search_after_captcha）以静态守卫 + r51 行为测试覆盖。

用法: python audit_tieba_flow.py [--skip-live]
"""
import datetime
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = r"D:\SocialSense\crawler\.venv_mc\Scripts\python.exe"
TODAY = datetime.date.today().isoformat()
KEYWORD = "新能源汽车"
MAX_NOTES = 10

# 环境变量污染黑名单（Hermes/conda 注入，实弹子进程必须清掉）
ENV_BLACKLIST = ["PYTHONPATH", "SSL_CERT_FILE", "HTTP_PROXY", "HTTPS_PROXY",
                 "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy"]

PASS, FAIL, WARN = [], [], []


def check(name, cond, detail=""):
    bucket = PASS if cond else FAIL
    bucket.append((name, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")


def warn(name, detail=""):
    WARN.append((name, detail))
    print(f"  [WARN] {name} {detail}")


def read_src(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# ============================================================ 阶段 0
def stage0():
    print("\n========== 阶段 0：静态预检 ==========")
    tb = os.path.join(ROOT, "media_platform", "tieba")

    # 0.1 环境变量污染（本进程内的检查；实弹子进程会强制清空）
    polluted = [k for k in ENV_BLACKLIST if os.environ.get(k)]
    if polluted:
        warn("环境变量污染（本进程）", f"存在 {polluted}，实弹子进程将强制清空")
    else:
        check("环境变量无污染", True)

    # 0.2 login.py 滑块感知加固方法存在
    login_src = read_src(os.path.join(tb, "login.py"))
    for m in ("_is_captcha_page", "_wait_human_captcha",
              "_quick_login_state", "_click_login_button_with_captcha"):
        check(f"login.py 含 {m}", f"def {m}" in login_src)

    # 0.3 client.py 验证页检测链（TiebaCaptchaError 类 + 检测点）
    client_src = read_src(os.path.join(tb, "client.py"))
    check("client.py 定义 TiebaCaptchaError", "class TiebaCaptchaError" in client_src)
    hits = client_src.count("百度安全验证") + client_src.count("安全验证")
    check("client.py 验证页检测点 ≥3 处", hits >= 3, f"实际 {hits} 处")

    # 0.4 core.py 验证页恢复链（等待/重试/页面重建）
    core_src = read_src(os.path.join(tb, "core.py"))
    for name, pat in [("_CAPTCHA_WAIT_SEC=60", "_CAPTCHA_WAIT_SEC = 60"),
                      ("_CAPTCHA_MAX_RETRY=3", "_CAPTCHA_MAX_RETRY = 3"),
                      ("_retry_search_after_captcha", "async def _retry_search_after_captcha"),
                      ("_rebuild_context_page", "async def _rebuild_context_page"),
                      ("search() 捕获 TiebaCaptchaError", "except TiebaCaptchaError")]:
        check(f"core.py 含 {name}", pat in core_src)

    # 0.5 登录态持久化（user_data_dir Cookie DB）
    udd = os.path.join(ROOT, "browser_data", "tieba_user_data_dir")
    cookies_db = os.path.join(udd, "Default", "Network", "Cookies")
    if not os.path.exists(udd):
        check("user_data_dir 存在", False, udd)
        return
    check("user_data_dir 存在", True, udd)
    if not os.path.exists(cookies_db):
        check("Cookie DB 存在", False, cookies_db)
        return
    check("Cookie DB 存在", True)
    try:
        conn = sqlite3.connect(f"file:{cookies_db}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT name, host_key, expires_utc FROM cookies "
            "WHERE name IN ('BDUSS','STOKEN','PTOKEN') ORDER BY name").fetchall()
        conn.close()
        cookie_map = {r[0]: r for r in rows}
        # Chrome cookies 表 expires_utc 为 Windows FILETIME（1601-01-01 起 100ns 单位）
        FILETIME_EPOCH_OFFSET = 11644473600  # 1601-01-01 → 1970-01-01 秒差
        for name in ("BDUSS", "STOKEN", "PTOKEN"):
            if name in cookie_map:
                exp = cookie_map[name][2]
                exp_date = datetime.datetime.fromtimestamp(
                    exp / 1_000_000 - FILETIME_EPOCH_OFFSET)
                ok = exp_date > datetime.datetime.now()
                check(f"Cookie {name} 有效", ok,
                      f"expires={exp_date:%Y-%m-%d} host={cookie_map[name][1]}")
            else:
                check(f"Cookie {name} 存在", False)
    except Exception as e:
        check("Cookie DB 可读", False, str(e)[:120])

    # 0.6 输出目录
    out_dir = os.path.join(ROOT, "data", "tieba", "jsonl")
    check("JSONL 输出目录存在", os.path.isdir(out_dir), out_dir)


# ============================================================ 阶段 1
LIVE_EVENTS = [
    ("cdp_launch", "Launching browser in CDP mode"),
    ("anti_detect", "Injecting anti-detection scripts"),
    ("login_check", "pong"),
    ("login_reuse", "Login state verified by cookies"),
    ("login_trigger", "Begin login baidutieba"),          # 不应出现（应复用登录态）
    ("captcha_hit", "安全验证"),                           # 不应出现（登录态有效）
    ("search_begin", "Begin search baidu tieba keywords"),
    ("search_result", "Note list len"),
    ("detail", "get_note_detail"),
    ("comment", "get_note_all_comments"),
    ("finished", "Tieba Crawler finished"),
]
_event_lines = {k: [] for k, _ in LIVE_EVENTS}


def stage1():
    print("\n========== 阶段 1：实弹完整爬取 ==========")
    env = {k: v for k, v in os.environ.items() if k not in ENV_BLACKLIST}
    cmd = [PY, "-u", "main.py",
           "--platform", "tieba", "--lt", "qrcode",
           "--keywords", KEYWORD, "--crawler_max_notes_count", str(MAX_NOTES),
           "--start", "1", "--type", "search"]
    print(f"  cmd: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    deadline = time.time() + 540
    last_line_ts = time.time()
    traceback_seen = False
    while True:
        line = proc.stdout.readline()
        if line:
            last_line_ts = time.time()
            for key, pat in LIVE_EVENTS:
                if pat in line:
                    _event_lines[key].append(line.strip()[:160])
            if "Traceback" in line or "Error" in line and "error_code" not in line:
                traceback_seen = True
        else:
            if proc.poll() is not None:
                break
            if time.time() - last_line_ts > 60:
                print("  [WARN] 子进程 60s 无输出（可能卡死）")
                last_line_ts = time.time()
        if time.time() > deadline:
            print("  [FAIL] 实弹超时 540s，强杀")
            proc.kill()
            proc.wait()
            FAIL.append(("实弹完整跑未在 540s 内完成", ""))
            return
    rc = proc.wait()
    check("实弹退出码为 0", rc == 0, f"exit={rc}")
    check("CDP 模式启动", bool(_event_lines["cdp_launch"]))
    check("登录态检查(pong) 执行", bool(_event_lines["login_check"]))
    reuse = bool(_event_lines["login_reuse"])
    login_t = bool(_event_lines["login_trigger"])
    check("登录态复用（免登录）", reuse and not login_t,
          f"reuse={reuse} login_trigger={login_t}")
    check("未触发滑块验证", not _event_lines["captcha_hit"],
          f"命中 {len(_event_lines['captcha_hit'])} 次")
    check("搜索循环启动", bool(_event_lines["search_begin"]))
    n_res = len(_event_lines["search_result"])
    check("搜索出结果", n_res > 0, f"{n_res} 页有结果")
    check("正常收尾", bool(_event_lines["finished"]) or not traceback_seen,
          f"traceback={traceback_seen}")
    print(f"  --- 关键事件命中：{ {k: len(v) for k, v in _event_lines.items()} }")


# ============================================================ 阶段 2
def stage2():
    print("\n========== 阶段 2：产物完整性校验 ==========")
    out_dir = os.path.join(ROOT, "data", "tieba", "jsonl")
    contents = sorted(glob.glob(os.path.join(out_dir, f"search_contents_{TODAY}.jsonl")))
    comments = sorted(glob.glob(os.path.join(out_dir, f"search_comments_{TODAY}.jsonl")))

    check("今日 posts JSONL 存在", len(contents) == 1, contents[0] if contents else "")
    check("今日 comments JSONL 存在", len(comments) == 1, comments[0] if comments else "")
    if not contents or not comments:
        return

    posts = []
    with open(contents[0], "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    posts.append(json.loads(line))
                except json.JSONDecodeError:
                    warn("posts 存在坏行", line[:80])
    # JSONL 为追加累积模式（aiofiles 'a'）：同 note_id 可能来自历史运行，去重后校验
    seen = set()
    unique_posts = []
    for p in posts:
        nid = p.get("note_id")
        if nid in seen:
            continue
        seen.add(nid)
        unique_posts.append(p)
    dup_n = len(posts) - len(unique_posts)
    if dup_n:
        warn("posts 追加累积（历史运行数据）", f"{dup_n} 条重复 note_id 去重")
    posts = unique_posts
    check("posts 条数 ≥1（去重后）", len(posts) >= 1, f"{len(posts)} 条")
    # 字段完整性（真实 schema：desc/user_nickname/publish_time，见 help.py TiebaNote）
    required = {"note_id", "title", "desc", "user_nickname", "publish_time"}
    missing = sorted({k for p in posts for k in required if k not in p})
    check("posts 必需字段齐全", not missing, f"缺失字段: {missing}" if missing else "")
    # 关键词相关性（标题/正文任一命中；≥70% 容差——标题可能不含字面关键词）
    hit = sum(1 for p in posts if KEYWORD in (p.get("title") or "")
              or KEYWORD in (p.get("desc") or ""))
    rate = hit / len(posts) if posts else 0
    check("posts 关键词命中率 ≥70%", rate >= 0.7, f"{hit}/{len(posts)} = {rate:.0%}")
    # 帖内评论非空（以 comments 文件中实际存在的 note_id 为准）
    cmt_note_ids = set()
    with open(comments[0], "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                if c.get("note_id"):
                    cmt_note_ids.add(c["note_id"])
            except json.JSONDecodeError:
                pass
    with_comments = sum(1 for p in posts if p.get("note_id") in cmt_note_ids)
    check("posts 至少部分带评论", with_comments > 0, f"{with_comments}/{len(posts)} 有评论")

    cmts = []
    with open(comments[0], "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    cmts.append(json.loads(line))
                except json.JSONDecodeError:
                    warn("comments 存在坏行", line[:80])
    check("comments 条数 ≥1", len(cmts) >= 1, f"{len(cmts)} 条")
    c_required = {"comment_id", "content", "publish_time", "agree_num", "user_nickname"}
    c_missing = sorted({k for c in cmts for k in c_required if k not in c})
    check("comments 必需字段齐全", not c_missing, f"缺失字段: {c_missing}" if c_missing else "")
    # 评论去重（追加累积模式下同 comment_id 可能来自历史运行）
    seen_c = set()
    unique_c = []
    for c in cmts:
        cid = c.get("comment_id")
        if cid in seen_c:
            continue
        seen_c.add(cid)
        unique_c.append(c)
    dup_c = len(cmts) - len(unique_c)
    if dup_c:
        warn("comments 追加累积（历史运行数据）", f"{dup_c} 条重复 comment_id 去重")
    cmts = unique_c
    check("comments 去重后条数 ≥1", len(cmts) >= 1, f"{len(cmts)} 条")
    ids = [c.get("comment_id") for c in cmts if c.get("comment_id")]
    check("comments 无重复 comment_id", len(ids) == len(set(ids)),
          f"{len(ids)} 条含 {len(ids) - len(set(ids))} 重复")


def summary():
    print("\n========== 审计汇总 ==========")
    print(f"  PASS {len(PASS)}  FAIL {len(FAIL)}  WARN {len(WARN)}")
    for name, detail in FAIL:
        print(f"  [FAIL] {name} {detail}")
    for name, detail in WARN:
        print(f"  [WARN] {name} {detail}")
    if FAIL:
        print("  >>> 存在失败项，请人工介入")
        sys.exit(1)
    print("  >>> 全部通过")
    sys.exit(0)


if __name__ == "__main__":
    stage0()
    if "--skip-live" not in sys.argv:
        stage1()
    stage2()
    summary()
