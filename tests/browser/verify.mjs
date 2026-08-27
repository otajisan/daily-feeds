// docs/index.html を実ブラウザ(headless Chrome + CDP)で操作して検証する。
//
// 依存パッケージなし。Node 24 の組み込み WebSocket / fetch から Chrome DevTools Protocol を直接叩く。
// 起動と後始末は tests/browser/run.sh がやるので、通常はそちらから実行する。
//
// 環境変数:
//   CDP_URL       CDP のエンドポイント (既定 http://127.0.0.1:9222)
//   PAGE_URL      検証対象のページ     (既定 http://127.0.0.1:8931/)
//   SCREENSHOT_DIR 指定するとスクリーンショットを保存する (未指定なら撮らない)
import { mkdirSync, writeFileSync } from "node:fs";

const CDP = process.env.CDP_URL || "http://127.0.0.1:9222";
const PAGE = process.env.PAGE_URL || "http://127.0.0.1:8931/";
const OUT = process.env.SCREENSHOT_DIR || "";
if (OUT) mkdirSync(OUT, { recursive: true });

// 件数はフィードから取る。記事は毎回入れ替わるのでハードコードしてはいけない
const TOTAL = (await (await fetch(new URL("feed.json", PAGE))).json()).items.length;
console.log("feed.json の件数:", TOTAL);

let ws, nextId = 1;
const pending = new Map();
const events = [];

function send(method, params = {}, sessionId) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
}

const evaluate = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "eval failed");
  return r.result.value;
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, label, timeout = 15000) {
  const started = Date.now();
  for (;;) {
    if (await fn()) return;
    if (Date.now() - started > timeout) throw new Error("timeout waiting for " + label);
    await sleep(150);
  }
}

async function shot(name, full = false) {
  if (!OUT) return null;
  const r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: full });
  const path = `${OUT}/${name}.png`;
  writeFileSync(path, Buffer.from(r.data, "base64"));
  return path;
}

const results = [];
function check(label, actual, expected) {
  const pass = typeof expected === "function" ? expected(actual) : JSON.stringify(actual) === JSON.stringify(expected);
  results.push({ pass, label, actual });
  console.log(`${pass ? "ok  " : "NG  "} ${label} -> ${JSON.stringify(actual)}`);
}

const shown = () => evaluate('document.querySelectorAll(".item").length');
const days = () => evaluate('document.querySelectorAll(".day").length');
const status = () => evaluate('document.getElementById("status").textContent');

// ---------------------------------------------------------------- main

const target = await (await fetch(`${CDP}/json/new?${encodeURIComponent(PAGE)}`, { method: "PUT" })).json();
ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
  } else if (msg.method) {
    events.push(msg);
  }
};

