# daily-feeds

日本語Tech記事とAI/LLM関連のRSS/Atomフィードを定期クロールし、Geminiで注目度をスコアリングして
GitHub Pages上に集約フィードとして公開するリポジトリ。

## 公開されるファイル (GitHub Pages)

| ファイル | 内容 |
|---|---|
| `docs/index.html` | 集約結果を読むタイムラインUI（フィルタ・検索・既読管理つき） |
| `docs/feed.xml` | 更新日時順のRSS 2.0フィード（最大 `max_items` 件） |
| `docs/feed-ranked.xml` | スコア順のRSS 2.0フィード（タイトルに `[score]` 付き、最大 `max_items` 件） |
| `docs/feed.json` | タイムラインUIのデータ源。**日付順・保持期間内をまとめて**出す（最大 `json_max_items` 件） |

公開URL: <https://otajisan.github.io/daily-feeds/> （タイムライン）、<https://otajisan.github.io/daily-feeds/feed.xml> など。
GitHub Pagesは `Access-Control-Allow-Origin: *` を返すため、別のフロントエンドから直接 `fetch` することもできる。

## タイムラインUI

`docs/index.html` は依存パッケージもビルド工程も持たない単一HTMLで、同一オリジンの `feed.json` を読んで描画する。

- 日付ごとにグルーピングしたタイムライン。スコアはバッジで色分け表示
- 絞り込み: キーワード検索 / ソース / カテゴリ / 最低スコア / 日付順⇔スコア順
- 既読管理: 記事を開くと既読になり減光表示。「未読のみ」で絞り込める。状態は `localStorage` にブラウザごとに保存される。記事がフィードから一時的に消えても既読は保たれ、45日経過したidだけ掃除される
- ダークモード（OS設定に追従、手動切替も可）
- `feed.json` の読み込みに失敗した場合はエラー表示と生フィードへのリンクを出す

RSS/Atom由来の文字列はすべて `textContent` で挿入し、リンクは `http(s)` のみ許可している。

## セットアップ

1. **Settings → Pages** で Source を `Deploy from a branch`、Branch を `main` / `/docs` に設定
2. [Google AI Studio](https://aistudio.google.com/) でAPIキーを取得し、
   **Settings → Secrets and variables → Actions** に `GEMINI_API_KEY` として登録
3. `feeds.yml` を編集して購読フィードと `settings.site_url` を調整
4. **Actions → Aggregate feeds → Run workflow** で初回実行(以後は2時間ごとに自動実行)

`GEMINI_API_KEY` が未設定でも動作し、その場合は全記事スコア50で集約だけ行われる。

## 仕組み

- `feeds.yml` — 購読フィード一覧と設定(保持日数、最大件数、モデル名など)。
  `max_items` はRSS2種、`json_max_items` は `feed.json` の上限
- `scripts/aggregate.py` — 取得 → 重複排除 → 新着のみGeminiでスコアリング → フィード生成
- `data/state.json` — スコア済み記事のキャッシュ。既出記事は再スコアリングしない(APIコスト削減)。
  30日より古いエントリは自動で削除される
- `.github/workflows/aggregate.yml` — 2時間ごと + 手動 + `feeds.yml`/`scripts/`変更時に実行し、
  生成物を `docs/` にコミットしてPagesへ反映。失敗時はトラッキングIssueに追記する

### 堅牢性まわりの挙動

- フィード取得は User-Agent 付き・タイムアウト20秒、一時的な失敗(429/5xx)は指数バックオフで2回リトライ。
  一部のフィードが落ちても残りで集約を続け、全滅した場合のみ異常終了して前回の出力を残す
- Gemini呼び出しも指数バックオフで3回リトライする。対象は408/425/429/5xxに加えて、
  コネクション断・空レスポンス・壊れたJSONといった一時的な失敗。逆にリクエスト内容が原因で
  投げ直しても直らない失敗(4xx、SAFETY等でのブロック、MAX_TOKENSでの切り詰め)は即座に諦める。
  失敗した記事は**キャッシュせず**スコア50で出力し、次回実行で再挑戦する
- 重複排除は `utm_*` などの追跡パラメータを除去した正規化URLで判定する
- 記事の日時はUTCとして解釈する(`calendar.timegm`)。未来日付の記事は現在時刻にクランプする

## ローカル実行

依存管理は [uv](https://docs.astral.sh/uv/)。Python のバージョンは `.python-version`、依存は `uv.lock` に固定されている。

```bash
uv sync                    # 依存をインストール (Python 3.12 も uv が用意する)

# APIキーは .env に置く (gitignore済み)
echo 'GEMINI_API_KEY=...' > .env
set -a; . ./.env; set +a

uv run python scripts/aggregate.py                # 通常実行
uv run python scripts/aggregate.py --dry-run      # ファイルを書かずに結果だけ確認
uv run python scripts/aggregate.py --no-gemini    # スコアリングせず全件50で動作確認
```

依存を追加するときは `uv add <パッケージ>`、開発用なら `uv add --dev <パッケージ>`。
`uv.lock` は必ずコミットする（CI は `uv sync --locked` でロックの鮮度ごと検証する）。

## テスト

タイムラインUIの操作テストをヘッドレスChromeで実行する。追加パッケージは不要で、
Node の組み込み `WebSocket` / `fetch` から Chrome DevTools Protocol を直接叩いている。

```bash
tests/browser/run.sh                              # docs/ を配信してChromeを起動し検証まで実行
SCREENSHOT_DIR=/tmp/shots tests/browser/run.sh    # スクリーンショットも保存する
```

Chromeのパスは自動検出する（macOSの `/Applications` 配下、または `google-chrome` / `chromium`）。
見つからない場合は `CHROME` 環境変数で指定する。ポートは `PORT` / `CDP_PORT` で変更できる。

## 注意事項

- `gemini-2.5-flash` は新規ユーザーには提供終了(404)。現在は `gemini-3.5-flash-lite` を使用している。
  精度優先なら `gemini-3.6-flash` に変更できるが、実測で1バッチ数十秒かかり503も発生した
- GitHub Actionsのcronは負荷状況で数分〜数十分遅れることがある
- 公開リポジトリで60日間コミットが無いとscheduleは自動停止される(このリポジトリは
  定期コミットが入るため実質問題にならない)
- Gemini APIの無料枠・レートリミットに注意。`score_batch_size` と実行間隔で調整する
- Hugging Face Blog / Google DeepMind はフィードに本文が無く、タイトルのみでスコアリングされる
