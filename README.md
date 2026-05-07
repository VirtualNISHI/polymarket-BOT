# Polymarket Smart Money Bot

Polymarket で一貫して勝っている「常勝ウォレット」の新規ポジションを追跡し Discord に通知するボット。
仮想NISHI 配信の **Phase 2**。Whale Alerts(Phase 1)が安定稼働してから着手。

## このプロジェクトの目的

- Polymarket のスマートマネー(常勝ウォレット 20–50件)をウォッチリスト化
- ウォッチリストの新規取引を継続ポーリング → Discord 配信
- 複数の常勝ウォレットが同マーケット同方向に賭けたら強調アラート
- データソースは **Polymarket 公開 API**(Gamma + Data API)— API キー不要・無料
- 「Polymarket スマートマネー分析」を、日本では誰もやっていないポジションで確立する

## アーキテクチャ

```
[初回 / 月次: ウォッチリスト構築バッチ]
   ↓ prediction_market_pnl_leaderboard を主要マーケット横断で取得
   ↓ 複数マーケット上位常連を抽出 → SQLite に保存
   
[VPS cron (1時間間隔)]
   ↓
[ウォッチリスト全ウォレットの prediction_market_address_trades をポーリング]
   ↓
[前回ポーリング以降の新規取引を抽出]
   ↓
[同マーケット同方向の集中検知(複数ウォレット)]
   ↓
[Discord Webhook POST(個別 / 集中検知の 2 種類)]
```

## ディレクトリ構成

```
smart-money/
├── README.md
├── SPEC.md
├── pyproject.toml
├── .env.example
├── config/
│   └── settings.yaml
├── src/
│   ├── __init__.py
│   ├── polymarket_client.py   # Gamma + Data API クライアント
│   ├── discord_client.py
│   ├── db.py
│   ├── watchlist_builder.py   # 常勝ウォレット抽出
│   ├── tracker.py             # 新規取引検知
│   ├── convergence.py         # 複数ウォレット同方向検知
│   ├── formatter.py
│   └── job.py
├── scripts/
│   ├── build_watchlist.py     # 初回 / 月次実行
│   ├── run.py                 # cron から呼ぶ(1h ごと)
│   └── refresh_pnl.py         # ウォッチリストの PnL 再計算(週次)
└── data/
    └── smart_money.db
```

## セットアップ(VPS)

1. `git clone` してこのディレクトリへ
2. `python3.11 -m venv .venv && source .venv/bin/activate && pip install -e .`
3. `cp .env.example .env` して値を埋める
4. DB 初期化: `python -m src.db init`
5. **初回ウォッチリスト構築**: `python scripts/build_watchlist.py`
   - ここで対象マーケット 10–30件を横断スキャンして常勝ウォレット候補を抽出
   - 結果を確認して、`watchlist` テーブルの `enabled` カラムを手動調整可能
6. dry-run: `python scripts/run.py --dry-run`
7. cron 登録:
   ```
   0 * * * * cd /path/to/smart-money && .venv/bin/python scripts/run.py >> data/run.log 2>&1
   0 3 * * 0 cd /path/to/smart-money && .venv/bin/python scripts/refresh_pnl.py >> data/refresh.log 2>&1
   1 4 1 * * cd /path/to/smart-money && .venv/bin/python scripts/build_watchlist.py >> data/build.log 2>&1
   ```

## 開発フェーズ

詳細は `SPEC.md`。実装順:

1. DB スキーマ
2. Polymarket クライアント(Gamma + Data API のラッパ)
3. Discord クライアント
4. **ウォッチリストビルダー**(最重要 — 精度がプロジェクト全体の命)
5. トラッカー(新規取引検知)
6. コンバージェンス検知(複数ウォレット集中)
7. Embed 整形
8. ジョブ統合

## 重要な設計判断

### ウォッチリスト構築の方針

「単一マーケットでたまたま勝った人」ではなく「**複数マーケットで継続的に勝っている人**」を抽出するのが核。

具体的には:

- スキャン対象マーケット 10–30 件(政治、暗号、マクロ系のメジャーマーケット)
  - Gamma API `/markets?tag_slug=politics&closed=true&order=volumeNum` 等で出来高上位を取得
- 各マーケットで Data API `/positions?market={conditionId}&sortBy=CASHPNL` の上位 50 件を取得
- 複数マーケットで上位 50 入りしたウォレットを候補にする(2 マーケット以上を初期閾値)
- 累計 PnL、勝率、取引マーケット数で最終スコアリング
- 上位 20–50 件をウォッチリスト確定

「複数銘柄での実績重視」は Nansen の Smart Money 抽出ロジックの発想を Polymarket 公開データに移植したもの。

### コンバージェンス検知

複数の常勝ウォレットが同マーケット・同方向に賭けた場合、シグナル強度が個別アラートより高い。検知条件:

- 直近 24 時間以内に
- 3 件以上の常勝ウォレットが
- 同一マーケットの同一サイドに
- 累計 $50k 以上のポジション

を取った場合、`#smart-money-poly` に `@here` 付きで強調投稿。

## 関連プロジェクト

- `whale-alerts/` — Phase 1: 大口・板異常速報(本プロジェクトの前段)
- `macro-divergence/` — Phase 3: 確率 vs 実勢乖離分析