await send("Page.enable");
await send("Runtime.enable");
await send("Log.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1000, height: 1300, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: PAGE });
await waitFor(async () => (await shown()) > 0, "初期描画");
// 前回実行の保存状態を消してから初期状態を確認する
await evaluate('localStorage.clear()');
await send("Page.navigate", { url: PAGE });
await waitFor(async () => (await shown()) > 0, "初期描画(クリーン)");

// --- 初期状態
check(`初期表示件数 = ${TOTAL}`, await shown(), TOTAL);
check("日付グループが複数", await days(), (n) => n > 5);
check("ステータスに最終更新と件数", await status(), (s) => s.includes("最終更新") && s.includes(`全${TOTAL}件中 ${TOTAL}件を表示`));
check("スコアバッジの色分けが5段階そろう",
  await evaluate('[...new Set([...document.querySelectorAll(".score")].map(n=>n.className.replace("score ","")))].sort()'),
  (v) => v.length >= 3);
check("タイトルは新規タブで開く",
  await evaluate('[...document.querySelectorAll(".title")].every(a=>a.target==="_blank"&&a.rel.includes("noopener"))'), true);
check("リンクは http(s) のみ",
  await evaluate('[...document.querySelectorAll(".title")].every(a=>a.protocol==="https:"||a.protocol==="http:")'), true);

// --- sticky の重なり
await evaluate('window.scrollTo(0, 2500)');
await sleep(400);
check("スクロール中も日付見出しがコントロールに隠れない",
  await evaluate(`(() => {
    const c = document.getElementById("controls").getBoundingClientRect();
    const heads = [...document.querySelectorAll(".day-head")].map(h => h.getBoundingClientRect());
    const stuck = heads.filter(h => h.top < c.bottom + 4 && h.bottom > 0);
    return stuck.length === 0 || stuck.every(h => h.top >= c.bottom - 1);
  })()`), true);
await evaluate('window.scrollTo(0, 0)');
await sleep(200);

// --- 検索
await evaluate('(()=>{const q=document.getElementById("q"); q.value="duckdb"; q.dispatchEvent(new Event("input"))})()');
await sleep(300);
const searched = await shown();
check("検索 'duckdb' で絞り込まれる", searched, (n) => n > 0 && n < TOTAL);
check("検索結果は全件がduckdbを含む",
  await evaluate('[...document.querySelectorAll(".item")].every(n=>n.textContent.toLowerCase().includes("duckdb"))'), true);
await evaluate('(()=>{const q=document.getElementById("q"); q.value=""; q.dispatchEvent(new Event("input"))})()');
await sleep(300);
check("検索クリアで全件に戻る", await shown(), TOTAL);
await evaluate('(()=>{const q=document.getElementById("q"); q.value="duckdb"; q.dispatchEvent(new Event("input"))})()');
await sleep(300);
await send("Page.navigate", { url: PAGE });
await waitFor(async () => (await shown()) > 0, "検索後のリロード");
check("検索語はリロードで持ち越さない", await shown(), TOTAL);

// --- 最低スコア
await evaluate('(()=>{const s=document.getElementById("min-score"); s.value="70"; s.dispatchEvent(new Event("input"))})()');
await sleep(150);
check("最低スコア70で絞り込み", await shown(), (n) => n > 0 && n < TOTAL);
check("表示された全件が70点以上",
  await evaluate('[...document.querySelectorAll(".score")].every(n=>Number(n.textContent)>=70)'), true);
check("スライダー表示値が70", await evaluate('document.getElementById("min-score-out").textContent'), "70");
await evaluate('(()=>{const s=document.getElementById("min-score"); s.value="0"; s.dispatchEvent(new Event("input"))})()');
await sleep(150);

// --- ソースチップ
await evaluate('[...document.querySelectorAll(".chip")].find(c=>c.textContent.includes("Publickey")).click()');
await sleep(150);
check("ソース絞り込み(Publickey)",
  await evaluate('[...document.querySelectorAll(".meta .src")].every(n=>n.textContent==="Publickey")'), true);
check("チップが押下状態",
  await evaluate('[...document.querySelectorAll(".chip")].find(c=>c.textContent.includes("Publickey")).getAttribute("aria-pressed")'), "true");
await evaluate('[...document.querySelectorAll(".chip")].find(c=>c.textContent.includes("Publickey")).click()');
await sleep(150);

// --- カテゴリ
await evaluate('(()=>{const c=document.getElementById("category"); c.value="ai"; c.dispatchEvent(new Event("change"))})()');
await sleep(150);
check("カテゴリ絞り込み(ai)",
  await evaluate('[...document.querySelectorAll(".meta .cat")].every(n=>n.textContent==="ai")'), true);
await evaluate('(()=>{const c=document.getElementById("category"); c.value=""; c.dispatchEvent(new Event("change"))})()');
await sleep(150);

// --- 並び替え
await evaluate('document.getElementById("sort-score").click()');
await sleep(200);
check("スコア順が降順",
  await evaluate('(()=>{const v=[...document.querySelectorAll(".score")].map(n=>Number(n.textContent));return v.every((x,i)=>i===0||v[i-1]>=x)})()'), true);
check("スコア順では日付見出しを出さない(グルーピングが成立しないため)",
  await evaluate('document.querySelectorAll(".day-head").length'), 0);
check("スコア順でも全件表示される", await shown(), TOTAL);
await evaluate('document.getElementById("sort-date").click()');
await sleep(200);
check("日付順に戻すと日付見出しが復活",
  await evaluate('document.querySelectorAll(".day-head").length'), (n) => n > 5);
check("日付見出しの件数表示が正しい",
  await evaluate(`(() => {
    return [...document.querySelectorAll(".day")].every(d => {
      const label = d.querySelector(".day-head .n");
      const n = d.querySelectorAll(".item").length;
      return label && label.textContent === n + "件";
    });
  })()`), true);
check("日付順が降順",
  await evaluate('(()=>{const t=[...document.querySelectorAll("time")].map(n=>n.dateTime);return t.every((x,i)=>i===0||t[i-1]>=x)})()'), true);

// --- 既読管理
await evaluate('document.querySelector(".item .mark").click()');
await sleep(150);
check("既読化でクラスが付く", await evaluate('document.querySelector(".item").classList.contains("is-read")'), true);
check("ボタン文言が切り替わる", await evaluate('document.querySelector(".item .mark").textContent'), "未読に戻す");
check("localStorageに保存される",
  await evaluate('Object.keys(JSON.parse(localStorage.getItem("daily-feeds:read:v1")||"{}")).length'), 1);
await evaluate('document.getElementById("unread").click()');
await sleep(200);
check("未読のみ表示で1件減る", await shown(), TOTAL - 1);

// --- リロード後の永続化
await send("Page.navigate", { url: PAGE });
await waitFor(async () => (await shown()) > 0, "リロード後の描画");
check("リロード後も未読のみが維持", await shown(), TOTAL - 1);
check("未読のみボタンが押下状態", await evaluate('document.getElementById("unread").getAttribute("aria-pressed")'), "true");
await evaluate('document.getElementById("unread").click()');
await sleep(200);
check("未読のみ解除で既読カードが戻る", await evaluate('document.querySelectorAll(".item.is-read").length'), 1);

// --- 既読を消去
await evaluate('document.getElementById("clear-read").click()');
await sleep(200);
check("既読消去で0件", await evaluate('document.querySelectorAll(".item.is-read").length'), 0);

// 指摘2: 未読のみ表示中に既読化しても再描画せず、クリックしたカードがDOMから消えない
check("未読のみ表示中の既読化で件数が変わらない", await evaluate(`(() => {
  const before = document.querySelectorAll(".item").length;
  const li = document.querySelectorAll(".item")[5];
  const id = li.dataset.id;
  li.querySelector(".mark").click();
  const after = document.querySelectorAll(".item").length;
  const same = document.querySelector('[data-id="' + CSS.escape(id) + '"]');
  return before === after && !!same && same.classList.contains("is-read");
})()`), true);
await evaluate('(()=>{const li=document.querySelectorAll(".item")[5]; li.querySelector(".mark").click()})()');
await sleep(150);

// 指摘5: 範囲外の保存値でもUIと絞り込みが食い違わない
await evaluate('localStorage.setItem("daily-feeds:prefs:v1", JSON.stringify({sources:[],category:"",minScore:9999,sort:"nonsense",unreadOnly:false,theme:"evil"}))');
await send("Page.navigate", { url: PAGE });
await waitFor(async () => (await shown()) > 0, "範囲外設定での描画");
check("範囲外のminScoreは0-90に丸められる", await evaluate('document.getElementById("min-score").value'), "90");
check("スライダー表示と絞り込みが一致する",
  await evaluate('Number(document.getElementById("min-score-out").textContent) === Number(document.getElementById("min-score").value)'), true);
check("不正なthemeはautoに戻る", await evaluate('document.documentElement.getAttribute("data-theme")'), null);
check("不正なsortはdateに戻る", await evaluate('document.getElementById("sort-date").getAttribute("aria-pressed")'), "true");
await evaluate('localStorage.clear()');
await send("Page.navigate", { url: PAGE });
await waitFor(async () => (await shown()) > 0, "クリーン状態に復帰");

// 指摘6: ぶら下がりインデントが揃う
check("メタ・要約・理由の左端が揃う", await evaluate(`(() => {
  const card = [...document.querySelectorAll(".card")].find(c => c.querySelector(".summary") && c.querySelector(".reason"));
  const left = n => Math.round(n.getBoundingClientRect().left);
  return left(card.querySelector(".meta")) === left(card.querySelector(".summary"))
      && left(card.querySelector(".summary")) === left(card.querySelector(".reason"))
      && left(card.querySelector(".summary")) === left(card.querySelector(".title"));
})()`), true);

// --- 空状態
await evaluate('(()=>{const q=document.getElementById("q"); q.value="zzzznothingzzz"; q.dispatchEvent(new Event("input"))})()');
await sleep(300);
check("該当なしの案内が出る", await evaluate('document.getElementById("notice").hidden===false && document.getElementById("notice").textContent'),
  (v) => typeof v === "string" && v.includes("条件に一致する記事がありません"));
await evaluate('(()=>{const q=document.getElementById("q"); q.value=""; q.dispatchEvent(new Event("input"))})()');
await sleep(300);

// --- テーマ
await evaluate('document.querySelector(\'[data-theme-btn="dark"]\').click()');
await sleep(150);
check("ダーク指定でdata-theme=dark", await evaluate('document.documentElement.getAttribute("data-theme")'), "dark");
const darkBg = await evaluate('getComputedStyle(document.body).backgroundColor');
await shot("page-dark", true);
await evaluate('document.querySelector(\'[data-theme-btn="light"]\').click()');
await sleep(150);
const lightBg = await evaluate('getComputedStyle(document.body).backgroundColor');
check("ライトとダークで背景色が変わる", [lightBg, darkBg], (v) => v[0] !== v[1]);
await shot("page-light", true);
await evaluate('document.querySelector(\'[data-theme-btn="auto"]\').click()');
await sleep(150);

// --- モバイル
await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
await sleep(400);
check("横スクロールが発生しない",
  await evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'), true);
await shot("page-mobile");
await send("Emulation.clearDeviceMetricsOverride");

// --- コンソールエラー
const errors = events.filter((e) => e.method === "Log.entryAdded" && e.params.entry.level === "error")
  .map((e) => e.params.entry.text);
check("コンソールエラーなし", errors, []);

// --- fetch失敗時のエラー表示
await send("Page.addScriptToEvaluateOnNewDocument", {
  source: 'window.fetch = () => Promise.reject(new Error("simulated offline"));'
});
await send("Page.navigate", { url: PAGE });
await waitFor(async () => await evaluate('document.getElementById("notice").hidden === false'), "エラー表示");
check("fetch失敗時にエラーと再読み込みボタン",
  await evaluate('document.getElementById("notice").textContent'),
  (v) => v.includes("feed.json を読み込めませんでした") && v.includes("再読み込み"));
check("エラー時もフィードへのリンクは残る", await evaluate('document.querySelectorAll("footer a").length'), (n) => n >= 3);
await shot("page-error");

// --- レビュー指摘の回帰テスト
// addScriptToEvaluateOnNewDocument で入れた fetch 差し替えは取り消せないので、新しいタブに移る
const t2 = await (await fetch(`${CDP}/json/new?${encodeURIComponent(PAGE)}`, { method: "PUT" })).json();
const ws2 = new WebSocket(t2.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws2.onopen = res; ws2.onerror = rej; });
ws = ws2;
ws2.onmessage = (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
  }
};
await send("Page.enable");
await send("Runtime.enable");

