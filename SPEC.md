# Smart Money — 詳細仕様

Claude Code はこのファイルを読んで実装を進める。

## 1. ウォッチリスト構築ロジック(`watchlist_builder.py`)

### 1.1 候補プール取得

`scripts/build_watchlist.py` 実行時、Polymarket 公式プロフィット・リーダーボード
(`https://lb-api.polymarket.com/profit?window=All`)で **全期間プロフィット上位 N 人**
(初期値 200)を取得する。

> **設計判断:** 当初は per-market `/holders` を横断スキャンする方針だったが、
> `/holders` は **シェア数順の現保有者** を返すため、すでに利確して退場した本物の
> スマートマネー(Theo4 等)を取り逃すことが実験で判明した(50 件取れず、
> 1 件のみヒット → SPEC §8.4 既知の課題に追記)。Polymarket 公式リーダーボードは
> リアライズド・プロフィット順なので、本来欲しい母集団そのもの。

### 1.2 候補ウォレットの補完

リーダーボードからは `(address, lifetime_pnl_usd, name/pseudonym)` が取れる。
各ウォレットについて以下を補完:

- `/trades?user={addr}&limit=500` を呼び、生涯の取引履歴を取得
- `market_appearances` = 取引履歴に現れる distinct conditionId 数
- `cumulative_volume_usd` = 各取引の `size × price` の累計(直近 500 件分)
- `win_rate` = リーダーボード上位入りの定義上必ず profitable なので **1.0 を初期値**
  として置く(per-market 勝率の正確な計算は §8.2 参照、将来課題)

### 1.3 スコアリング

```python
score = (
    cumulative_pnl_usd * 0.4 +
    win_rate * 1_000_000 * 0.3 +
    market_appearances * 100_000 * 0.3
)
```

このスコアの上位 50 件を `watchlist` に登録(`enabled=True`)。
スコア重みは `config/settings.yaml` で調整可能。

スコア例(実データ):

| ウォレット | PnL | Markets | Score |
|---|---|---|---|
| Theo4 (0x5668…) | $22.05M | 14 | 9.54M |
| 多市場アクティブ #1 (0xbddf…) | $2.68M | 334 | 11.39M |
| Fredi9999 (0x1f2d…) | $16.62M | 25 | 7.70M |

### 1.4 手動調整

ビルド完了後、CLI で確認できるサマリー出力を行う:

```
Top 50 candidates:
1. 0xbddf61af... | PnL: $2,684,640 | WinRate: 100% | Markets: 334 | Score: 11,393,856
2. 0xd38b71f3... | PnL: $2,673,262 | WinRate: 100% | Markets: 329 | Score: 11,239,305
...
```

ユーザーがこの結果を見て、`watchlist` テーブルの `enabled` を手動編集できるよう、
シンプルな SQL で済むスキーマにする(例: `UPDATE watchlist SET enabled=FALSE
WHERE wallet_address='0x...';`)。

## 2. トラッキングロジック(`tracker.py`)

### 2.1 ポーリング

cron で 1 時間ごとに実行:

1. `watchlist` から `enabled=True` のウォレットを取得
2. 各ウォレットで `prediction_market_address_trades` を呼ぶ
3. `poll_state` テーブルに記録された前回最終取引時刻以降の取引を新規として抽出
4. 各新規取引を `new_trades` テーブルに INSERT
5. `poll_state` を更新

### 2.2 個別通知判定

新規取引のうち、以下の条件で Discord 通知:

- 取引額が `min_notify_usd` 以上(初期値 $10,000、Whale Alerts より低めに設定)
- または、ウォッチリスト内のスコア上位 10 件のウォレットの取引(額に関わらず)

ノイズ低減のため、同一ウォレット・同一マーケット・同一サイドの連続取引は 1 時間以内なら集約して 1 回通知。

## 3. コンバージェンス検知(`convergence.py`)

### 3.1 検知条件

直近 24 時間の `new_trades` を SQL で集計:

```sql
SELECT
    market_id,
    market_question,
    side,
    COUNT(DISTINCT wallet_address) AS wallet_count,
    SUM(amount_usd) AS total_amount,
    GROUP_CONCAT(wallet_address) AS wallets
FROM new_trades
WHERE detected_at >= datetime('now', '-24 hours')
GROUP BY market_id, side
HAVING wallet_count >= 3 AND total_amount >= 50000;
```

