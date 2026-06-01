#!/usr/bin/env python3
"""可重現的測試流量情境產生器。

每個請求都帶 X-PiWAF-Scenario header，parser 會把它記進 SQLite，
之後就能在儀表板用 scenario 篩選、驗證 WAF 是否正確偵測。
"""
import os
import time

import requests

TARGET = os.environ.get("TARGET", "http://bunkerweb:8080").rstrip("/")
ROUNDS = int(os.environ.get("ROUNDS", "3"))
DELAY = float(os.environ.get("DELAY", "0.3"))

# (scenario, method, path, 預期是否為攻擊)
SCENARIOS = [
    ("normal_browsing", "GET", "/", False),
    ("normal_browsing", "GET", "/#/search", False),
    ("normal_browsing", "GET", "/rest/products/search?q=apple", False),
    ("scanner_like_behavior", "GET", "/admin", True),
    ("scanner_like_behavior", "GET", "/.env", True),
    ("scanner_like_behavior", "GET", "/wp-login.php", True),
    ("scanner_like_behavior", "GET", "/phpmyadmin/", True),
    ("sqli_detection_test", "GET", "/rest/products/search?q=apple' OR '1'='1", True),
    ("sqli_detection_test", "GET", "/rest/products/search?q=1 UNION SELECT username,password FROM users--", True),
    ("xss_detection_test", "GET", "/#/search?q=<script>alert(1)</script>", True),
    ("xss_detection_test", "GET", "/?name=<img src=x onerror=alert(1)>", True),
    ("path_traversal_detection_test", "GET", "/../../../../etc/passwd", True),
    ("path_traversal_detection_test", "GET", "/ftp/..%2f..%2f..%2fetc%2fpasswd", True),
    ("login_abuse_simulation", "POST", "/rest/user/login", True),
]


def send(scenario, method, path, expect_attack):
    # 把情境標籤帶進 URL（pscn），讓它出現在 nginx error_log 的 request 行，
    # parser 才能在「被擋的攻擊」上也標出 scenario（那些不會進 audit log）。
    sep = "&" if "?" in path else "?"
    path = f"{path}{sep}pscn={scenario}"
    url = TARGET + path
    headers = {"X-PiWAF-Scenario": scenario, "User-Agent": "PiWAF-Generator/1.0"}
    try:
        if method == "POST":
            r = requests.post(url, headers=headers,
                              json={"email": "a' OR 1=1--", "password": "x"},
                              timeout=5)
        else:
            r = requests.get(url, headers=headers, timeout=5)
        flag = "🚫" if r.status_code == 403 else "✅"
        tag = "[attack]" if expect_attack else "[normal]"
        print(f"{flag} {r.status_code} {tag:9} {scenario:30} {method} {path}", flush=True)
    except requests.RequestException as e:
        print(f"⚠  ERR  {scenario:30} {method} {path} → {e}", flush=True)


def wait_for_waf():
    """等 BunkerWeb scheduler 把 WAF 設定推上線：用一發 SQLi 探測直到回 403。
    （Direct Mode 直打測試站不會 403，最多等 WAIT_SECS 秒就放行。）"""
    if os.environ.get("WAIT_FOR_WAF", "yes").lower() in ("no", "0", "false"):
        return
    probe = TARGET + "/?q=1'+OR+'1'='1&pscn=_probe"
    deadline = int(os.environ.get("WAIT_SECS", "120"))
    waited = 0
    while waited < deadline:
        try:
            if requests.get(probe, headers={"Host": "piwaf.local"}, timeout=5).status_code == 403:
                print(f"[generator] WAF 已就緒（等了 {waited}s）", flush=True)
                return
        except requests.RequestException:
            pass
        print(f"[generator] 等 WAF 上線... {waited}s", flush=True)
        time.sleep(3)
        waited += 3
    print("[generator] 等逾時，仍繼續（可能是 Direct Mode）", flush=True)


def main():
    print(f"[generator] 目標 = {TARGET}，回合 = {ROUNDS}", flush=True)
    wait_for_waf()
    for i in range(1, ROUNDS + 1):
        print(f"\n── 第 {i}/{ROUNDS} 回合 ──", flush=True)
        for sc in SCENARIOS:
            send(*sc)
            time.sleep(DELAY)
    print("\n[generator] 完成。打開儀表板看結果。", flush=True)


if __name__ == "__main__":
    main()