// 指摘1: 生成側の並びが崩れていても日付グループが重複しない
await send("Page.addScriptToEvaluateOnNewDocument", { source: `
  const orig = window.fetch;
  window.fetch = async (...a) => {
    const r = await orig(...a);
    const j = await r.json();
    j.items.sort(() => Math.random() - 0.5);          // 生成側の順序を壊す
    return new Response(JSON.stringify(j), { status: 200, headers: { "content-type": "application/json" } });
  };
` });
await send("Page.navigate", { url: PAGE });
await waitFor(async () => (await shown()) > 0, "シャッフル入力の描画");
check("入力が未ソートでも日付グループが重複しない",
  await evaluate('(()=>{const k=[...document.querySelectorAll(".day-head")].map(h=>h.firstChild.textContent);return k.length===new Set(k).size})()'), true);
check("入力が未ソートでも記事は日付降順",
  await evaluate('(()=>{const t=[...document.querySelectorAll("time")].map(n=>n.dateTime);return t.every((x,i)=>i===0||t[i-1]>=x)})()'), true);

// 指摘3: 存在しないソース名が保存されていても全件消えない
await evaluate('localStorage.setItem("daily-feeds:prefs:v1", JSON.stringify({sources:["消えたフィード"],category:"",minScore:0,sort:"date",unreadOnly:false,theme:"auto"}))');
await send("Page.navigate", { url: PAGE });
await waitFor(async () => (await shown()) > 0, "不正なソース設定での描画");
check("存在しないソース設定は無視される", await shown(), TOTAL);
check("不正なソース設定は保存から除去される",
  await evaluate('JSON.parse(localStorage.getItem("daily-feeds:prefs:v1")).sources'), []);

