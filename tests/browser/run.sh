#!/usr/bin/env bash
# docs/ を静的配信してヘッドレスChromeを起動し、tests/browser/verify.mjs を実行する。
#
# 使い方:
#   tests/browser/run.sh
#   SCREENSHOT_DIR=/tmp/shots tests/browser/run.sh   # スクリーンショットも保存する
#
# 環境変数: PORT / CDP_PORT / CHROME (Chromeバイナリのパス) / SCREENSHOT_DIR
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${PORT:-8931}"
CDP_PORT="${CDP_PORT:-9222}"

export PAGE_URL="http://127.0.0.1:${PORT}/"
export CDP_URL="http://127.0.0.1:${CDP_PORT}"

find_chrome() {
  if [ -n "${CHROME:-}" ]; then
    echo "$CHROME"
    return
  fi
  local mac="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  if [ -x "$mac" ]; then
    echo "$mac"
    return
  fi
  for c in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "$c" >/dev/null 2>&1; then
      echo "$c"
      return
    fi
  done
  echo "Chrome が見つかりません。CHROME 環境変数でパスを指定してください。" >&2
  exit 1
}

CHROME_BIN="$(find_chrome)"
PROFILE="$(mktemp -d)"
SERVER_PID=""
CHROME_PID=""

cleanup() {
  [ -n "$CHROME_PID" ] && kill "$CHROME_PID" 2>/dev/null || true
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
  # Chromeが終わりきる前に消すとプロファイルが書き戻されて rm が失敗する
  [ -n "$CHROME_PID" ] && wait "$CHROME_PID" 2>/dev/null || true
  [ -n "$SERVER_PID" ] && wait "$SERVER_PID" 2>/dev/null || true
  # 親を待っても、子プロセス(zygote等)がまだ profile に書いていることがある。
  # 数回リトライし、それでも残るなら諦める。一時ディレクトリなので実害は無い一方、
  # ここで失敗すると trap の終了ステータスがそのままスクリプトの失敗になってしまう
  for _ in 1 2 3 4 5; do
    rm -rf "$PROFILE" 2>/dev/null && break
    sleep 0.3
  done
}
trap cleanup EXIT

# 指定URLが 2xx を返すまで待つ。404 でも fetch は解決するので res.ok まで見る
wait_for() {
  node -e '
    const url = process.argv[1];
    const label = process.argv[2];
    (async () => {
      for (let i = 0; i < 80; i++) {
        try {
          const res = await fetch(url);
          if (res.ok) process.exit(0);
        } catch {}
        await new Promise((r) => setTimeout(r, 250));
      }
      console.error("timeout waiting for " + label + " (" + url + ")");
      process.exit(1);
    })();
  ' "$1" "$2"
}

# 既存の Chrome が CDP ポートを掴んでいると、そちらを操作してしまい
# 開発者の実プロファイルで localStorage.clear() を撃つことになる
if node -e 'fetch(process.argv[1]).then(() => process.exit(0), () => process.exit(1))' "${CDP_URL}/json/version" 2>/dev/null; then
  echo "ポート ${CDP_PORT} で既に何かが待ち受けています。CDP_PORT を変えて実行してください。" >&2
  exit 1
fi

echo "静的サーバを起動: ${PAGE_URL} (docs/)"
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$ROOT/docs" >/dev/null 2>&1 &
SERVER_PID=$!

echo "ヘッドレスChromeを起動: ${CHROME_BIN}"
"$CHROME_BIN" \
  --headless=new \
  --disable-gpu \
  --no-first-run \
  --no-default-browser-check \
  --disable-dev-shm-usage \
  --remote-debugging-port="$CDP_PORT" \
  --user-data-dir="$PROFILE" \
  about:blank >/dev/null 2>&1 &
CHROME_PID=$!

wait_for "${PAGE_URL}feed.json" "静的サーバ"
wait_for "${CDP_URL}/json/version" "CDP"

node "$ROOT/tests/browser/verify.mjs"
