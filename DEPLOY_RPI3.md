# Raspberry Pi 3 部署指南

把 PiWAF Observatory 部署到 **Raspberry Pi 3 (B / B+)**。Pi 3 跟一般機器有三個關鍵差異，
照這份走才不會踩雷。

## ⚠ 先讀：Pi 3 的三個限制

1. **只有 1GB RAM** —— 最大瓶頸。BunkerWeb + Juice Shop + Streamlit 同時跑會吃緊，
   **一定要開 swap**（步驟 2）。
2. **必須用 64-bit (arm64) 作業系統** —— BunkerWeb 的容器映像只有 arm64，
   32-bit Raspberry Pi OS 跑不起來。
3. **用 Docker Compose v2（`docker compose`）** —— Pi 上用官方腳本裝，內建 v2 plugin，
   **不需要**開發機那個 `PYTHONNOUSERSITE=1` 前綴。

---

## 1. 燒錄 64-bit Raspberry Pi OS

用 Raspberry Pi Imager：

- OS 選 **Raspberry Pi OS (64-bit) — Lite**（無桌面版，省 RAM）
- 齒輪設定：開 SSH、設 hostname、Wi-Fi/帳號密碼
- 開機後 SSH 進去，確認是 64-bit：

```bash
uname -m        # 要顯示 aarch64（不是 armv7l）
```

> 若顯示 `armv7l`，表示燒成 32-bit，請重燒 64-bit 版。

## 2. 系統更新 + 開 swap（重要）

```bash
sudo apt update && sudo apt full-upgrade -y
```

**開 2GB swap（通用做法，任何發行版都可用）：**

```bash
swapon --show                 # 先看有沒有現成 swap；有 /swap.img 之類就只需擴大
sudo swapoff -a 2>/dev/null
sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
free -h                       # 確認 Swap 那行約 2.0Gi
grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

> **只有 Raspberry Pi OS (Raspbian)** 才有 `dphys-swapfile` 工具；那種系統可改用：
> `sudo dphys-swapfile swapoff` →
> 編輯 `/etc/dphys-swapfile` 設 `CONF_SWAPSIZE=2048`、`CONF_MAXSWAP=2048` →
> `sudo dphys-swapfile setup && sudo dphys-swapfile swapon`。
> Ubuntu Server / 其他系統沒有此工具，請用上面的 `/swapfile` 通用做法。

> 根目錄若是 **btrfs**，swapfile 需特別處理（`chattr +C`、不可壓縮）；ext4 直接照上面即可。

（可選）啟用 cgroup 記憶體控制，之後才能對容器設記憶體上限 —— 編輯 `/boot/firmware/cmdline.txt`
（舊版在 `/boot/cmdline.txt`），在**同一行結尾**加上：`cgroup_enable=memory cgroup_memory=1`，
然後 `sudo reboot`。

## 3. 安裝 Docker + Compose v2

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker            # 或登出再登入，讓群組生效

docker version
docker compose version   # 要看到 v2.x（注意是空格的 docker compose）
```

## 4. 取得專案 + 設定

```bash
git clone <你的-repo-url> piwaf-deploy
cd piwaf-deploy/piwaf        # 視 repo 結構，進到有 docker-compose.yml 的目錄
cp .env.example .env

hostname -I                  # 查 Pi 的 LAN IP，例如 192.168.1.50
```

編輯 `.env`：

```ini
BIND_ADDR=192.168.1.50:      # ← 改成上面查到的 Pi LAN IP，結尾保留冒號（鎖區網）
BW_HTTP_PORT=80              # Pi 上 80 沒被佔，可直接用 80
APP_DIRECT_PORT=3001
DASHBOARD_PORT=8501
```

## 5. 啟動（首次 build 會慢，耐心等）

```bash
docker compose up -d --build
```

- Pi 3 上 `dashboard` 要 pip 裝 streamlit/pandas（會抓 arm64 wheel，約 5–15 分）。
- `bunkerweb` / `juice-shop` 是直接拉 arm64 映像。
- `generator` / `ftw` 是 `tools` profile，**不會在這步 build**，等你第一次 `run` 才建。

確認都起來：

```bash
docker compose ps          # bunkerweb 要 healthy
free -h                    # 看 RAM/Swap 用量
```

## 6. 送測試流量 + 看儀表板

```bash
docker compose run --rm generator     # 會自動等 WAF 上線再打
```

瀏覽器（同網段的電腦）開：**`http://192.168.1.50:8501`**（換成你的 Pi IP）。

> go-ftw 全套約 7000 筆，Pi 3 上建議先縮範圍跑，省時間又省 RAM：
> `docker compose run --rm ftw run -d /tests --config /etc/ftw/config.yaml -i 942`

## 7. 防火牆（只准區網來源）

```bash
sudo apt install -y ufw
sudo ufw allow OpenSSH                                   # ⚠ 先放行 SSH，別把自己鎖在外面
sudo ufw allow from 192.168.1.0/24 to any port 80       # 換成你的網段
sudo ufw allow from 192.168.1.0/24 to any port 8501
sudo ufw allow from 192.168.1.0/24 to any port 3001
sudo ufw enable
sudo ufw status
```

## 8. 開機自動啟動

`docker-compose.yml` 各服務已設 `restart: unless-stopped`，重開機會自己回來。
確認 Docker 本身開機啟動：

```bash
sudo systemctl enable docker
```

---

## 疑難排解（Pi 3 常見）

| 症狀 | 原因 / 解法 |
|------|------------|
| 容器一直重啟、`dmesg` 看到 **OOM / Killed** | RAM 不夠。確認 swap 有開（步驟 2）；或見下方「省 RAM」 |
| `uname -m` 是 `armv7l`、映像 pull 失敗 `no matching manifest` | 燒成 32-bit 了，要重燒 **64-bit** OS |
| `docker compose` 找不到指令 | 用對版本：Pi 上是 `docker compose`（v2，空格），不是 `docker-compose` |
| build `dashboard` 很久 | 正常，Pi 3 慢；pandas/pyarrow 會抓 arm64 wheel 不會現編，等就好 |
| 瀏覽器連不到儀表板 | 確認 `.env` 的 `BIND_ADDR` 是 Pi 的 LAN IP；防火牆有放行該網段；用 `http://<pi-ip>:8501` 不是 localhost |

### 省 RAM：換掉吃資源的 Juice Shop（可選）

Juice Shop 是 Node.js，1GB 上很重。WAF 的偵測是在**請求進來時**就判定（攻擊 payload 在
WAF 端就回 403，根本到不了後端），所以後端換成極輕量的服務不影響「攻擊偵測」的展示
——只是「放行」的請求會回 404 而非 200。

若 swap 還是不夠，把 `juice-shop` 換成輕量、且 listen 在 3000 的後端即可（保持
`REVERSE_PROXY_HOST=http://juice-shop:3000` 不變）。先確認該映像有 arm64 再用。

### 把 build 移到電腦上做（進階，可選）

Pi 3 build 慢。可在 x86 電腦用 `docker buildx --platform linux/arm64` 建好映像、推到
registry 或 `docker save`/`load` 搬到 Pi，Pi 上就只 `docker compose up -d`（不加 `--build`）。
