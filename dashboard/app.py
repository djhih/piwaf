#!/usr/bin/env python3
"""PiWAF 輕量儀表板後端：FastAPI 對 SQLite 下 SQL 聚合，回傳 JSON。

不用 pandas/streamlit —— 聚合全交給 SQLite，渲染交給瀏覽器的 Chart.js。
另含「負載監測」：背景採樣主機 load/RAM/swap + 目前流量，判定能否負荷，
過載時在儀表板跳警示、並可打 webhook 對外告警。
"""
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta

import sqlite3

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

DB_PATH = os.environ.get("DB_PATH", "/data/piwaf.db")
THRESHOLD = int(os.environ.get("ANOMALY_THRESHOLD", "5"))
HERE = os.path.dirname(os.path.abspath(__file__))

# ── 負載監測門檻（可用環境變數覆寫）──
LOAD_WARN = float(os.environ.get("HEALTH_LOAD_WARN", "1.0"))   # load1/核心數
LOAD_CRIT = float(os.environ.get("HEALTH_LOAD_CRIT", "2.0"))
MEM_WARN = float(os.environ.get("HEALTH_MEM_WARN", "15"))      # 可用記憶體 %（低於則警示）
MEM_CRIT = float(os.environ.get("HEALTH_MEM_CRIT", "7"))
SWAP_WARN = float(os.environ.get("HEALTH_SWAP_WARN", "50"))    # swap 使用 %（高於則警示）
SWAP_CRIT = float(os.environ.get("HEALTH_SWAP_CRIT", "80"))
HEALTH_INTERVAL = int(os.environ.get("HEALTH_INTERVAL", "10"))
ALERT_WEBHOOK = os.environ.get("ALERT_WEBHOOK", "").strip()
ALERT_COOLDOWN = int(os.environ.get("ALERT_COOLDOWN", "300"))

app = FastAPI(title="PiWAF API")

LATEST_HEALTH = {"status": "unknown", "reasons": [], "metrics": {}, "time": None}
_alert_state = {"last_status": "ok", "last_sent": 0.0}


def connect():
    """唯讀開啟 SQLite（配合 parser 的 WAL，可邊寫邊讀）。"""
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


# ── 負載監測 ────────────────────────────────────────────────────────────
def read_loadavg():
    with open("/proc/loadavg") as f:
        parts = f.read().split()
    return float(parts[0]), float(parts[1]), float(parts[2])


def read_meminfo():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            info[k] = int(rest.split()[0])  # kB
    return info


def req_per_min():
    try:
        con = connect()
        cutoff = (datetime.now() - timedelta(seconds=60)).isoformat(timespec="seconds")
        n = con.execute("SELECT COUNT(*) FROM events WHERE ts>=?", (cutoff,)).fetchone()[0]
        con.close()
        return n
    except sqlite3.OperationalError:
        return 0


def worst(a, b):
    order = {"ok": 0, "warn": 1, "critical": 2}
    return a if order[a] >= order[b] else b


