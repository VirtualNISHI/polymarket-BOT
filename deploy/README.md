# Deploy

VPS デプロイ用テンプレート。`/srv/polymarket-smart-money/` にプロジェクトを置く前提。
パスが違う場合は cron / systemd ファイル内の `PROJECT_DIR` / `WorkingDirectory` を書き換えてください。

## 1. VPS 初期セットアップ

```bash
# 専用ユーザー(systemd を使う場合)
sudo useradd -r -m -d /srv/polymarket-smart-money -s /bin/bash smartmoney

# プロジェクト clone
sudo -u smartmoney git clone <repo> /srv/polymarket-smart-money
cd /srv/polymarket-smart-money

# 依存関係
sudo -u smartmoney python3.11 -m venv .venv
sudo -u smartmoney .venv/bin/pip install -e .

# .env を埋める(DISCORD_WEBHOOK_URL は必須)
sudo -u smartmoney cp .env.example .env
sudo -u smartmoney $EDITOR .env

# DB 初期化 + 初回ウォッチリスト構築(数分〜数十分)
sudo -u smartmoney .venv/bin/python -m src.db init
sudo -u smartmoney .venv/bin/python scripts/build_watchlist.py

# Discord 疎通確認
sudo -u smartmoney .venv/bin/python scripts/test_discord.py
```

## 2a. cron で運用する場合

```bash
sudo -u smartmoney crontab /srv/polymarket-smart-money/deploy/smart-money.cron
sudo -u smartmoney crontab -l   # 確認
```

ログ:
- `/srv/polymarket-smart-money/data/run.log`     ← 1時間ごと
- `/srv/polymarket-smart-money/data/refresh.log` ← 週次
- `/srv/polymarket-smart-money/data/build.log`   ← 月次

## 2b. systemd timer で運用する場合(推奨)

cron より柔軟で、`Persistent=true` により VPS 再起動後も追従します。

```bash
# ユニットを配置
sudo cp deploy/smart-money-*.{service,timer} /etc/systemd/system/

# 有効化
sudo systemctl daemon-reload
sudo systemctl enable --now smart-money-tracker.timer
sudo systemctl enable --now smart-money-refresh.timer
sudo systemctl enable --now smart-money-build.timer

# 状態確認
systemctl list-timers | grep smart-money
journalctl -u smart-money-tracker.service -n 50
```

手動実行:

```bash
sudo systemctl start smart-money-tracker.service
```

## 3. 監視

最低限のヘルスチェック:

```bash
# 過去 24h で取引が記録されているか
sqlite3 /srv/polymarket-smart-money/data/smart_money.db \
  "SELECT COUNT(*) FROM new_trades WHERE detected_at >= datetime('now','-24 hours');"

# poll_state が更新されているか
sqlite3 /srv/polymarket-smart-money/data/smart_money.db \
  "SELECT MAX(last_polled_at) FROM poll_state;"

# 直近のジョブ出力
tail -50 /srv/polymarket-smart-money/data/run.log
```

`run.log` に Polymarket の 429 が頻発する場合は SPEC §8.1 を参照して
ポーリング間隔を緩めてください。

## 4. 更新

```bash
cd /srv/polymarket-smart-money
sudo -u smartmoney git pull
sudo -u smartmoney .venv/bin/pip install -e .   # 依存変更があれば
# DB マイグレーションは `python -m src.db init` で冪等
sudo -u smartmoney .venv/bin/python -m src.db init
sudo systemctl restart smart-money-tracker.timer
```