### 3.2 重複防止

`convergence_alerts` テーブルに `(market_id, side, last_alerted_at)` を記録。同一の市場・サイドに対する再アラートは、ウォレット数が前回より 1 件以上増えた場合のみ。

## 4. SQLite スキーマ

```sql
-- ウォッチリスト
CREATE TABLE watchlist (
    wallet_address TEXT PRIMARY KEY,
    score REAL NOT NULL,
    cumulative_pnl_usd REAL,
    win_rate REAL,
    market_appearances INTEGER,
    cumulative_volume_usd REAL,
    enabled BOOLEAN DEFAULT TRUE,
    note TEXT,                    -- 手動メモ
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ウォッチリスト構築履歴(変遷を残す)
CREATE TABLE watchlist_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    wallet_count INTEGER,
    scanned_markets INTEGER,
    note TEXT
);

-- 新規取引(検知済み)
CREATE TABLE new_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT UNIQUE,
    wallet_address TEXT NOT NULL,
    market_id TEXT NOT NULL,
    market_question TEXT,
    side TEXT NOT NULL,           -- 'YES' | 'NO'
    amount_usd REAL,
    entry_price REAL,
    probability_at_trade REAL,
    traded_at TIMESTAMP,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notified_individually BOOLEAN DEFAULT FALSE,
    raw_payload TEXT,
    FOREIGN KEY (wallet_address) REFERENCES watchlist(wallet_address)
);

CREATE INDEX idx_new_trades_market ON new_trades(market_id, side);
CREATE INDEX idx_new_trades_detected ON new_trades(detected_at);
CREATE INDEX idx_new_trades_wallet ON new_trades(wallet_address);

-- ポーリング状態(ウォレットごと)
CREATE TABLE poll_state (
    wallet_address TEXT PRIMARY KEY,
    last_polled_at TIMESTAMP,
    last_seen_trade_at TIMESTAMP,
    last_seen_trade_id TEXT,
    FOREIGN KEY (wallet_address) REFERENCES watchlist(wallet_address)
);

-- コンバージェンスアラート
CREATE TABLE convergence_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    market_question TEXT,
    side TEXT NOT NULL,
    wallet_count INTEGER,
    total_amount_usd REAL,
    wallets TEXT,                 -- JSON array
    last_alerted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(market_id, side)
);
```

## 5. Discord Embed フォーマット

### 5.1 個別取引通知

```
🎯 Smart Money Trade | Polymarket
━━━━━━━━━━━━━━━━━━━━
Market: {market_question}
Trade: {side} ${amount_usd:,.0f} @ ${entry_price}
Wallet: {wallet_short} (Score Rank #{rank})
  Cumulative PnL: ${cum_pnl:,.0f} | Win Rate: {win_rate:.0%}
  Active Markets: {market_count}
[View Market]({market_url}) | [View Wallet]({nansen_url})
```

色: `0x9b59b6`(紫、Smart Money 専用)

### 5.2 コンバージェンス通知(@here 付き)

```
🚨 SMART MONEY CONVERGENCE 🚨
━━━━━━━━━━━━━━━━━━━━
Market: {market_question}
Direction: {side} | Total: ${total_amount:,.0f}
{wallet_count} smart wallets in the last 24h:

  • {wallet_1_short} ${amount_1:,.0f}
  • {wallet_2_short} ${amount_2:,.0f}
  • {wallet_3_short} ${amount_3:,.0f}
  ...

Current Probability: {current_prob}%
[View Market]({market_url})
```

色: `0xe74c3c`(赤、最重要)
コンテンツ部分に `@here` を含める(設定で無効化可能)。

## 6. 設定ファイル(config/settings.yaml)

```yaml
watchlist_builder:
  scan_categories:
    - Crypto
    - Politics
    - Macro
  markets_per_category: 10
  leaderboard_top_n: 50
  min_market_appearances: 2
  min_cumulative_pnl_usd: 100000
  watchlist_size: 50
  scoring:
    pnl_weight: 0.4
    win_rate_weight: 0.3
    market_count_weight: 0.3

tracker:
  poll_interval_minutes: 60
  min_notify_usd: 10000
  always_notify_top_rank: 10     # スコア上位 N 件は額に関わらず通知
  consolidation_window_minutes: 60

convergence:
  lookback_hours: 24
  min_wallet_count: 3
  min_total_amount_usd: 50000
  enable_here_mention: true

refresh_pnl:
  schedule: weekly
  rebuild_threshold_score_drop: 0.3   # 30% 以上スコア低下したら除外候補
```

