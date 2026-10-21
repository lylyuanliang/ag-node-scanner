#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clash 节点体检器 / ClashCheckup

真实性说明（重要）：
  * 延迟 = 由临时 mihomo 内核实测（Clash 内核自己的 delay 测试），不是估算。
  * 速率 = 真实下载 512 KB / 耗时，不是按 ASN 猜的。
  * 出口 IP = 真实穿透该节点后看到的公网 IP。
  * 本工具不使用任何 Google 账号凭据，因此无法判定 Google 的「地区 400」——
    该校验发生在鉴权之后。工具只测「这个节点通不通、多快、出口在哪」。

为什么不影响你正在跑的 Clash：
  启动一个独立的临时 mihomo 实例（另开端口、另写临时配置），
  节点列表从 Clash Verge 运行态配置复制，测完即杀进程并删除临时目录。
"""

import argparse
import atexit
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APPNAME = "Clash 节点体检器"
VERSION = "1.0.0"

VERGE_DIR = os.path.join(os.environ.get("APPDATA", ""), "io.github.clash-verge-rev.clash-verge-rev")
RUNTIME_YAML = os.path.join(VERGE_DIR, "clash-verge.yaml")

DELAY_URL = "http://www.gstatic.com/generate_204"
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=524288"
EXITIP_URL = "https://v4.mypublicipnow.com"
GOOGLE_PROBE = "https://daily-cloudcode-pa.googleapis.com/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) clash-checkup/" + VERSION

MIHOMO_CANDIDATES = [
    r"D:\soft\Clash Verge\verge-mihomo.exe",
    os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Clash Verge", "verge-mihomo.exe"),
    os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Clash Verge", "verge-mihomo.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\clash-verge\verge-mihomo.exe"),
]

STATE = {"running": False, "mode": None, "rows": [], "total": 0, "done": 0,
         "message": "空闲", "finished": False, "started_at": 0, "core": "",
         "phase": "", "phase_done": 0, "phase_total": 0, "cur": "", "cur_step": 0,
         "ok": 0, "fail": 0, "static_done": 0, "static_total": 0}
STATE_LOCK = threading.Lock()
STOP_FLAG = threading.Event()
RUN_ID = [0]


def set_state(**kw):
    with STATE_LOCK:
        STATE.update(kw)


def is_ok(r):
    """有没有拿到任何真实信号。全空 = 这个节点连不上或没测到。"""
    return bool(r.get("latency_ms") is not None or r.get("exit_ip") or r.get("server_ip")
                or r.get("speed_kbps") is not None or r.get("google_ok") is not None
                or r.get("asn"))


def update_row(name, **fields):
    """按名字就地更新已有行，供后台线程回填用。"""
    with STATE_LOCK:
        for r in STATE["rows"]:
            if r.get("name") == name:
                r.update(fields)
                r["ok"] = is_ok(r)
                r["score"] = score_row(r)
                r["verdict"] = verdict(r)
                return


def push_row(row):
    """同一节点重复推送时覆盖旧行，进度由调用方控制。"""
    row["ok"] = is_ok(row)
    row["score"] = score_row(row)
    row["verdict"] = verdict(row)
    with STATE_LOCK:
        for i, old in enumerate(STATE["rows"]):
            if old.get("name") == row["name"]:
                STATE["rows"][i] = row
                return
        STATE["rows"].append(row)


# ---------------------------------------------------------------- 配置

def parse_config():
    if not os.path.exists(RUNTIME_YAML):
        raise RuntimeError("找不到 Clash Verge 运行态配置：%s" % RUNTIME_YAML)
    with open(RUNTIME_YAML, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.read().splitlines()

    try:
        p_start = next(i for i, l in enumerate(lines) if l.startswith("proxies:"))
        p_end = next(i for i, l in enumerate(lines) if l.startswith("proxy-groups:"))
    except StopIteration:
        raise RuntimeError("运行态配置缺少 proxies / proxy-groups 段")

    nodes, cur = [], None
    for raw in lines[p_start + 1:p_end]:
        if not raw.strip():
            continue
        s, indent = raw.strip(), len(raw) - len(raw.lstrip())
        if s.startswith("- name:"):
            if cur:
                nodes.append(cur)
            cur = {"name": s.split(":", 1)[1].strip().strip("'\""), "server": "", "type": "", "port": 0}
        elif cur is not None and indent == 2 and ":" in s:
            k, v = s.split(":", 1)
            k, v = k.strip(), v.strip().strip("'\"")
            if k == "server" and v:
                cur["server"] = v
            elif k == "type" and v:
                cur["type"] = v
            elif k == "port" and v.isdigit():
                cur["port"] = int(v)
    if cur:
        nodes.append(cur)

    SKIP = ("剩余流量", "距离下次重置", "套餐到期", "如果很少节点可用", "建议每天更新", "官网")
    nodes = [n for n in nodes if n["name"] and n["server"]
             and not n["server"].startswith("127.")
             and not any(w in n["name"] for w in SKIP)]

    return {"nodes": nodes, "proxies_block": lines[p_start:p_end],
            "config_path": RUNTIME_YAML,
            "config_mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(RUNTIME_YAML)))}


def traffic_note(name):
    if "5倍率" in name:
        return "5 倍率"
    if "2倍率" in name:
        return "2 倍率"
    if "勿跑大流量" in name:
        return "限流量"
    return ""


# ---------------------------------------------------------------- HTTP

try:
    import certifi as _certifi
    _CAFILE = _certifi.where()
except Exception:
    _CAFILE = None


def _ssl_context():
    import ssl
    try:
        return ssl.create_default_context(cafile=_CAFILE) if _CAFILE else ssl.create_default_context()
    except Exception:
        return ssl.create_default_context()


def http_get(url, proxy=None, timeout=15, max_bytes=800000, deadline=None):
    """deadline：整个请求的墙钟上限（秒）。到点就断，返回已读到的字节。"""
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}),
                urllib.request.HTTPSHandler(context=_ssl_context())]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.time()
    cutoff = (t0 + deadline) if deadline else None
    try:
        with opener.open(req, timeout=timeout) as resp:
            if not cutoff:
                body = resp.read(max_bytes)
            else:
                chunks, got = [], 0
                ttfb = None
                while got < max_bytes:
                    if time.time() > cutoff:
                        break
                    try:
                        b = resp.read(min(32768, max_bytes - got))
                    except Exception:
                        break
                    if not b:
                        break
                    if ttfb is None:
                        ttfb = time.time()
                    chunks.append(b)
                    got += len(b)
                body = b"".join(chunks)
            return {"ok": True, "status": resp.status, "body": body,
                    "ttfb": (ttfb - t0) if (cutoff and ttfb) else None,
                    "elapsed_ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        return {"ok": True, "status": e.code, "body": b"",
                "elapsed_ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "status": 0, "body": b"", "error": str(e),
                "elapsed_ms": int((time.time() - t0) * 1000)}


def doh_resolve(host, timeout=8):
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host or ""):
        return host
    r = http_get("https://dns.alidns.com/resolve?name=%s&type=A" % urllib.parse.quote(host), timeout=timeout)
    if not r["ok"]:
        return ""
    try:
        for ans in json.loads(r["body"].decode("utf-8", "ignore")).get("Answer", []):
            if ans.get("type") == 1:
                return ans.get("data", "")
    except Exception:
        return ""
    return ""


IPINFO_CACHE, IPINFO_LOCK, IPINFO_LAST = {}, threading.Lock(), [0.0]


def lookup_ip(ip):
    if not ip:
        return {}
    with IPINFO_LOCK:
        if ip in IPINFO_CACHE:
            return IPINFO_CACHE[ip]
        wait = 1.05 - (time.time() - IPINFO_LAST[0])
        if wait > 0:
            time.sleep(wait)
        IPINFO_LAST[0] = time.time()
    r = http_get("http://ip-api.com/json/%s?fields=status,country,regionName,city,isp,org,as,"
                 "hosting,proxy,mobile" % ip, timeout=10)
    info = {}
    if r["ok"] and r["body"]:
        try:
            info = json.loads(r["body"].decode("utf-8", "ignore"))
        except Exception:
            info = {}
    with IPINFO_LOCK:
        IPINFO_CACHE[ip] = info
    return info


# ---------------------------------------------------------------- 纯净度（参考 ippure 的维度）

SCAM_CACHE, SCAM_LOCK, SCAM_LAST = {}, threading.Lock(), [0.0]


def scam_lookup(ip):
    """抓 scamalytics 的欺诈分与代理历史。免费、无需 key。"""
    if not ip:
        return {}
    with SCAM_LOCK:
        if ip in SCAM_CACHE:
            return SCAM_CACHE[ip]
        wait = 1.2 - (time.time() - SCAM_LAST[0])
        if wait > 0:
            time.sleep(wait)
        SCAM_LAST[0] = time.time()
    res = {}
    r = http_get("https://scamalytics.com/ip/" + ip, timeout=20, max_bytes=300000)
    if r["ok"] and r["status"] == 200:
        html = r["body"].decode("utf-8", "ignore")
        m = re.search(r"Fraud Score:\s*(\d+)", html)
        if m:
            res["fraud"] = int(m.group(1))
        m2 = re.search(r"Last Proxy Provider\s*</th>\s*<td[^>]*>\s*([^<]{1,48})", html)
        if m2 and m2.group(1).strip() and m2.group(1).strip().lower() != "n/a":
            res["last_proxy"] = m2.group(1).strip()
        m3 = re.search(r'<div class="risk[^"]*\b(risk-none|no-risk|low|medium|high)', html)
        if m3:
            res["risk_bar"] = m3.group(1)
    with SCAM_LOCK:
        SCAM_CACHE[ip] = res
    return res


def purity_of(info, scam):
    """0-100，越高越干净。维度对齐 ippure：欺诈分、机房属性、代理标记、代理历史。"""
    fraud = scam.get("fraud")
    if fraud is None:
        return None, ""
    p = 100.0 - fraud * 0.85
    tags = []
    if info.get("hosting"):
        p -= 15
        tags.append("机房")
    if info.get("proxy"):
        p -= 15
        tags.append("代理标记")
    if info.get("mobile"):
        tags.append("移动网络")
    lp = scam.get("last_proxy")
    if lp:
        p -= 8
        tags.append(lp[:18])
    return max(0, min(100, int(round(p)))), ",".join(tags)


# ---------------------------------------------------------------- 临时内核

def find_mihomo():
    for p in MIHOMO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for key in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Clash Verge",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Clash Verge"):
                try:
                    k = winreg.OpenKey(root, key)
                    loc, _ = winreg.QueryValueEx(k, "InstallLocation")
                    cand = os.path.join(str(loc).strip('"'), "verge-mihomo.exe")
                    if os.path.exists(cand):
                        return cand
                except Exception:
                    continue
    except Exception:
        pass
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("PROGRAMFILES", "")):
        if not base or not os.path.isdir(base):
            continue
        try:
            for name in os.listdir(base):
                d = os.path.join(base, name)
                if os.path.isdir(d) and ("clash" in name.lower() or "verge" in name.lower()):
                    cand = os.path.join(d, "verge-mihomo.exe")
                    if os.path.exists(cand):
                        return cand
        except Exception:
            pass
    return ""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Core:
    """独立临时 mihomo 实例，与机器上正在运行的 Clash 完全隔离。"""

    GROUP = "AG-SELECT"

    def __init__(self, proxies_block, names):
        self.bin = find_mihomo()
        if not self.bin:
            raise RuntimeError("找不到 verge-mihomo.exe（Clash Verge 的内核）")
        self.dir = tempfile.mkdtemp(prefix="agscan-")
        self.api_port = free_port()
        self.mixed_port = free_port()
        for _ in range(20):                     # 两个端口绝不能撞号
            if self.mixed_port != self.api_port:
                break
            self.mixed_port = free_port()
        self.proc = None
        cfg = ["mixed-port: %d" % self.mixed_port, "allow-lan: false", "mode: rule",
               "log-level: silent", "ipv6: false",
               "external-controller: 127.0.0.1:%d" % self.api_port, "secret: ''", ""]
        cfg += list(proxies_block)
        cfg += ["proxy-groups:", "  - name: %s" % self.GROUP, "    type: select", "    proxies:"]
        cfg += ['      - "%s"' % n.replace('"', "") for n in names]
        cfg += ["rules:", "  - MATCH,%s" % self.GROUP, ""]
        self.cfg_path = os.path.join(self.dir, "config.yaml")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write("\n".join(cfg))

    def start(self):
        flags = 0x08000000 if os.name == "nt" else 0
        self.proc = subprocess.Popen([self.bin, "-d", self.dir, "-f", self.cfg_path],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=flags)
        for _ in range(80):
            try:
                self.api("/version")
                break
            except Exception:
                time.sleep(0.25)
        else:
            self.stop()
            raise RuntimeError("临时内核启动超时")
        # 确认混合端口真的在监听，否则后面每个节点都会连接被拒
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.mixed_port), timeout=0.3).close()
                return True
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("临时内核混合端口 %d 未监听" % self.mixed_port)

    def api(self, path, method="GET", data=None, timeout=10):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.api_port, path),
                                     data=json.dumps(data).encode() if data is not None else None,
                                     method=method)
        req.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")

    def delay(self, name, timeout_ms=5000):
        q = urllib.parse.urlencode({"timeout": timeout_ms, "url": DELAY_URL})
        try:
            d = json.loads(self.api("/proxies/%s/delay?%s" % (urllib.parse.quote(name, safe=""), q),
                                    timeout=timeout_ms / 1000 + 6))
            return d.get("delay")
        except Exception:
            return None

    def select(self, name):
        self.api("/proxies/%s" % urllib.parse.quote(self.GROUP, safe=""), "PUT", {"name": name})

    def proxy_url(self):
        return "http://127.0.0.1:%d" % self.mixed_port

    def stop(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(3)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass
        self.proc = None
        shutil.rmtree(self.dir, ignore_errors=True)


CORE = None


def core_stop():
    global CORE
    if CORE:
        CORE.stop()
        CORE = None


atexit.register(core_stop)


# ---------------------------------------------------------------- 评分

HOSTING_WORDS = ("1gservers", "reliablesite", "fdcservers", "wholesale", "ovh", "hetzner",
                 "digitalocean", "vultr", "linode", "amazon", "aws", "google", "cloudflare",
                 "g-core", "gcore", "cyberverse", "contabo", "oracle", "alibaba")


def score_row(r):
    if not is_ok(r):
        return 0
    s = 0
    h = r.get("hosting")
    s += 34 if h is False else (0 if h is True else 10)
    if r.get("proxy_flag") is False:
        s += 6
    g = r.get("google_ok")
    s += 8 if g is True else (-8 if g is False else 0)
    s += min(28, int((r.get("speed_kbps") or 0) / 25))
    pw = r.get("purity")
    if pw is not None:
        s += int(16 * pw / 100.0)
        if pw < 30:
            s -= 6
    lat = r.get("latency_ms")
    if lat:
        s += max(0, 24 - int(lat / 40))
    if any(w in (r.get("asn") or "").lower() for w in HOSTING_WORDS):
        s -= 12
    if "5倍率" in (r.get("name") or ""):
        s -= 4
    return max(0, min(100, s))


def verdict(r):
    if not is_ok(r):
        return "连不上"
    if r.get("google_ok") is False:
        return "连不上"
    sc, sp = r.get("score", 0), (r.get("speed_kbps") or 0)
    if sc >= 62 and sp >= 150:
        return "优先试"
    if sc >= 42:
        return "可试"
    return "不建议"


# ---------------------------------------------------------------- 扫描

def scan_static(cfg, nodes, chk=None):
    chk = chk or {"latency": True, "exitip": True, "speed": True,
                  "purity": True, "domain": True}
    set_state(mode="static", total=len(nodes), message="查 IP 属性…")
    idx = 0
    for n in nodes:
        if STOP_FLAG.is_set():
            break
        ip = doh_resolve(n["server"])
        info = lookup_ip(ip) if ip else {}
        scam = (scam_lookup(ip) if ip else {}) if chk["purity"] else {}
        push_row({"name": n["name"], "type": n["type"], "server": n["server"], "server_ip": ip,
                  "exit_ip": "", "asn": info.get("as") or "", "isp": info.get("isp") or "",
                  "country": info.get("country") or "", "city": info.get("city") or "",
                  "hosting": info.get("hosting"), "proxy_flag": info.get("proxy"),
                  "fraud": scam.get("fraud"),
                  "purity": (purity_of(info, scam)[0] if ip else None),
                  "tags": (purity_of(info, scam)[1] if ip else ""),
                  "latency_ms": None, "speed_kbps": None, "google_ok": None,
                  "mode": "static", "traffic": traffic_note(n["name"]), "note": "仅静态"})
        idx += 1
        set_state(done=idx, message="查 IP 属性 %d/%d" % (idx, len(nodes)))
    set_state(finished=True, running=False, message="静态归类完成（未测延迟/速率）")


def scan_live(cfg, nodes, deep, run_id, chk=None):
    """并发调度：阶段一并发测延迟，阶段二深测与静态补查同时进行。"""
    global CORE
    chk = chk or {"latency": True, "exitip": True, "speed": True,
                  "purity": True, "domain": True}

    def alive():
        return run_id == RUN_ID[0] and not STOP_FLAG.is_set()

    def blank(n, lat=None):
        return {"name": n["name"], "type": n["type"], "server": n["server"], "server_ip": "",
                "exit_ip": "", "asn": "", "isp": "", "country": "", "city": "",
                "hosting": None, "proxy_flag": None, "fraud": None,
                "purity": None, "tags": "",
                "latency_ms": lat, "speed_kbps": None, "google_ok": None,
                "mode": "live", "traffic": traffic_note(n["name"]), "note": ""}

    set_state(mode="live", phase="start", total=len(nodes), done=0, ok=0, fail=0,
              phase_done=0, phase_total=0, cur="", cur_step=0,
              static_done=0, static_total=0, message="启动临时内核…")
    core = None
    try:
        core = Core(cfg["proxies_block"], [n["name"] for n in nodes])
        core.start()
        CORE = core
        set_state(core="%s @ %d" % (os.path.basename(core.bin), core.api_port))
    except Exception as e:
        if core:
            core.stop()
        if run_id == RUN_ID[0]:
            set_state(running=False, finished=True, message="内核启动失败：%s" % e)
        return

    # ---------------- 阶段一：延迟，12 并发
    lat = {}
    if chk["latency"]:
        done_c = {"n": 0}

        def one(n):
            if not alive():
                return
            d = core.delay(n["name"])
            lat[n["name"]] = d
            row = blank(n, d)
            row["note"] = "连不上" if d is None else "待测速"
            push_row(row)
            with STATE_LOCK:
                done_c["n"] += 1
                got = done_c["n"]
            set_state(phase="delay", done=got, phase_done=got, phase_total=len(nodes),
                      message="测延迟 %d/%d" % (got, len(nodes)))

        try:
            with ThreadPoolExecutor(max_workers=12) as ex:
                list(ex.map(one, nodes))
        except Exception:
            pass

    if not alive():
        if core is CORE:
            core_stop()
        return

    # ---------------- 划分：深测对象 / 静态补查对象
    if chk["latency"]:
        ordered = [n for n in nodes if lat.get(n["name"]) is not None]
        ordered.sort(key=lambda n: lat.get(n["name"]) or 99999)
    else:
        ordered = list(nodes)
    measured = ordered[:20] if deep else []
    names = {n["name"] for n in measured}
    rest = [n for n in nodes if n["name"] not in names]

    base = len(nodes) if chk["latency"] else 0
    total = base + len(measured)
    set_state(phase="deep", done=base, total=total, phase_done=0,
              phase_total=len(measured), static_total=len(rest),
              message="深测 %d 个，同时后台补查 %d 个" % (len(measured), len(rest)))

    proxy_url = core.proxy_url()
    pool = ThreadPoolExecutor(max_workers=4)
    stat_c = {"n": 0}

    # ---------------- 后台任务 A：静态补查（不碰内核）
    def static_task(n):
        if not alive():
            return
        row = blank(n, lat.get(n["name"]))
        try:
            ip = doh_resolve(n["server"])
            if ip:
                row["server_ip"] = ip
                info = lookup_ip(ip)
                row["asn"] = info.get("as") or ""
                row["isp"] = info.get("isp") or ""
                row["hosting"] = info.get("hosting")
                row["proxy_flag"] = info.get("proxy")
                if chk["purity"]:
                    scam = scam_lookup(ip)
                    row["fraud"] = scam.get("fraud")
                    row["purity"], row["tags"] = purity_of(info, scam)
            row["note"] = "仅测延迟" if chk["latency"] else "未深测"
        except Exception as e:
            row["note"] = str(e)[:40]
        push_row(row)
        with STATE_LOCK:
            stat_c["n"] += 1
            got = stat_c["n"]
        set_state(static_done=got, message="后台补查 %d/%d" % (got, len(rest)))

    # ---------------- 后台任务 B：出口 IP 的补充信息（属性 + 纯净度）
    def enrich_task(name, ip):
        try:
            info = lookup_ip(ip)
            fields = {"asn": info.get("as") or "", "isp": info.get("isp") or "",
                      "hosting": info.get("hosting"), "proxy_flag": info.get("proxy"),
                      "country": info.get("country") or "", "city": info.get("city") or ""}
            if chk["purity"]:
                scam = scam_lookup(ip)
                pw, tg = purity_of(info, scam)
                if pw is not None:
                    fields.update(fraud=scam.get("fraud"), purity=pw, tags=tg)
            update_row(name, **fields)
        except Exception:
            pass

    for n in rest:
        pool.submit(static_task, n)

    # ---------------- 阶段二：深测（串行过内核，但不再等外部查询）
    dc = {"n": 0}
    okc, failc = 0, 0
    for n in measured:
        if not alive():
            break
        name = n["name"]
        with STATE_LOCK:
            nxt = dc["n"] + 1
        row = blank(n, lat.get(name))
        push_row(row)
        set_state(cur=name, cur_step=1,
                  message="深测 %d/%d（%s）" % (nxt, len(measured), name))
        good = False
        try:
            core.select(name)
            time.sleep(0.3)
            if chk["exitip"] or chk["purity"]:
                set_state(cur_step=2)
                r = http_get(EXITIP_URL, proxy=proxy_url, timeout=6, deadline=6)
                if r["ok"] and r["status"] == 200:
                    ip = r["body"].decode("utf-8", "ignore").strip().splitlines()[0].strip()
                    update_row(name, exit_ip=ip)
                    pool.submit(enrich_task, name, ip)
                    good = True
                else:
                    update_row(name, note=("连不上 " + str(r.get("error") or r.get("status")))[:50])
            else:
                good = True
            if good:
                if chk["domain"]:
                    set_state(cur_step=3)
                    g = http_get(GOOGLE_PROBE, proxy=proxy_url, timeout=6, max_bytes=2048,
                                 deadline=5)
                    update_row(name, google_ok=bool(g["ok"] and g["status"] in
                                                    (200, 204, 301, 302, 400, 401, 403, 404)))
                if chk["speed"]:
                    set_state(cur_step=4)
                    t0 = time.time()
                    sp = http_get(SPEED_URL, proxy=proxy_url, timeout=5,
                                  max_bytes=393216, deadline=8)
                    dt = time.time() - t0
                    ttfb = sp.get("ttfb") or 0
                    span = max(0.25, dt - ttfb)   # 首字节之后才算下载耗时
                    if sp["ok"] and sp["status"] == 200 and sp["body"] and span > 0:
                        cut = (len(sp["body"]) < 393216)
                        update_row(name, speed_kbps=int(len(sp["body"]) / 1024 / span),
                                   speed_cut=cut)
                    elif sp.get("error"):
                        update_row(name, note=("测速失败:" + str(sp["error"])[:40]))
        except Exception as e:
            update_row(name, note=str(e)[:60])
        set_state(cur_step=5)
        if good:
            okc += 1
        else:
            failc += 1
        with STATE_LOCK:
            dc["n"] += 1
            dn = dc["n"]
        set_state(done=base + dn, phase_done=dn, ok=okc, fail=failc)

    pool.shutdown(wait=True)
    if core is CORE:
        core_stop()
    else:
        core.stop()
    if run_id == RUN_ID[0]:
        set_state(finished=True, running=False, message="实测完成", core="",
                  phase="done", cur="", cur_step=0)


# ---------------------------------------------------------------- 窗口

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser():
    for p in BROWSER_CANDIDATES:
        if os.path.exists(p):
            return p
    return ""


def open_app_window(url):
    """用 Edge/Chrome 的应用模式开一个无标签页、无地址栏的独立窗口。返回进程对象或 None。"""
    exe = find_browser()
    if not exe:
        return None
    prof = os.path.join(tempfile.gettempdir(), "agscan-ui-profile")
    args = [exe, "--app=" + url, "--window-size=1400,920", "--user-data-dir=" + prof,
            "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"]
    try:
        return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None


# ---------------------------------------------------------------- Web

class Handler(BaseHTTPRequestHandler):
    server_version = "node-checkup/" + VERSION
    sys_version = ""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p in ("/", "/index.html"):
            return self._send(200, HTML, "text/html; charset=utf-8")
        if p == "/api/state":
            try:
                cfg = parse_config()
            except Exception as e:
                return self._send(500, {"error": str(e)})
            core = find_mihomo()
            return self._send(200, {"version": VERSION, "nodes": cfg["nodes"],
                                    "config_path": cfg["config_path"],
                                    "config_mtime": cfg["config_mtime"],
                                    "core_found": bool(core), "core_path": core,
                                    "running": STATE["running"]})
        if p == "/api/progress":
            with STATE_LOCK:
                return self._send(200, dict(STATE))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        payload = {}
        if n:
            try:
                payload = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                pass

        if p == "/api/start":
            if STATE["running"]:
                return self._send(409, {"error": "已有任务在进行"})
            try:
                cfg = parse_config()
            except Exception as e:
                return self._send(500, {"error": str(e)})
            mode = payload.get("mode", "live")
            deep = bool(payload.get("deep", True))
            chk = {"latency": True, "exitip": True, "speed": True,
                   "purity": True, "domain": True}
            for k, v in (payload.get("checks") or {}).items():
                if k in chk:
                    chk[k] = bool(v)
            if chk["purity"]:
                chk["exitip"] = True  # 纯净度依赖真实出口 IP
            want = payload.get("nodes")
            nodes = cfg["nodes"] if not want else [x for x in cfg["nodes"] if x["name"] in set(want)]
            if mode == "live" and not find_mihomo():
                return self._send(400, {"error": "找不到 verge-mihomo.exe，请确认 Clash Verge 安装正常"})
            STOP_FLAG.clear()
            RUN_ID[0] += 1
            my_id = RUN_ID[0]
            set_state(running=True, finished=False, rows=[], done=0, total=len(nodes),
                      mode=mode, message="准备中", started_at=time.time(), core="")
            t = threading.Thread(target=(scan_live if mode == "live" else scan_static),
                                 args=((cfg, nodes, deep, my_id, chk) if mode == "live"
                                       else (cfg, nodes, chk)),
                                 daemon=True)
            t.start()
            return self._send(200, {"ok": True, "total": len(nodes)})

        if p == "/api/stop":
            STOP_FLAG.set()
            return self._send(200, {"ok": True})

        if p == "/api/quit":
            threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Clash 节点体检器</title>
<style>
 :root{--bg:#0f1116;--card:#171a21;--line:#242833;--txt:#e6e8ee;--dim:#8b93a5;
       --ok:#3ddc84;--warn:#ffb020;--bad:#ff5c5c;--accent:#4d9fff}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
 .wrap{max-width:1560px;margin:0 auto;padding:20px 20px 50px}
 h1{font-size:19px;margin:0 0 3px}
 .sub{color:var(--dim);font-size:12.5px;margin-bottom:14px}
 .bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:var(--card);
      border:1px solid var(--line);border-radius:12px;padding:11px 14px;margin-bottom:12px}
 button{background:#232838;color:var(--txt);border:1px solid #2e3547;border-radius:9px;
        padding:8px 14px;font-size:13px;cursor:pointer;transition:.15s}
 button:hover{background:#2b3145}
 button.primary{background:var(--accent);border-color:var(--accent);color:#08101c;font-weight:600}
 button.danger{border-color:#5a2b2b;color:#ff9b9b}
 button:disabled{opacity:.4;cursor:not-allowed}
 .pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--dim);
       border:1px solid var(--line);border-radius:999px;padding:4px 11px;background:#12151c}
 .dot{width:7px;height:7px;border-radius:50%;background:var(--bad)}.dot.on{background:var(--ok)}
 .prog{height:6px;background:#20242f;border-radius:99px;overflow:hidden;flex:1;min-width:140px}
 .prog>i{display:block;height:100%;width:0;background:linear-gradient(90deg,#4d9fff,#3ddc84);transition:.25s}
 table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
       border-radius:12px;overflow:hidden}
 th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line);font-size:12.8px;white-space:nowrap}
 th{background:#1b1f28;color:var(--dim);font-weight:600;position:sticky;top:0;cursor:pointer;user-select:none}
 tr:hover td{background:#1a1e27}
 .num{text-align:right;font-variant-numeric:tabular-nums}
 .badge{display:inline-block;padding:1px 8px;border-radius:7px;font-size:11.5px;font-weight:600}
 .b-ok{background:rgba(61,220,132,.16);color:var(--ok)}
 .b-warn{background:rgba(255,176,32,.16);color:var(--warn)}
 .b-bad{background:rgba(255,92,92,.16);color:var(--bad)}
 .b-dim{background:#252a36;color:var(--dim)}
 .sc{font-weight:700}
 .empty{color:var(--dim);padding:34px;text-align:center}
 .tip{color:var(--dim);font-size:12px;margin-top:11px;line-height:1.7}
.chk{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:#c9d1d9;cursor:pointer;user-select:none}
.chk input{width:15px;height:15px;accent-color:#2ea043;cursor:pointer}
</style></head><body><div class="wrap">
<h1>Clash 节点体检器</h1>
<div class="sub">数据源：Clash Verge 运行态配置 · 延迟 / 速率 / 出口 IP / 纯净度 都是真实实测 · 独立临时内核，不干扰正在用的 Clash · 不使用任何 Google 账号凭据</div>

<div class="bar">
  <span class="pill"><span class="dot" id="dotCore"></span><span id="coreText">检查内核…</span></span>
  <button class="primary" id="btnLive">实测扫描（延迟+出口+速率）</button>
  <button id="btnQuick">只测延迟（快）</button>
  <button id="btnStatic">静态归类</button>
  <button id="btnStop" disabled>停止</button>
  <div class="prog"><i id="prog"></i></div>
  <span class="pill" id="cnt">0 / 0</span>
  <span class="pill" id="eta">—</span>
  <span style="flex:1"></span>
  <button class="danger" id="btnQuit">退出程序</button>
</div>

<div class="bar" style="gap:14px">
  <span class="pill">检测项</span>
  <label class="chk"><input type="checkbox" id="cLat" checked> 延迟</label>
  <label class="chk"><input type="checkbox" id="cExit" checked> 出口 IP</label>
  <label class="chk"><input type="checkbox" id="cSpeed" checked> 速率</label>
  <label class="chk"><input type="checkbox" id="cPure" checked> 纯净度</label>
  <label class="chk"><input type="checkbox" id="cDom" checked> 域名可达</label>
  <span style="flex:1"></span>
  <span class="pill" id="est">预计约 4 分钟</span>
</div>

<div class="bar" style="gap:8px">
  <span class="pill">阶段</span>
  <div class="prog" style="max-width:360px"><i id="prog2" style="background:#58a6ff"></i></div>
  <span class="pill" id="phaseText">—</span>
  <span class="pill" id="stat2">成功 0 · 失败 0</span>
  <span class="pill" id="cur">—</span>
</div>

<div class="bar" style="gap:8px">
  <span class="pill">筛选</span>
  <button data-f="all">全部</button>
  <button data-f="us">美国</button>
  <button data-f="asia">亚洲</button>
  <button data-f="fast">延迟&lt;300ms</button>
  <button data-f="good">评分≥42</button>
  <button data-f="pure">纯净度≥70</button>
  <button data-f="dirty">纯净度&lt;40</button>
  <span style="flex:1"></span>
  <span class="pill" id="msg">空闲</span>
</div>

<table><thead><tr>
 <th data-k="name">节点</th><th data-k="type">协议</th><th data-k="exit_ip">出口 IP</th>
 <th data-k="asn">ASN / ISP</th><th data-k="hostTxt">IP 属性</th>
 <th data-k="latency_ms" class="num">延迟 ms</th><th data-k="speed_kbps" class="num">速率 KB/s</th>
 <th data-k="purity" class="num">纯净度</th><th data-k="fraud" class="num">欺诈分</th>
 <th data-k="tags">标记</th><th data-k="googleTxt">域名可达</th><th data-k="traffic">倍率</th>
 <th data-k="score" class="num">评分</th><th data-k="verdict">建议</th>
</tr></thead><tbody id="tb"><tr><td colspan="14" class="empty">点上面的按钮开始</td></tr></tbody></table>

<div class="tip">
延迟 = 内核真实 delay 测试（www.gstatic.com/generate_204）；速率 = 真实下载 512 KB 除以耗时。<br>
纯净度参考 ippure 的维度：欺诈分（scamalytics）、机房属性、代理标记、代理历史，综合成 0-100，越高越干净。<br>
「域名可达」只表示 daily-cloudcode-pa.googleapis.com 能不能连上，<b>不代表 Google 地区放行</b>——那个校验在鉴权之后，本工具不碰账号所以测不到。<br>
实测会另起一个临时内核（另开端口、临时配置），结束后自动关闭并删除，你的 Clash 全程不受影响。
</div>
</div>
<script>
let rows=[],filter='all',sortKey='score',sortAsc=false,timer=null;
const el=function(id){return document.getElementById(id)};
const esc=function(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})};

async function loadState(){
  const s=await (await fetch('/api/state')).json();
  if(s.error){el('coreText').textContent=s.error;return;}
  el('dotCore').className='dot'+(s.core_found?' on':'');
  el('coreText').textContent=s.core_found?('内核就绪 v'+s.version):'未找到内核';
  el('btnLive').disabled=!s.core_found;
}
function readChecks(){
  return {latency:cLat.checked, exitip:cExit.checked, speed:cSpeed.checked,
          purity:cPure.checked, domain:cDom.checked};
}
function estTime(){
  const c=readChecks();
  const lat=c.latency?0.75:0;          // 延迟阶段整体约 45 秒
  let per=3;                            // 每个深测节点的固定开销
  if(c.exitip||c.purity) per+=3;        // 出口 IP 查询
  if(c.purity) per+=3;                  // scamalytics 抓取
  if(c.domain) per+=1;                  // Google 域名探测
  if(c.speed) per+=8;                   // 512 KB 测速
  const min=lat+(per*20)/60;
  return '预计约 '+Math.max(1,Math.round(min))+' 分钟';
}
function refreshEst(){ est.textContent=estTime(); }
['cLat','cExit','cSpeed','cPure','cDom'].forEach(function(id){
  document.getElementById(id).addEventListener('change',refreshEst);
});
refreshEst();

async function start(mode,deep){
  const r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:mode,deep:deep,checks:readChecks()})});
  const j=await r.json();
  if(j.error){alert(j.error);return;}
  rows=[];render();
  el('btnLive').disabled=true;el('btnQuick').disabled=true;el('btnStatic').disabled=true;el('btnStop').disabled=false;
  if(!timer)timer=setInterval(poll,500);
}
async function poll(){
  const s=await (await fetch('/api/progress')).json();
  rows=s.rows||[];
  const done=s.done||0, total=s.total||0, pd=s.phase_done||0, pt=s.phase_total||0;
  el('cnt').textContent=done+' / '+total;
  el('msg').textContent=s.message||'空闲';
  el('prog').style.width=(total?Math.round(100*done/total):0)+'%';
  el('prog2').style.width=(pt?Math.round(100*pd/pt):0)+'%';
  const pname={delay:'阶段一 测延迟',deep:'阶段二 深测',done:'已完成',start:'准备中'}[s.phase]||'—';
  el('phaseText').textContent=pname+(pt?' '+pd+'/'+pt:'');
  el('stat2').textContent='成功 '+(s.ok||0)+' · 失败 '+(s.fail||0)+' · 补查 '+(s.static_done||0)+'/'+(s.static_total||0);
  const stepName={1:'选中节点',2:'取出口 IP',3:'探域名',4:'测速',5:'结束'}[s.cur_step]||'';
  el('cur').textContent=s.cur?('当前 '+s.cur+(stepName?' · '+stepName:'')):'—';
  if(s.running&&s.started_at&&done>0&&total>done){
    const elapsed=(Date.now()/1000)-s.started_at;
    const left=Math.max(0,Math.round((elapsed/done)*(total-done)));
    el('eta').textContent='剩余约 '+Math.floor(left/60)+' 分 '+String(left%60).padStart(2,'0')+' 秒';
  }else if(!s.running){ el('eta').textContent='—'; }
  render();
  if(s.finished&&!s.running){
    el('btnLive').disabled=false;el('btnQuick').disabled=false;el('btnStatic').disabled=false;el('btnStop').disabled=true;
    clearInterval(timer);timer=null;loadState();
  }
}
const hostTxt=function(v){return v===true?'机房':(v===false?'家宽/ISP':'未知')};
const gTxt=function(v){return v===true?'连通':(v===false?'连不上':'未测')};
function match(r){
  const n=r.name||'';
  if(filter==='us')return /美国|美区|USA/.test(n);
  if(filter==='asia')return /台湾|香港|日本|新加坡|韩国/.test(n);
  if(filter==='fast')return r.latency_ms&&r.latency_ms<300;
  if(filter==='good')return (r.score||0)>=42;
  if(filter==='pure')return (r.purity||0)>=70;
  if(filter==='dirty')return r.purity!==null&&r.purity!==undefined&&r.purity<40;
  return true;
}
function render(){
  const list=rows.filter(match).slice(),k=sortKey;
  list.sort(function(a,b){
    const ao=(a.ok===false)?1:0, bo=(b.ok===false)?1:0;
    if(ao!==bo)return ao-bo;
    let x=a[k],y=b[k];
    if(k==='hostTxt'){x=a.hosting;y=b.hosting}
    if(k==='googleTxt'){x=a.google_ok;y=b.google_ok}
    if(x===null||x===undefined)x=-1; if(y===null||y===undefined)y=-1;
    if(typeof x==='string'||typeof y==='string')return sortAsc?String(x).localeCompare(String(y)):String(y).localeCompare(String(x));
    return sortAsc?x-y:y-x;});
  const tb=el('tb');
  if(!list.length){tb.innerHTML='<tr><td colspan="14" class="empty">暂无数据</td></tr>';return;}
  tb.innerHTML=list.map(function(r){
    const hv=hostTxt(r.hosting),hc=r.hosting===false?'b-ok':(r.hosting===true?'b-warn':'b-dim');
    const gv=gTxt(r.google_ok),gc=r.google_ok===true?'b-ok':(r.google_ok===false?'b-bad':'b-dim');
    const sc=r.score||0,scc=sc>=62?'b-ok':(sc>=42?'b-warn':'b-bad');
    const vv=r.verdict||'—',vc=vv==='优先试'?'b-ok':(vv==='可试'?'b-warn':(vv==='连不上'?'b-bad':'b-dim'));
    const lat=(r.latency_ms===null||r.latency_ms===undefined)?'—':r.latency_ms;
    const sp=(r.speed_kbps===null||r.speed_kbps===undefined)?'—':r.speed_kbps;
    return '<tr><td>'+esc(r.name)+'</td><td>'+esc(r.type||'')+'</td>'+
      '<td>'+esc(r.exit_ip||r.server_ip||'—')+'</td>'+
      '<td>'+esc(r.asn||'')+(r.isp?' · '+esc(r.isp):'')+'</td>'+
      '<td><span class="badge '+hc+'">'+hv+'</span></td>'+
      '<td class="num">'+lat+'</td><td class="num">'+sp+'</td>'+
      '<td class="num">'+(r.purity===null||r.purity===undefined?'—':'<span class="badge '+
        (r.purity>=70?'b-ok':(r.purity>=40?'b-warn':'b-bad'))+'">'+r.purity+'</span>')+'</td>'+
      '<td class="num">'+(r.fraud===null||r.fraud===undefined?'—':r.fraud)+'</td>'+
      '<td>'+esc(r.tags||'')+'</td>'+
      '<td><span class="badge '+gc+'">'+gv+'</span></td><td>'+esc(r.traffic||'')+'</td>'+
      '<td class="num"><span class="badge '+scc+' sc">'+sc+'</span></td>'+
      '<td><span class="badge '+vc+'">'+vv+'</span></td></tr>';}).join('');
}
el('btnLive').onclick=function(){start('live',true)};
el('btnQuick').onclick=function(){start('live',false)};
el('btnStatic').onclick=function(){start('static',false)};
el('btnStop').onclick=function(){fetch('/api/stop',{method:'POST'})};
el('btnQuit').onclick=function(){if(confirm('确定退出体检器？')){fetch('/api/quit',{method:'POST'});
  document.body.innerHTML='<div style="padding:40px;color:#8b93a5">已退出，可以关闭此页面。</div>';}};
document.querySelectorAll('[data-f]').forEach(function(b){b.onclick=function(){filter=b.dataset.f;render()}});
document.querySelectorAll('th[data-k]').forEach(function(th){th.onclick=function(){
  const k=th.dataset.k; if(sortKey===k)sortAsc=!sortAsc; else{sortKey=k;sortAsc=false} render();}});
loadState();
poll();   // 打开/刷新页面时把上一次的结果直接拉回来
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="只启动服务，不显示界面")
    ap.add_argument("--browser", action="store_true", help="用系统浏览器而不是桌面窗口")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        cfg = parse_config()
        print("%s v%s" % (APPNAME, VERSION))
        print("  配置：%s" % RUNTIME_YAML)
        print("  节点：%d 个" % len(cfg["nodes"]))
        print("  内核：%s" % (find_mihomo() or "未找到"))
    except Exception as e:
        print("读取配置失败：%s" % e)

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = "http://%s:%d/" % (a.host, a.port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("  界面：%s" % url)

    if a.no_browser:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            core_stop()
        return

    if not a.browser:
        # 优先原生窗口
        if True:
            try:
                import importlib
                webview = importlib.import_module("webview")
                webview.create_window(APPNAME + " v" + VERSION, url,
                                      width=1400, height=920, min_size=(980, 640))
                webview.start()
                core_stop()
                os._exit(0)
            except Exception as e:
                print("  原生窗口不可用（%s），改用应用窗口" % e)
        # 应用窗口：无标签页、无地址栏，关掉就退出
        proc = open_app_window(url)
        if proc is not None:
            print("  已打开应用窗口（关闭窗口即退出程序）")
            try:
                while proc.poll() is None:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            core_stop()
            os._exit(0)
        print("  未找到 Edge/Chrome，退回系统浏览器")

    try:
        webbrowser.open(url)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        core_stop()


if __name__ == "__main__":
    main()
