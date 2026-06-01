#!/usr/bin/env python3
"""把 BunkerWeb 的兩個 log 來源合併成結構化事件，寫入 SQLite。

BunkerWeb / libmodsecurity-nginx 的限制：
  - JSON audit log（SecAuditEngine On）只記「所有請求的中繼資料」，但 messages 是空的、
    且被擋下的 403 不會進來 → 用來抓「正常/通過」流量、scenario 標籤、總請求數。
  - nginx error_log 才有每一條觸發規則的完整資訊（rule id、tags、severity、anomaly
    score、是否 403）→ 用來抓「攻擊/被擋」的細節。

兩邊都有 unique_id，用它當 key 合併（UPSERT）：
  乾淨通過 → 只在 audit；觸發但放行 → 兩邊都有；被擋 403 → 只在 error_log。
"""
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime

AUDIT_LOG = os.environ.get("MODSEC_AUDIT_LOG", "/var/log/bunkerweb/modsec_audit.log")
ERROR_LOG = os.environ.get("ERROR_LOG", "/var/log/bunkerweb/error.log")
DB_PATH = os.environ.get("DB_PATH", "/data/piwaf.db")
THRESHOLD = int(os.environ.get("ANOMALY_THRESHOLD", "5"))
WAF_MODE = os.environ.get("WAF_MODE", "On")
POLL = float(os.environ.get("POLL_INTERVAL", "1.0"))

# CRS tag → 可讀攻擊類型
TAG_CATEGORY = {
    "attack-sqli": "SQLi", "attack-xss": "XSS", "attack-lfi": "LFI",
    "attack-rfi": "RFI", "attack-rce": "RCE", "attack-protocol": "Protocol",
    "attack-generic": "Generic", "attack-disclosure": "Disclosure",
    "attack-fixation": "SessionFixation", "attack-injection-php": "PHPi",
    "attack-ssrf": "SSRF", "attack-java": "Java", "attack-fileupload": "FileUpload",
}
# ModSecurity severity → CRS 異常分數權重
SEV_WEIGHT = {"2": 5, "3": 4, "4": 3, "5": 2}

# error_log 解析用
BRACKET_RE = re.compile(r'\[(\w+) "((?:\\.|[^"\\])*)"\]')
CLIENT_RE = re.compile(r'client: ([^,]+)')
REQUEST_RE = re.compile(r'request: "([^"]*)"')
VALUE_RE = re.compile(r'Value: `?(\d+)')

_lock = threading.Lock()