## 7. 環境変数(.env)

```
DISCORD_WEBHOOK_URL=                 # #smart-money-poly チャンネル(必須)
POLYMARKET_USER_AGENT=polymarket-smart-money/0.1   # 任意・省略可
LOG_LEVEL=INFO
DB_PATH=./data/smart_money.db
DRY_RUN=false
```

データ取得は Polymarket 公開 API(Gamma + Data API)を使用。API キー不要。
- `https://gamma-api.polymarket.com/markets` — マーケット screener / 現価
- `https://data-api.polymarket.com/positions` — マーケット別 PnL リーダーボード相当
- `https://data-api.polymarket.com/trades` — ウォレット別取引履歴

## 8. 実装上の注意

### 8.1 API コール量

ウォッチリスト 50 件 × 1 時間ごと = 1 日 1,200 コール。Polymarket Data API は公開で無料だが
非公開のレート制限がある(経験則で数百 RPM 程度)。429 を観測したら:

- ポーリング間隔を 2–4 時間に延長
- スコア上位 20 件のみ 1 時間ポーリング、残りは 6 時間ポーリングなどの段階制
- httpx のリトライ(tenacity 既設定)でバックオフ

### 8.2 ウォレット属性更新と win_rate

ウォッチリストの累計 PnL は時間とともに変化する。`refresh_pnl.py` で週次に再計算:

- リーダーボードを再取得して各ウォレットの最新 PnL を反映
- スコアが大幅低下(`rebuild_threshold_score_drop` 以上)したら `enabled=False` に
- 新規候補が出てきたら月次の `build_watchlist` でリビルドして取り込む

**win_rate の正確な計算は将来課題**。理由:
- `/positions` は現時点保有のみを返す(完全利確済みウォレットは空)
- 生涯勝率を計算するには「ウォレットの全取引 + 各マーケットの解決結果 + 各取引の
  ネットコスト」をクロスする必要があり、Polymarket 公開 API ではマーケット数 ×
  Gamma `/markets?condition_ids=…` の問い合わせが必要
- 現状はリーダーボード上位入り = profitable という事実に基づき `win_rate=1.0`
  プレースホルダで運用 → スコアの **win_rate × 1M × 0.3** の項は全員同じ寄与
- 改善方針: `/trades?user=X` の全件をマーケットごとにグループ化し、Gamma の
  resolution outcome を引いて per-market realized PnL を計算 → wins / total

### 8.3 Whale Alerts / Nansen との連携(将来)

- Whale Alerts 側で検知されたウォレットを Smart Money 候補としてプール、月次ビルド時に追加候補としてスキャン。
- Nansen を「ウォレットタグ付けエンリッチメント」として後付けで併用可能(取引検知後に Nansen でタグだけ問い合わせる、など)。本プロジェクトのコア検知は Polymarket 公開 API のみで完結。

### 8.4 既知の課題

- Polymarket のウォレットは TradFi 的な意味での「ファンド」ではないため、ロットや戦略が読みにくい個人ウォレットも混じる
- **インサイダー疑惑のあるウォレット**(過去に Trump 投稿前に大量ポジション等)が紛れる可能性がある。配信時には「インサイダー疑惑あり」のフラグ付与機能を将来検討
- Polymarket は決済前に大きな価格変動が起きうるので、PnL 評価は決済後ベースを優先
- **per-market `/holders` 起点では smart money が拾えない**(§1.1 で詳述) → 公式リーダーボード起点に切り替え済み
- `win_rate` は現状 1.0 固定(§8.2)。スコアの差別化は PnL と market_count に依存
- `cumulative_volume_usd` は直近 500 取引のみの累計(`/trades` の API 上限)。生涯ボリュームではない

## 9. 後続タスク

- インサイダー疑惑フラグ機能(過去事例 DB との照合)
- ウォレット属性の暗号資産取引履歴とのクロス分析(Nansen 通常 API 経由)
- 月次 / 週次 サマリーレポート自動生成(CoinPost 寄稿素材化)