def compute_health():
    ncpu = os.cpu_count() or 1
    load1, load5, _ = read_loadavg()
    mem = read_meminfo()
    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable", 0)
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)

    load_ratio = load1 / ncpu
    mem_avail_pct = (mem_avail / mem_total * 100) if mem_total else 100
    swap_used_pct = ((swap_total - swap_free) / swap_total * 100) if swap_total else 0
    rpm = req_per_min()

    status, reasons = "ok", []

    def check(level_warn, level_crit, value, hi, msg):
        nonlocal status
        if hi:  # 越高越糟
            if value >= level_crit:
                status = worst(status, "critical"); reasons.append("🔴 " + msg)
            elif value >= level_warn:
                status = worst(status, "warn"); reasons.append("🟠 " + msg)
        else:   # 越低越糟
            if value <= level_crit:
                status = worst(status, "critical"); reasons.append("🔴 " + msg)
            elif value <= level_warn:
                status = worst(status, "warn"); reasons.append("🟠 " + msg)

    check(LOAD_WARN, LOAD_CRIT, load_ratio, True,
          f"CPU 負載 {load1:.1f}（{ncpu} 核，比值 {load_ratio:.2f}）")
    check(MEM_WARN, MEM_CRIT, mem_avail_pct, False,
          f"可用記憶體僅 {mem_avail_pct:.0f}%")
    # swap 只有在「記憶體也吃緊」時才算過載訊號 —— 單純 swap 滿、RAM 還夠是 Linux 常態，
    # 會在記憶體耗盡 + 持續換頁（thrashing）時才真正拖垮機器。
    if mem_avail_pct <= MEM_WARN:
        check(SWAP_WARN, SWAP_CRIT, swap_used_pct, True,
              f"swap 使用 {swap_used_pct:.0f}%（且記憶體吃緊）")

    return {
        "status": status,
        "reasons": reasons,
        "metrics": {
            "load1": round(load1, 2), "load5": round(load5, 2), "ncpu": ncpu,
            "load_ratio": round(load_ratio, 2),
            "mem_total_mb": round(mem_total / 1024),
            "mem_avail_mb": round(mem_avail / 1024),
            "mem_avail_pct": round(mem_avail_pct),
            "swap_total_mb": round(swap_total / 1024),
            "swap_used_pct": round(swap_used_pct),
            "req_per_min": rpm, "req_per_sec": round(rpm / 60, 1),
        },
        "time": datetime.now().isoformat(timespec="seconds"),
    }