// 指摘2: 記事が一時的に消えても既読が失われない
await evaluate('document.querySelector(".item .mark").click()');
await sleep(200);
const keptId = await evaluate('Object.keys(JSON.parse(localStorage.getItem("daily-feeds:read:v1")))[0]');
await send("Page.addScriptToEvaluateOnNewDocument", { source: `
  const orig = window.fetch;
  window.fetch = async (...a) => {
    const r = await orig(...a);
    const j = await r.json();
    j.items = j.items.slice(20);                      // 先頭ソースが落ちた状況を模す
    return new Response(JSON.stringify(j), { status: 200, headers: { "content-type": "application/json" } });
  };
` });
await send("Page.navigate", { url: PAGE });
await waitFor(async () => (await shown()) > 0, "記事が減った状態の描画");
check("消えた記事の既読が保持される",
  await evaluate('Object.keys(JSON.parse(localStorage.getItem("daily-feeds:read:v1") || "{}")).length'), 1);
check("保持されているのは同じid", await evaluate('Object.keys(JSON.parse(localStorage.getItem("daily-feeds:read:v1")))[0]'), keptId);

// 指摘4: 取得失敗時でもテーマ切替は効く
await send("Page.addScriptToEvaluateOnNewDocument", { source: 'window.fetch = () => Promise.reject(new Error("offline"));' });
await send("Page.navigate", { url: PAGE });
await waitFor(async () => await evaluate('document.getElementById("notice").hidden === false'), "エラー表示");
await evaluate('document.querySelector(\'[data-theme-btn="dark"]\').click()');
await sleep(200);
check("取得失敗時もテーマ切替が効く", await evaluate('document.documentElement.getAttribute("data-theme")'), "dark");
check("テーマ切替UIは常に見えている",
  await evaluate('!!document.querySelector(".masthead [data-theme-btn]")?.offsetParent'), true);

const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (failed.length) { console.log("失敗:", failed.map((f) => f.label).join(", ")); process.exit(1); }
process.exit(0);
