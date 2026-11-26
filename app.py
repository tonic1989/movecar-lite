#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MoveCar Lite —— 轻量级多用户挪车通知系统
纯 Python 标准库 + SQLite，无 Node、无 Redis、无第三方运行时依赖。
（唯一的第三方库 segno 已 vendor 在 ./vendor，仅用于生成二维码矩阵）

路由：
  GET  /                    扫码呼叫页（需 ?u=车主标识）
  GET  /owner-confirm       车主接单页（?u=）
  GET  /qr                  挪车海报页（?u=）
  GET  /admin               管理后台
  GET  /api/pub/{code}      车主公开信息
  POST /api/challenge       领取呼叫凭证（防骚扰罚站计时起点）
  POST /api/call            发起呼叫（服务端强制校验罚站）
  GET  /api/calls/{code}    呼叫列表
  POST /api/ack             车主标记已处理
  GET  /api/stream/{code}   SSE 状态流
  GET  /api/qr/{code}.png   二维码 PNG
  GET  /api/qr/{code}.json  二维码矩阵
  /api/admin/*              管理接口（Cookie/Bearer 鉴权）
"""
import base64
import hashlib
import html as html_lib
import io
import json
import math
import os
import queue
import re
import secrets
import smtplib
import socket
import socketserver
import sqlite3
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "vendor"))
try:
    import segno
except Exception:  # pragma: no cover
    segno = None

STATIC = os.path.join(BASE, "static")
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "movecar.db")
PORT = int(os.environ.get("PORT", "8000"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# 可选：密码文件（fnOS 的 fpk 包用它持久化后台改的密码；docker 部署不设此变量）
PASSWORD_FILE = os.environ.get("PASSWORD_FILE") or ""
if PASSWORD_FILE and os.path.isfile(PASSWORD_FILE):
    try:
        with open(PASSWORD_FILE, "r", encoding="utf-8") as _pf:
            _saved_pwd = _pf.read().strip()
        if _saved_pwd:
            ADMIN_PASSWORD = _saved_pwd
    except Exception:
        pass
if not ADMIN_PASSWORD:
    # 既没环境变量也没密码文件时，随机生成一个，避免代码里带默认密码
    ADMIN_PASSWORD = secrets.token_urlsafe(9)
    print(
        "WARN 未设置 ADMIN_PASSWORD，本次随机生成管理密码：%s\n"
        "     重启后会变，请设置环境变量 ADMIN_PASSWORD 或 PASSWORD_FILE 持久化。" % ADMIN_PASSWORD,
        flush=True,
    )
TZ_OFFSET = 8 * 3600

# fnOS 统一网关（反代/中继/域名访问都走它）：unix socket + 路径前缀
# 不设这两个变量时行为与以前完全一致（docker 部署不受影响）
GATEWAY_SOCKET = os.environ.get("GATEWAY_SOCKET") or ""
GATEWAY_PREFIX = (os.environ.get("GATEWAY_PREFIX") or "").rstrip("/")

REASONS = ["挡住出口", "占用我的车位", "挡住通道", "车辆剐蹭", "临时停靠请挪车", "其他"]

DEFAULT_SETTINGS = {
    "site_domain": "",          # 外网主域名，https:// 开头，末尾不带斜杠
    "cooldown": "30",           # 拒绝定位后的罚站秒数
    "ratelimit": "60",          # 同一车主同一 IP 最短呼叫间隔
    "title": "智能挪车",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_from": "",            # 自定义发件人：名称 <邮箱>
    "default_wxpusher_token": "",
}

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}

# ---------------------------------------------------------------- 数据库
_db_lock = threading.RLock()
_conn = None


def now():
    return int(time.time())


def db():
    global _conn
    if _conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


def init_db():
    with _db_lock:
        c = db()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS owners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT DEFAULT '',
                plate TEXT DEFAULT '',
                note TEXT DEFAULT '',
                email TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                channels TEXT DEFAULT '[]',
                enabled INTEGER DEFAULT 1,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                reason TEXT DEFAULT '',
                note TEXT DEFAULT '',
                lat REAL, lng REAL, accuracy REAL,
                glat REAL, glng REAL,
                loc_ok INTEGER DEFAULT 0,
                ip TEXT DEFAULT '',
                ua TEXT DEFAULT '',
                push TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                acked_at INTEGER,
                FOREIGN KEY (owner_id) REFERENCES owners(id)
            );
            CREATE INDEX IF NOT EXISTS idx_calls_code ON calls(code, id DESC);
            """
        )
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(k, v) VALUES(?, ?)", (k, v))
        # 增量迁移：老库补字段（SQLite 无 ADD COLUMN IF NOT EXISTS）
        cols = {r["name"] for r in c.execute("PRAGMA table_info(owners)").fetchall()}
        if "phone" not in cols:
            c.execute("ALTER TABLE owners ADD COLUMN phone TEXT DEFAULT ''")
            print("· 迁移：owners 表新增 phone 字段")
        c.commit()


def get_settings():
    with _db_lock:
        rows = db().execute("SELECT k, v FROM settings").fetchall()
    s = dict(DEFAULT_SETTINGS)
    s.update({r["k"]: r["v"] for r in rows})
    return s


def save_settings(patch):
    with _db_lock:
        c = db()
        for k, v in patch.items():
            if k in DEFAULT_SETTINGS:
                c.execute(
                    "INSERT INTO settings(k, v) VALUES(?, ?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (k, "" if v is None else str(v)),
                )
        c.commit()


def owner_row(code):
    with _db_lock:
        r = db().execute("SELECT * FROM owners WHERE code=?", (code,)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["channels"] = json.loads(d.get("channels") or "[]")
    except Exception:
        d["channels"] = []
    return d


def owner_public(o):
    return {
        "code": o["code"],
        "name": o["name"],
        "plate": o["plate"],
        "note": o["note"],
        "enabled": bool(o["enabled"]),
    }


# ---------------------------------------------------------------- 坐标纠偏
def wgs84_to_gcj02(lat, lng):
    """WGS84 → GCJ-02（高德/苹果地图纠偏）"""
    a = 6378245.0
    ee = 0.00669342162296594323

    def out_of_china(lat, lng):
        return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)

    if out_of_china(lat, lng):
        return lat, lng

    def transform_lat(x, y):
        ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
        return ret

    def transform_lng(x, y):
        ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
        return ret

    dlat = transform_lat(lng - 105.0, lat - 35.0)
    dlng = transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lng + dlng


# ---------------------------------------------------------------- SSE 总线
_subs = {}
_subs_lock = threading.Lock()


def subscribe(code):
    q = queue.Queue(maxsize=64)
    with _subs_lock:
        _subs.setdefault(code, set()).add(q)
    return q


def unsubscribe(code, q):
    with _subs_lock:
        s = _subs.get(code)
        if s:
            s.discard(q)
            if not s:
                _subs.pop(code, None)


def broadcast(code, event, data):
    payload = (event, data)
    with _subs_lock:
        targets = list(_subs.get(code, ()))
    for q in targets:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass


# ---------------------------------------------------------------- 推送通道
def _http_post_json(url, body, headers=None, timeout=10):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def handle_url(domains, code):
    return f"{domains}/owner-confirm?u={code}"


def call_lines(o, c):
    lines = [f"【{c['reason'] or '挪车通知'}】", f"车辆：{o['plate'] or o['code']}"]
    # 固定按 UTC+8 格式化，不依赖容器 TZ（否则会出现 +8 再加 8 的时间错乱）
    lines.append("时间：" + time.strftime("%Y-%m-%d %H:%M", time.gmtime(c["created_at"] + TZ_OFFSET)))
    if c.get("lat") is not None:
        lines.append(f"位置：{c['lat']:.6f}, {c['lng']:.6f}")
        lines.append(
            f"导航：https://uri.amap.com/marker?position={c['glng']:.6f},{c['glat']:.6f}"
            "&name=挪车位置&callnative=1"
        )
    else:
        lines.append("位置：对方未授权定位")
    if c.get("note"):
        lines.append(f"留言：{c['note']}")
    return lines


def call_text(o, c, domains):
    """纯文本版（Webhook / 兜底）"""
    return "\n".join(call_lines(o, c) + [f"点击处理挪车请求：{handle_url(domains, o['code'])}"])


def call_html(o, c, domains):
    """富文本版（WxPusher / 邮件）：处理入口是可点击超链接"""
    url = handle_url(domains, o["code"])
    body = "<br>".join(html_lib.escape(x) for x in call_lines(o, c))
    link = (
        f'<a href="{html_lib.escape(url)}" '
        f'style="color:#1f6feb;font-weight:600;text-decoration:underline">点击处理挪车请求</a>'
    )
    tail = f'<br><span style="color:#9aa3ad;font-size:12px;word-break:break-all">{html_lib.escape(url)}</span>'
    return f"{body}<br><br>{link}{tail}"


def call_markdown(o, c, domains):
    """Markdown 版（企业微信 / 钉钉群机器人）：标题加粗、链接可点"""
    url = handle_url(domains, o["code"])
    t = time.strftime("%Y-%m-%d %H:%M", time.gmtime(c["created_at"] + TZ_OFFSET))
    lines = [
        f"**【{c['reason'] or '挪车通知'}】**",
        f"> 车辆：{o['plate'] or o['code']}",
        f"> 时间：{t}",
    ]
    if c.get("lat") is not None:
        nav = (
            f"https://uri.amap.com/marker?position={c['glng']:.6f},{c['glat']:.6f}"
            "&name=挪车位置&callnative=1"
        )
        lines.append(f"> 位置：[{c['lat']:.6f}, {c['lng']:.6f}]({nav})")
    else:
        lines.append("> 位置：对方未授权定位")
    if c.get("note"):
        lines.append(f"> 留言：{c['note']}")
    lines.append("")
    lines.append(f"[点击处理挪车请求]({url})")
    return "\n".join(lines)


def call_news(o, c, domains):
    """图文卡片（企业微信 news）：微信插件里也能正常显示，点卡片直达处理页"""
    t = time.strftime("%Y-%m-%d %H:%M", time.gmtime(c["created_at"] + TZ_OFFSET))
    desc = [f"车辆：{o['plate'] or o['code']}", f"时间：{t}"]
    if c.get("lat") is not None:
        desc.append(f"位置：{c['lat']:.6f}, {c['lng']:.6f}")
    else:
        desc.append("位置：对方未授权定位")
    if c.get("note"):
        desc.append(f"留言：{c['note']}")
    desc.append("点这条消息打开处理页")
    return {
        "title": f"{c['reason'] or '挪车通知'} · {o['plate'] or o['code']}",
        "description": "\n".join(desc),
        "url": handle_url(domains, o["code"]),
        "picurl": "",
    }


def bot_kind(url):
    """识别群机器人地址：企业微信 / 钉钉"""
    u = (url or "").lower()
    if "qyapi.weixin.qq.com" in u and "/webhook/send" in u:
        return "wecom"
    if "oapi.dingtalk.com" in u and "robot/send" in u:
        return "dingtalk"
    return ""


def platform_result(raw):
    """企业微信/钉钉/Server酱 失败时也返回 HTTP 200，必须看错误码，否则会把失败当成功"""
    try:
        r = json.loads(raw)
    except Exception:
        return True, f"200：{raw[:80]}"
    if not isinstance(r, dict):
        return True, f"200：{raw[:80]}"
    code = r.get("errcode")
    if code not in (None, 0, "0"):
        return False, f"{r.get('errmsg') or r}（errcode={code}）"[:200]
    code = r.get("code")
    if code not in (None, 0, 200, "0", "200"):
        return False, f"{r.get('message') or r}（code={code}）"[:200]
    if r.get("success") is False:
        return False, str(r.get("msg") or r)[:200]
    return True, "200：" + (raw[:80] if raw.strip() else "ok")


def push_one(ch, o, c, st, domains):
    """返回 (ok, info)"""
    kind = (ch.get("type") or "").strip()
    text = call_text(o, c, domains)
    rich = call_html(o, c, domains)
    if kind == "wxpusher":
        token = (ch.get("app_token") or st.get("default_wxpusher_token") or "").strip()
        if not token:
            return False, "wxpusher 缺少 appToken"
        target = (ch.get("target") or "").strip()
        body = {
            "appToken": token,
            "content": rich,
            "summary": f"{o['plate'] or o['code']} 请求挪车",
            "contentType": 2,  # 2 = HTML，处理入口是可点击超链接
            # 同时给官方「原文链接」，微信里点消息卡片也能直接进处理页
            "url": handle_url(domains, o["code"]),
        }
        if target.upper().startswith("UID_"):
            body["uids"] = [target]
        elif target.isdigit():
            body["topicIds"] = [int(target)]
        else:
            return False, "wxpusher 目标需 UID_xxx 或 topicId"
        try:
            r = json.loads(_http_post_json("https://wxpusher.zjiecode.com/api/send/message", body))
            return bool(r.get("success")), str(r.get("msg") or r)[:200]
        except Exception as e:
            return False, f"wxpusher 异常 {e.__class__.__name__}: {e}"[:200]
    if kind == "email":
        to = (ch.get("to") or o.get("email") or "").strip()
        if not to:
            return False, "未填收件邮箱"
        host = st.get("smtp_host", "").strip()
        if not host:
            return False, "未配置 SMTP"
        port = int(st.get("smtp_port") or 587)
        user = st.get("smtp_user", "").strip()
        pwd = st.get("smtp_pass", "")
        sender = st.get("smtp_from", "").strip() or user
        m = re.search(r"<([^>]+)>", sender)
        sender_addr = (m.group(1) if m else sender).strip()
        sender_name = sender[: m.start()].strip() if m else ""
        msg = MIMEText(rich, "html", "utf-8")
        msg["Subject"] = Header(f"【挪车】{o['plate'] or o['code']} 请挪车", "utf-8")
        msg["From"] = formataddr((str(Header(sender_name, "utf-8")), sender_addr or user))
        msg["To"] = to
        msg["Date"] = formatdate(localtime=True)
        try:
            if port == 465:
                ctx = ssl.create_default_context()
                srv = smtplib.SMTP_SSL(host, port, timeout=15, context=ctx)
            else:
                srv = smtplib.SMTP(host, port, timeout=15)
                srv.ehlo()
                try:
                    srv.starttls(context=ssl.create_default_context())
                    srv.ehlo()
                except smtplib.SMTPException:
                    pass
            if user and pwd:
                srv.login(user, pwd)
            srv.sendmail(sender_addr or user, [to], msg.as_string())
            srv.quit()
            return True, "邮件已发送"
        except Exception as e:
            return False, f"邮件失败 {e.__class__.__name__}: {e}"[:200]
    if kind in ("webhook", "wecom", "dingtalk"):
        url = (ch.get("url") or "").strip()
        if not url:
            return False, f"{kind} 缺少 url"
        # 企业微信 / 钉钉群机器人有固定报文格式，不能发自定义 JSON
        bot = "wecom" if kind == "wecom" else "dingtalk" if kind == "dingtalk" else bot_kind(url)
        fmt = (ch.get("format") or "auto").strip().lower()
        if bot == "wecom":
            # ⚠️ markdown 消息在「微信插件」（微信里收企业微信消息）中不支持，会提示「暂不支持消息类型」
            # 默认发 news 图文卡片：微信、企业微信都能显示，点卡片直达处理页
            if fmt == "text":
                body = {"msgtype": "text", "text": {"content": text}}
            elif fmt == "markdown":
                body = {"msgtype": "markdown", "markdown": {"content": call_markdown(o, c, domains)}}
            else:
                body = {"msgtype": "news", "news": {"articles": [call_news(o, c, domains)]}}
        elif bot == "dingtalk":
            md = call_markdown(o, c, domains)
            body = {"msgtype": "markdown", "markdown": {"title": "挪车通知", "text": md}}
        else:
            body = {
                "event": "movecar.call",
                "code": o["code"],
                "plate": o["plate"],
                "reason": c["reason"],
                "note": c["note"],
                "lat": c["lat"],
                "lng": c["lng"],
                "glat": c["glat"],
                "glng": c["glng"],
                "time": c["created_at"],
                "text": text,
            }
        try:
            ok, info = platform_result(_http_post_json(url, body))
            # 自动模式：图文卡片被拒（比如机器人不支持 news）时退回纯文本，保证能收到
            if not ok and bot == "wecom" and fmt == "auto":
                ok2, info2 = platform_result(
                    _http_post_json(url, {"msgtype": "text", "text": {"content": text}})
                )
                if ok2:
                    return True, f"wecom 文本兜底成功：{info2}（图文失败：{info}）"[:200]
                return False, f"wecom 图文与文本均失败：{info} / {info2}"[:200]
            return ok, (f"{bot} {info}" if bot else f"webhook {info}")
        except Exception as e:
            return False, f"{bot or 'webhook'} 异常 {e.__class__.__name__}: {e}"[:200]
    return False, f"未知通道 {kind}"


def push_all(o, c, st, domains):
    results = []
    for ch in o.get("channels") or []:
        try:
            ok, info = push_one(ch, o, c, st, domains)
        except Exception as e:
            ok, info = False, f"{e.__class__.__name__}: {e}"
        results.append({"type": ch.get("type"), "ok": ok, "info": info})
    return results


# ---------------------------------------------------------------- 会话
_sessions = {}      # token -> expire_ts
_sess_lock = threading.Lock()
_challenges = {}    # token -> dict
_ch_lock = threading.Lock()
_last_call = {}     # (code, ip) -> ts
_lc_lock = threading.Lock()


def new_session():
    t = secrets.token_urlsafe(24)
    with _sess_lock:
        _sessions[t] = now() + 12 * 3600
    return t


def valid_session(token):
    if not token:
        return False
    with _sess_lock:
        exp = _sessions.get(token)
        if not exp:
            return False
        if exp < now():
            _sessions.pop(token, None)
            return False
    return True


class ApiError(Exception):
    def __init__(self, code, msg, extra=None):
        self.code = code
        self.msg = msg
        self.extra = extra or {}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "MoveCarLite/1.0"
    protocol_version = "HTTP/1.1"

    # ---- helpers
    def log_message(self, fmt, *args):
        sys.stdout.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
        sys.stdout.flush()

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, headers=None):
        self._send(code, obj, headers=headers)

    def _err(self, code, msg, extra=None):
        payload = {"ok": False, "error": msg}
        payload.update(extra or {})
        self._send(code, payload)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        if n > 2 * 1024 * 1024:
            raise ApiError(413, "请求体过大")
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, "JSON 解析失败")

    def _token(self):
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == "mc_admin":
                    return v.strip()
        return ""

    def _require_admin(self):
        if not valid_session(self._token()):
            raise ApiError(401, "未登录或登录已过期")

    def _ip(self):
        xff = self.headers.get("X-Forwarded-For") or ""
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def _domains(self, code):
        st = get_settings()
        d = (st.get("site_domain") or "").strip().rstrip("/")
        if not d:
            proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
            host = self.headers.get("Host") or f"127.0.0.1:{PORT}"
            d = f"{proto}://{host}"
            # 走 fnOS 统一网关时，生成的链接要带 /app/<appname> 前缀
            if GATEWAY_PREFIX and self._via_gateway():
                d += GATEWAY_PREFIX
        return d

    def _via_gateway(self):
        if getattr(self, "_prefix_hit", False):
            return True
        return bool(
            self.headers.get("X-Trim-Userid")
            or self.headers.get("X-Trim-Username")
            or self.headers.get("X-Trim-Isadmin")
        )

    # ---- dispatch
    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("POST")

    def _route(self, method):
        try:
            u = urlparse(self.path)
            path = u.path
            self._prefix_hit = False

            # fnOS 网关可能把 /app/<appname> 原样转进来：剥掉前缀后按普通路径处理
            if GATEWAY_PREFIX and (path == GATEWAY_PREFIX or path.startswith(GATEWAY_PREFIX + "/")):
                path = path[len(GATEWAY_PREFIX):] or "/"
                self._prefix_hit = True

            qs = {k: v[0] for k, v in parse_qs(u.query).items()}

            if path in ("/", "/index.html", "/call"):
                return self._page("call.html")
            if path in ("/owner-confirm", "/owner"):
                return self._page("owner.html")
            if path in ("/qr", "/poster"):
                return self._page("poster.html")
            if path in ("/admin", "/admin/"):
                return self._page("admin.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/favicon.ico":
                return self._send(204, b"")

            if path.startswith("/api/"):
                return self._api(method, path, qs)
            return self._err(404, "页面不存在")
        except ApiError as e:
            self._err(e.code, e.msg, e.extra)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self._err(500, "服务器内部错误")
            except Exception:
                pass

    def _page(self, name):
        fp = os.path.join(STATIC, name)
        if not os.path.isfile(fp):
            return self._err(404, "页面缺失")
        with open(fp, "rb") as f:
            data = f.read()
        # 页面里的资源/接口全部用相对地址；由 <base> 决定落在 / 还是 /app/<appname>/
        base = (GATEWAY_PREFIX + "/") if (GATEWAY_PREFIX and self._via_gateway()) else "/"
        if b"<head>" in data:
            data = data.replace(b"<head>", b'<head>\n<base href="' + base.encode("utf-8") + b'">', 1)
        self._send(200, data, MIME[".html"], {"Cache-Control": "no-cache"})

    def _static(self, rel):
        rel = rel.replace("\\", "/")
        if ".." in rel or rel.startswith("/"):
            return self._err(403, "禁止访问")
        fp = os.path.join(STATIC, rel)
        if not os.path.isfile(fp):
            return self._err(404, "资源不存在")
        ext = os.path.splitext(fp)[1].lower()
        with open(fp, "rb") as f:
            data = f.read()
        # css/js 每次回源校验（文件就几 KB），否则改样式后手机会缓存一整天不生效
        if ext in (".html", ".css", ".js"):
            cache = "no-cache"
        else:
            cache = "public, max-age=86400"
        self._send(200, data, MIME.get(ext, "application/octet-stream"), {"Cache-Control": cache})

    # ---- API 分派
    def _api(self, method, path, qs):
        m = method

        if path == "/api/pub" and m == "GET":
            code = qs.get("u", "")
            return self.api_pub(code)

        if path == "/api/challenge" and m == "POST":
            return self.api_challenge()

        if path == "/api/call" and m == "POST":
            return self.api_call()

        if path == "/api/ack" and m == "POST":
            return self.api_ack()

        m1 = re.match(r"^/api/pub/([^/]+)$", path)
        if m1 and m == "GET":
            return self.api_pub(m1.group(1))

        m2 = re.match(r"^/api/calls/([^/]+)$", path)
        if m2 and m == "GET":
            return self.api_calls(m2.group(1))

        m3 = re.match(r"^/api/stream/([^/]+)$", path)
        if m3 and m == "GET":
            return self.api_stream(m3.group(1))

        m4 = re.match(r"^/api/qr/([^/]+)\.(png|json)$", path)
        if m4 and m == "GET":
            return self.api_qr(m4.group(1), m4.group(2))

        # ---- admin
        if path == "/api/admin/login" and m == "POST":
            return self.api_login()
        if path == "/api/admin/logout" and m == "POST":
            with _sess_lock:
                _sessions.pop(self._token(), None)
            return self._json({"ok": True})
        if path == "/api/admin/state" and m == "GET":
            self._require_admin()
            return self.api_admin_state()
        if path == "/api/admin/settings" and m == "POST":
            self._require_admin()
            body = self._body()
            save_settings(body.get("settings") or {})
            return self._json({"ok": True, "settings": get_settings()})
        if path == "/api/admin/owner" and m == "POST":
            self._require_admin()
            return self.api_admin_owner(self._body())
        if path == "/api/admin/owner/delete" and m == "POST":
            self._require_admin()
            return self.api_admin_delete(self._body())
        if path == "/api/admin/owner/test" and m == "POST":
            self._require_admin()
            return self.api_admin_test(self._body())
        if path == "/api/admin/password" and m == "POST":
            global ADMIN_PASSWORD
            self._require_admin()
            body = self._body()
            pwd = (body.get("password") or "").strip()
            if len(pwd) < 6:
                raise ApiError(400, "密码至少 6 位")
            ADMIN_PASSWORD = pwd
            if PASSWORD_FILE:
                try:
                    with open(PASSWORD_FILE, "w", encoding="utf-8") as _pf:
                        _pf.write(pwd + "\n")
                    try:
                        os.chmod(PASSWORD_FILE, 0o600)
                    except Exception:
                        pass
                    note = "新密码已保存，重启后依然有效"
                except Exception as _e:
                    note = "新密码已生效，但写入密码文件失败：%s" % _e
            else:
                note = "新密码已生效，但未设置密码文件，重启后可能恢复原密码"
            return self._json({"ok": True, "note": note})

        raise ApiError(404, "接口不存在")

    # ---- 公共 API
    def api_pub(self, code):
        if not code:
            raise ApiError(400, "缺少车主标识 u")
        o = owner_row(code)
        if not o:
            raise ApiError(404, "二维码无效：车主不存在")
        if not o["enabled"]:
            raise ApiError(403, "该车主已停用挪车通知")
        st = get_settings()
        d = self._domains(code)
        return self._json(
            {
                "ok": True,
                "owner": owner_public(o),
                "cooldown": int(st.get("cooldown") or 30),
                "reasons": REASONS,
                "domain": d,
            }
        )

    def api_challenge(self):
        body = self._body()
        code = (body.get("code") or "").strip()
        o = owner_row(code)
        if not o:
            raise ApiError(404, "车主不存在")
        loc_ok = bool(body.get("loc_ok"))
        st = get_settings()
        cooldown = int(st.get("cooldown") or 30)
        token = secrets.token_urlsafe(16)
        with _ch_lock:
            _challenges[token] = {
                "code": code,
                "issued_at": now(),
                "loc_ok": loc_ok,
                "ip": self._ip(),
            }
            if len(_challenges) > 5000:
                cutoff = now() - 3600
                for k in [k for k, v in _challenges.items() if v["issued_at"] < cutoff]:
                    _challenges.pop(k, None)
        return self._json(
            {
                "ok": True,
                "challenge": token,
                "issued_at": now(),
                "cooldown": 0 if loc_ok else cooldown,
            }
        )

    def api_call(self):
        body = self._body()
        code = (body.get("code") or "").strip()
        token = (body.get("challenge") or "").strip()
        o = owner_row(code)
        if not o:
            raise ApiError(404, "车主不存在")
        if not o["enabled"]:
            raise ApiError(403, "该车主已停用挪车通知")
        with _ch_lock:
            ch = _challenges.get(token)
            if not ch or ch["code"] != code:
                raise ApiError(400, "呼叫凭证无效，请刷新页面重试")
            _challenges.pop(token, None)
        st = get_settings()
        cooldown = int(st.get("cooldown") or 30)
        elapsed = now() - ch["issued_at"]
        if not ch["loc_ok"] and elapsed < cooldown:
            raise ApiError(
                403,
                "未授权定位，需等待倒计时结束",
                {"retry_after": cooldown - elapsed},
            )

        ip = self._ip()
        rl = int(st.get("ratelimit") or 60)
        key = (code, ip)
        with _lc_lock:
            last = _last_call.get(key, 0)
            if rl > 0 and now() - last < rl:
                raise ApiError(429, f"呼叫过于频繁，请 {rl - (now() - last)} 秒后再试",
                               {"retry_after": rl - (now() - last)})
            _last_call[key] = now()

        loc = body.get("loc") or {}
        lat = lng = acc = None
        glat = glng = None
        if loc.get("lat") is not None and loc.get("lng") is not None:
            try:
                lat = float(loc["lat"])
                lng = float(loc["lng"])
                acc = float(loc.get("accuracy") or 0) or None
                if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                    lat = lng = acc = None
                else:
                    glat, glng = wgs84_to_gcj02(lat, lng)
            except (TypeError, ValueError):
                lat = lng = acc = None

        rec = {
            "code": code,
            "reason": (body.get("reason") or "请挪车")[:40],
            "note": (body.get("note") or "")[:300],
            "lat": lat,
            "lng": lng,
            "glat": glat,
            "glng": glng,
            "accuracy": acc,
            "loc_ok": 1 if ch["loc_ok"] else 0,
            "ip": ip,
            "ua": (self.headers.get("User-Agent") or "")[:200],
            "created_at": now(),
        }
        with _db_lock:
            c = db()
            cur = c.execute(
                "INSERT INTO calls(owner_id, code, reason, note, lat, lng, accuracy, glat, glng,"
                " loc_ok, ip, ua, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (o["id"], code, rec["reason"], rec["note"], lat, lng, acc, glat, glng,
                 rec["loc_ok"], ip, rec["ua"], rec["created_at"]),
            )
            c.commit()
            rec["id"] = cur.lastrowid

        broadcast(code, "call", {"id": rec["id"], "reason": rec["reason"],
                                 "note": rec["note"], "created_at": rec["created_at"],
                                 "lat": lat, "lng": lng, "glat": glat, "glng": glng})

        results = push_all(o, rec, st, self._domains(code))
        info = "; ".join(f"{r['type']}:{'OK' if r['ok'] else 'FAIL-' + r['info']}" for r in results)
        with _db_lock:
            c = db()
            c.execute("UPDATE calls SET push=? WHERE id=?", (info[:500], rec["id"]))
            c.commit()
        success = any(r["ok"] for r in results) or not results
        return self._json(
            {
                "ok": True,
                "id": rec["id"],
                "pushed": success,
                "channels": results,
                "message": "已通知车主" if success else "通知发送失败，请电话联系",
                # 电话只在「已发起呼叫」后才下发，避免扫码即可拿到号码
                "phone": (o.get("phone") or ""),
            }
        )

    def api_calls(self, code):
        o = owner_row(code)
        if not o:
            raise ApiError(404, "车主不存在")
        with _db_lock:
            rows = db().execute(
                "SELECT id, reason, note, lat, lng, glat, glng, accuracy, created_at, acked_at, push"
                " FROM calls WHERE code=? ORDER BY id DESC LIMIT 30",
                (code,),
            ).fetchall()
        items = [dict(r) for r in rows]
        return self._json({"ok": True, "owner": owner_public(o), "calls": items})

    def api_ack(self):
        body = self._body()
        code = (body.get("code") or "").strip()
        try:
            cid = int(body.get("id"))
        except (TypeError, ValueError):
            raise ApiError(400, "缺少呼叫 id")
        with _db_lock:
            c = db()
            row = c.execute("SELECT id, code FROM calls WHERE id=?", (cid,)).fetchone()
            if not row or row["code"] != code:
                raise ApiError(404, "呼叫记录不存在")
            c.execute("UPDATE calls SET acked_at=? WHERE id=?", (now(), cid))
            c.commit()
        broadcast(code, "ack", {"id": cid, "acked_at": now()})
        return self._json({"ok": True, "id": cid})

    def api_stream(self, code):
        """SSE：车主页/呼叫页共用，推送 call / ack 事件"""
        o = owner_row(code)
        if not o:
            raise ApiError(404, "车主不存在")
        q = subscribe(code)
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            last_ping = time.time()
            while True:
                try:
                    ev, data = q.get(timeout=5)
                    chunk = f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    if time.time() - last_ping > 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            unsubscribe(code, q)

    def api_qr(self, code, fmt):
        o = owner_row(code)
        if not o:
            raise ApiError(404, "车主不存在")
        if segno is None:
            raise ApiError(500, "二维码模块缺失")
        base = self._domains(code)
        url = f"{base}/?u={code}"
        qr = segno.make(url, error="m")
        if fmt == "json":
            m = qr.matrix
            return self._json(
                {"ok": True, "url": url, "size": len(m),
                 "modules": [[1 if v else 0 for v in row] for row in m]}
            )
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=10, border=2, dark="#111111", light="#ffffff")
        self._send(200, buf.getvalue(), "image/png", {"Cache-Control": "public, max-age=300"})

    # ---- 管理 API
    def api_login(self):
        body = self._body()
        pwd = (body.get("password") or "").strip()
        if not pwd or not secrets.compare_digest(pwd, ADMIN_PASSWORD):
            raise ApiError(401, "密码错误")
        token = new_session()
        return self._json(
            {"ok": True, "token": token},
            headers={"Set-Cookie": f"mc_admin={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200"},
        )

    def api_admin_state(self):
        st = get_settings()
        with _db_lock:
            c = db()
            owners = [dict(r) for r in c.execute("SELECT * FROM owners ORDER BY id").fetchall()]
            calls = [dict(r) for r in c.execute(
                "SELECT id, code, reason, note, lat, lng, glat, glng, created_at, acked_at, push"
                " FROM calls ORDER BY id DESC LIMIT 50").fetchall()]
            stats = {
                "owners": len(owners),
                "calls_today": c.execute(
                    "SELECT COUNT(*) n FROM calls WHERE created_at>=?",
                    (((now() + TZ_OFFSET) // 86400) * 86400 - TZ_OFFSET,),
                ).fetchone()["n"],
            }
        for o in owners:
            try:
                o["channels"] = json.loads(o.get("channels") or "[]")
            except Exception:
                o["channels"] = []
        # 不回传 SMTP 密码明文
        safe = dict(st)
        if safe.get("smtp_pass"):
            safe["smtp_pass"] = "__KEEP__"
        return self._json({"ok": True, "settings": safe, "owners": owners,
                           "calls": calls, "stats": stats,
                           "reasons": REASONS, "domain": self._domains("")})

    def api_admin_owner(self, body):
        oid = body.get("id")
        code = (body.get("code") or "").strip()
        data = {
            "code": code,
            "name": (body.get("name") or "")[:40],
            "plate": (body.get("plate") or "")[:20],
            "note": (body.get("note") or "")[:120],
            "email": (body.get("email") or "")[:120],
            "phone": re.sub(r"[^\d+]", "", body.get("phone") or "")[:20],
            "channels": json.dumps(body.get("channels") or [], ensure_ascii=False),
            "enabled": 1 if body.get("enabled", True) else 0,
        }
        if not re.match(r"^[A-Za-z0-9_\-]{2,32}$", code):
            raise ApiError(400, "标识只能是 2-32 位字母/数字/下划线/短横线")
        with _db_lock:
            c = db()
            try:
                if oid:
                    c.execute(
                        "UPDATE owners SET code=?,name=?,plate=?,note=?,email=?,phone=?,channels=?,enabled=?"
                        " WHERE id=?",
                        (data["code"], data["name"], data["plate"], data["note"],
                         data["email"], data["phone"], data["channels"], data["enabled"], int(oid)),
                    )
                else:
                    cur = c.execute(
                        "INSERT INTO owners(code,name,plate,note,email,phone,channels,enabled,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (data["code"], data["name"], data["plate"], data["note"],
                         data["email"], data["phone"], data["channels"], data["enabled"], now()),
                    )
                    oid = cur.lastrowid
                c.commit()
            except sqlite3.IntegrityError:
                raise ApiError(400, "标识已存在，请换一个")
        return self._json({"ok": True, "id": oid})

    def api_admin_delete(self, body):
        try:
            oid = int(body.get("id"))
        except (TypeError, ValueError):
            raise ApiError(400, "缺少车主 id")
        with _db_lock:
            c = db()
            c.execute("DELETE FROM owners WHERE id=?", (oid,))
            c.execute("DELETE FROM calls WHERE owner_id=?", (oid,))
            c.commit()
        return self._json({"ok": True})

    def api_admin_test(self, body):
        oid = body.get("id")
        with _db_lock:
            r = db().execute("SELECT * FROM owners WHERE id=?", (int(oid or 0),)).fetchone()
        if not r:
            raise ApiError(404, "车主不存在")
        o = dict(r)
        try:
            o["channels"] = json.loads(o.get("channels") or "[]")
        except Exception:
            o["channels"] = []
        st = get_settings()
        rec = {"reason": "测试通知", "note": "这是一条测试消息，无需处理",
               "lat": None, "lng": None, "glat": None, "glng": None,
               "created_at": now(), "id": 0}
        results = push_all(o, rec, st, self._domains(o["code"]))
        return self._json({"ok": any(x["ok"] for x in results) or not results,
                           "channels": results})


class UnixHTTPServer(ThreadingHTTPServer):
    """监听 Unix socket 的 HTTP 服务，供 fnOS 统一网关（/app/<appname>）转发。"""

    # Windows 版 Python 没有 AF_UNIX，用 getattr 兜底，避免 import 就崩
    address_family = getattr(socket, "AF_UNIX", 1)
    allow_reuse_address = False

    def server_bind(self):
        try:
            os.unlink(self.server_address)
        except OSError:
            pass
        socketserver.TCPServer.server_bind(self)
        self.server_name = "unix"
        self.server_port = 0
        try:
            os.chmod(self.server_address, 0o666)
        except OSError:
            pass

    def get_request(self):
        sock, _addr = self.socket.accept()
        return sock, ("gateway", 0)

    def server_close(self):
        super().server_close()
        try:
            os.unlink(self.server_address)
        except OSError:
            pass


def main():
    init_db()
    os.makedirs(DATA_DIR, exist_ok=True)

    threads = []

    # fnOS 网关 socket：反代 / 中继 / 域名访问都靠它，和端口无关
    if GATEWAY_SOCKET:
        try:
            usrv = UnixHTTPServer(GATEWAY_SOCKET, Handler)
            usrv.daemon_threads = True
            t = threading.Thread(target=usrv.serve_forever, daemon=True)
            t.start()
            threads.append(t)
            print(f"网关 socket 已就绪：{GATEWAY_SOCKET}  前缀={GATEWAY_PREFIX or '(无)'}")
        except Exception as e:
            print(f"⚠️  网关 socket 启动失败（不影响端口访问）：{e}")

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"MoveCar Lite 已启动：http://0.0.0.0:{PORT}  数据目录={DATA_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            srv.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