def send_webhook(health, recovered=False):
    if not ALERT_WEBHOOK:
        return
    m = health["metrics"]
    icon = "✅" if recovered else ("🔴" if health["status"] == "critical" else "🟠")
    title = "負載已恢復" if recovered else f"負載警報 [{health['status']}]"
    msg = (f"{icon} PiWAF {title}\n"
           f"原因: {'; '.join(health['reasons']) or '正常'}\n"
           f"流量: {m['req_per_min']} req/min（{m['req_per_sec']} req/s）\n"
           f"load {m['load1']}/{m['ncpu']} · mem avail {m['mem_avail_pct']}% · "
           f"swap {m['swap_used_pct']}%")
    payload = json.dumps({"content": msg, "text": msg,  # Discord/Slack 相容
                          "status": health["status"], "reasons": health["reasons"],
                          "metrics": m}).encode()
    try:
        req = urllib.request.Request(ALERT_WEBHOOK, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:  # noqa: BLE001 — 告警失敗不能拖垮服務
        print(f"[monitor] webhook 失敗：{e}", flush=True)


def monitor_loop():
    global LATEST_HEALTH
    while True:
        try:
            h = compute_health()
            LATEST_HEALTH = h
            now = time.time()
            prev = _alert_state["last_status"]
            if h["status"] == "critical":
                if prev != "critical" or now - _alert_state["last_sent"] > ALERT_COOLDOWN:
                    send_webhook(h)
                    _alert_state["last_sent"] = now
            elif prev == "critical":   # 從 critical 恢復
                send_webhook(h, recovered=True)
            _alert_state["last_status"] = h["status"]
        except Exception as e:  # noqa: BLE001
            print(f"[monitor] 採樣失敗：{e}", flush=True)
        time.sleep(HEALTH_INTERVAL)


@app.on_event("startup")
def start_monitor():
    LATEST_HEALTH.update(compute_health())
    threading.Thread(target=monitor_loop, daemon=True).start()
    print(f"[monitor] 啟動，每 {HEALTH_INTERVAL}s 採樣"
          f"{'（webhook 已設定）' if ALERT_WEBHOOK else ''}", flush=True)


@app.get("/api/health")
def health():
    return JSONResponse(LATEST_HEALTH)


# ── 攻擊資料聚合 ─────────────────────────────────────────────────────────
def build_where(mode, scenario):
    where, params = "1=1", []
    if mode:
        where += " AND mode=?"
        params.append(mode)
    if scenario:
        where += " AND scenario=?"
        params.append(scenario)
    return where, params


@app.get("/api/stats")
def stats(mode: str = "", scenario: str = ""):
    base = {"threshold": THRESHOLD, "health": LATEST_HEALTH}
    try:
        con = connect()
        con.execute("SELECT 1 FROM events LIMIT 1")
    except sqlite3.OperationalError:
        return JSONResponse({"empty": True, **base})

    w, p = build_where(mode, scenario)
    one = lambda sql, pr=p: con.execute(sql, pr).fetchone()[0]
    rows = lambda sql, pr=p: con.execute(sql, pr).fetchall()

    total = one(f"SELECT COUNT(*) FROM events WHERE {w}")
    if total == 0:
        opts = filter_options(con)
        return JSONResponse({"empty": True, "filters": opts, **base})

    blocked = one(f"SELECT COALESCE(SUM(blocked),0) FROM events WHERE {w}")
    would = one(f"SELECT COALESCE(SUM(would_block),0) FROM events WHERE {w}")
    attacks = one(f"SELECT COUNT(*) FROM events WHERE category!='None' AND {w}")

    data = {
        "empty": False,
        **base,
        "kpi": {"total": total, "blocked": blocked,
                "would_block": would, "attacks": attacks},
        "categories": [
            {"label": r[0], "n": r[1]} for r in rows(
                f"SELECT category,COUNT(*) FROM events "
                f"WHERE category!='None' AND {w} GROUP BY category ORDER BY 2 DESC")],
        "outcome": {"blocked": blocked, "passed": total - blocked},
        "top_rules": [
            {"label": r[0], "n": r[1]} for r in rows(
                f"SELECT je.value,COUNT(*) FROM events,json_each(events.rule_ids) je "
                f"WHERE {w} GROUP BY je.value ORDER BY 2 DESC LIMIT 10")],
        "top_endpoints": [
            {"label": r[0], "n": r[1]} for r in rows(
                f"SELECT path,COUNT(*) FROM events "
                f"WHERE path IS NOT NULL AND {w} GROUP BY path ORDER BY 2 DESC LIMIT 10")],
        "scores": [
            {"score": r[0], "n": r[1]} for r in rows(
                f"SELECT anomaly_score,COUNT(*) FROM events "
                f"WHERE {w} GROUP BY anomaly_score ORDER BY anomaly_score")],
        "timeline": [
            {"t": r[0], "total": r[1], "blocked": r[2]} for r in rows(
                f"SELECT substr(ts,1,16),COUNT(*),COALESCE(SUM(blocked),0) "
                f"FROM events WHERE {w} GROUP BY 1 ORDER BY 1")],
        "modes": [
            {"mode": r[0], "requests": r[1], "blocked": r[2],
             "would_block": r[3], "avg_score": r[4]} for r in con.execute(
                "SELECT mode,COUNT(*),COALESCE(SUM(blocked),0),"
                "COALESCE(SUM(would_block),0),ROUND(AVG(anomaly_score),2) "
                "FROM events GROUP BY mode ORDER BY 2 DESC")],
        "filters": filter_options(con),
    }
    con.close()
    return JSONResponse(data)


def filter_options(con):
    try:
        modes = [r[0] for r in con.execute(
            "SELECT DISTINCT mode FROM events WHERE mode IS NOT NULL ORDER BY 1")]
        scenarios = [r[0] for r in con.execute(
            "SELECT DISTINCT scenario FROM events WHERE scenario IS NOT NULL ORDER BY 1")]
    except sqlite3.OperationalError:
        modes, scenarios = [], []
    return {"modes": modes, "scenarios": scenarios}


# 靜態前端掛在根路徑（要放在 API 路由之後）
app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True),
          name="static")