def init_db(con):
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, ts TEXT, modsec_time TEXT, client_ip TEXT,
            method TEXT, uri TEXT, path TEXT, status INTEGER,
            anomaly_score INTEGER, blocked INTEGER, would_block INTEGER,
            category TEXT, categories TEXT, rule_ids TEXT, n_rules INTEGER,
            scenario TEXT, mode TEXT, tags TEXT
        )""")
    con.commit()


def classify(tags):
    cats = sorted({TAG_CATEGORY[t] for t in tags if t in TAG_CATEGORY})
    return cats or ["None"]


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def scenario_from(uri):
    """從 query 參數 pscn 取出情境標籤（generator 會帶上）。"""
    if uri and "pscn=" in uri:
        return uri.split("pscn=", 1)[1].split("&", 1)[0]
    return None


# ── 來源 1：JSON audit log（所有請求的中繼資料）──────────────────────────
def handle_audit_line(con, line):
    try:
        tx = json.loads(line)["transaction"]
    except (json.JSONDecodeError, KeyError):
        return
    uid = tx.get("unique_id")
    if not uid:
        return
    req = tx.get("request", {}) or {}
    resp = tx.get("response", {}) or {}
    uri = req.get("uri")
    rec = {
        "id": uid, "ts": now_iso(), "modsec_time": tx.get("time_stamp"),
        "client_ip": tx.get("client_ip"), "method": req.get("method"),
        "uri": uri, "path": (uri or "").split("?", 1)[0] or None,
        "status": resp.get("http_code"), "scenario": scenario_from(uri),
    }
    upsert_meta(con, rec)


def upsert_meta(con, r):
    """只填中繼資料；不碰攻擊欄位（rule/score/blocked 由 error_log 負責）。"""
    with _lock:
        con.execute("""
            INSERT INTO events (id,ts,modsec_time,client_ip,method,uri,path,status,
                scenario,mode,anomaly_score,blocked,would_block,category,categories,
                rule_ids,n_rules,tags)
            VALUES (:id,:ts,:modsec_time,:client_ip,:method,:uri,:path,:status,
                :scenario,:mode,0,0,0,'None','["None"]','[]',0,'[]')
            ON CONFLICT(id) DO UPDATE SET
                modsec_time=COALESCE(events.modsec_time,:modsec_time),
                client_ip=COALESCE(events.client_ip,:client_ip),
                method=COALESCE(events.method,:method),
                uri=COALESCE(events.uri,:uri),
                path=COALESCE(events.path,:path),
                status=COALESCE(events.status,:status),
                scenario=COALESCE(events.scenario,:scenario)
        """, {**r, "mode": WAF_MODE})
        con.commit()


# ── 來源 2：nginx error_log（每條觸發規則 / 阻擋決策）────────────────────
def handle_error_line(con, line):
    if "ModSecurity:" not in line:
        return
    pairs = BRACKET_RE.findall(line)
    if not pairs:
        return
    fields, tags = {}, []
    for k, v in pairs:
        if k == "tag":
            tags.append(v)
        else:
            fields[k] = v
    uid = fields.get("unique_id")
    if not uid:
        return

    rule_id = fields.get("id")
    severity = fields.get("severity")
    is_block = "Access denied with code 403" in line
    # 攻擊規則才計分；949/980 是評估/彙總規則
    weight = 0
    if rule_id and not rule_id.startswith(("949", "980")):
        weight = SEV_WEIGHT.get(severity, 0)
    explicit = None
    if is_block:
        m = VALUE_RE.search(line)
        if m:
            explicit = int(m.group(1))

    cm = CLIENT_RE.search(line)
    rm = REQUEST_RE.search(line)
    method = uri = None
    if rm:
        parts = rm.group(1).split(" ")
        if len(parts) >= 2:
            method, uri = parts[0], parts[1]

    upsert_attack(con, {
        "id": uid, "ts": now_iso(), "client_ip": cm.group(1) if cm else None,
        "method": method, "uri": uri,
        "path": (uri or "").split("?", 1)[0] or None,
        "scenario": scenario_from(uri or fields.get("uri")),
        "rule_id": rule_id, "tags": tags, "weight": weight,
        "is_block": is_block, "explicit": explicit,
    })


def upsert_attack(con, r):
    with _lock:
        cur = con.execute(
            "SELECT anomaly_score,blocked,rule_ids,tags,status FROM events WHERE id=?",
            (r["id"],)).fetchone()
        if cur:
            score, blocked, rule_ids, tag_json, status = cur
            rule_ids = json.loads(rule_ids or "[]")
            all_tags = set(json.loads(tag_json or "[]"))
        else:
            score, blocked, rule_ids, all_tags, status = 0, 0, [], set(), None

        score += r["weight"]
        if r["explicit"] is not None:
            score = max(score, r["explicit"])
        if r["is_block"]:
            blocked, status = 1, 403
        if r["rule_id"] and r["rule_id"] not in rule_ids:
            rule_ids.append(r["rule_id"])
        all_tags.update(r["tags"])

        cats = classify(all_tags)
        row = {
            "id": r["id"], "ts": r["ts"], "client_ip": r["client_ip"],
            "method": r["method"], "uri": r["uri"], "path": r["path"],
            "status": status, "scenario": r["scenario"], "mode": WAF_MODE,
            "anomaly_score": score, "blocked": blocked,
            "would_block": 1 if score >= THRESHOLD else 0,
            "category": cats[0], "categories": json.dumps(cats),
            "rule_ids": json.dumps(rule_ids), "n_rules": len(rule_ids),
            "tags": json.dumps(sorted(all_tags)),
        }
        con.execute("""
            INSERT INTO events (id,ts,modsec_time,client_ip,method,uri,path,status,
                anomaly_score,blocked,would_block,category,categories,rule_ids,
                n_rules,scenario,mode,tags)
            VALUES (:id,:ts,NULL,:client_ip,:method,:uri,:path,:status,
                :anomaly_score,:blocked,:would_block,:category,:categories,:rule_ids,
                :n_rules,:scenario,:mode,:tags)
            ON CONFLICT(id) DO UPDATE SET
                ts=:ts, anomaly_score=:anomaly_score, blocked=:blocked,
                would_block=:would_block, category=:category, categories=:categories,
                rule_ids=:rule_ids, n_rules=:n_rules, tags=:tags,
                status=COALESCE(:status, events.status),
                client_ip=COALESCE(events.client_ip,:client_ip),
                method=COALESCE(events.method,:method),
                uri=COALESCE(events.uri,:uri), path=COALESCE(events.path,:path),
                scenario=COALESCE(events.scenario,:scenario)
        """, row)
        con.commit()


# ── 通用 tail ────────────────────────────────────────────────────────────
def tail(path, handler, con):
    while not os.path.exists(path):
        time.sleep(2)
    f = open(path, "r", encoding="utf-8", errors="replace")
    pos = 0
    while True:
        line = f.readline()
        if line:
            pos = f.tell()
            try:
                handler(con, line.strip())
            except Exception as ex:  # noqa: BLE001
                print(f"[parser] 跳過一行：{ex}", flush=True)
            continue
        try:
            if os.path.getsize(path) < pos:
                f.seek(0); pos = 0; continue
        except OSError:
            pass
        time.sleep(POLL)


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    init_db(con)
    print(f"[parser] 啟動：audit={AUDIT_LOG}, error={ERROR_LOG} → {DB_PATH} "
          f"（門檻={THRESHOLD}, 模式={WAF_MODE}）", flush=True)
    t = threading.Thread(target=tail, args=(AUDIT_LOG, handle_audit_line, con),
                         daemon=True)
    t.start()
    tail(ERROR_LOG, handle_error_line, con)   # 主執行緒跑 error_log


if __name__ == "__main__":
    main()
