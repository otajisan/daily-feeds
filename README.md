# daily-feeds

日本語Tech記事とAI/LLM関連のRSS/Atomフィードを定期クロールし、Geminiで注目度をスコアリングして
GitHub Pages上に集約フィードとして公開するリポジトリ。

## 公開されるファイル (GitHub Pages)

| ファイル | 内容 |
|---|---|
| `docs/feed.xml` | 更新日時順のRSS 2.0フィード |
| `docs/feed-ranked.xml` | スコア順のRSS 2.0フィード(タイトルに `[score]` 付き) |
| `docs/feed.json` | アプリ消費用JSON(`score` / `reason` フィールド付き、スコア順) |

公開URL: <https://otajisan.github.io/daily-feeds/feed.xml> など。
GitHub Pagesは `Access-Control-Allow-Origin: *` を返すため、別リポジトリのフロントエンドから直接 `fetch` できる。

## セットアップ

1. **Settings → Pages** で Source を `Deploy from a branch`、Branch を `main` / `/docs` に設定
2. [Google AI Studio](https://aistudio.google.com/) でAPIキーを取得し、
   **Settings → Secrets and variables → Actions** に `GEMINI_API_KEY` として登録
3. `feeds.yml` を編集して購読フィードと `settings.site_url` を調整
4. **Actions → Aggregate feeds → Run workflow** で初回実行(以後は2時間ごとに自動実行)

`GEMINI_API_KEY` が未設定でも動作し、その場合は全記事スコア50で集約だけ行われる。

## 仕組み

- `feeds.yml` — 購読フィード一覧と設定(保持日数、最大件数、モデル名など)
- `scripts/aggregate.py` — 取得 → 重複排除 → 新着のみGeminiでスコアリング → フィード生成
- `data/state.json` — スコア済み記事のキャッシュ。既出記事は再スコアリングしない(APIコスト削減)。
  30日より古いエントリは自動で削除される
- `.github/workflows/aggregate.yml` — 2時間ごと + 手動 + `feeds.yml`/`scripts/`変更時に実行し、
  生成物を `docs/` にコミットしてPagesへ反映。失敗時はトラッキングIssueに追記する

### 堅牢性まわりの挙動

- フィード取得は User-Agent 付き・タイムアウト20秒、一時的な失敗(429/5xx)は指数バックオフで2回リトライ。
  一部のフィードが落ちても残りで集約を続け、全滅した場合のみ異常終了して前回の出力を残す
- Gemini呼び出しも指数バックオフで3回リトライする。対象は429/5xxに加えて、コネクション断・
  空レスポンス・壊れたJSONといった一時的な失敗。それでも失敗した記事は**キャッシュせず**
  スコア50で出力し、次回実行で再挑戦する
- 重複排除は `utm_*` などの追跡パラメータを除去した正規化URLで判定する
- 記事の日時はUTCとして解釈する(`calendar.timegm`)。未来日付の記事は現在時刻にクランプする

## ローカル実行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# APIキーは .env に置く (gitignore済み)
echo 'GEMINI_API_KEY=...' > .env
set -a; . ./.env; set +a

.venv/bin/python scripts/aggregate.py                    # 通常実行
.venv/bin/python scripts/aggregate.py --dry-run          # ファイルを書かずに結果だけ確認
.venv/bin/python scripts/aggregate.py --no-gemini        # スコアリングせず全件50で動作確認
```

## 注意事項

- `gemini-2.5-flash` は新規ユーザーには提供終了(404)。現在は `gemini-3.5-flash-lite` を使用している。
  精度優先なら `gemini-3.6-flash` に変更できるが、実測で1バッチ数十秒かかり503も発生した
- GitHub Actionsのcronは負荷状況で数分〜数十分遅れることがある
- 公開リポジトリで60日間コミットが無いとscheduleは自動停止される(このリポジトリは
  定期コミットが入るため実質問題にならない)
- Gemini APIの無料枠・レートリミットに注意。`score_batch_size` と実行間隔で調整する
- Hugging Face Blog / Google DeepMind はフィードに本文が無く、タイトルのみでスコアリングされる
