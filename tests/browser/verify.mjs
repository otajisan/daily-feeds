// docs/index.html を実ブラウザ(headless Chrome + CDP)で操作して検証する。
//
// 依存パッケージなし。Node 24 の組み込み WebSocket / fetch から Chrome DevTools Protocol を直接叩く。
// 起動と後始末は tests/browser/run.sh がやるので、通常はそちらから実行する。
//
// 環境変数:
//   CDP_URL        CDP のエンドポイント (既定 http://127.0.0.1:9222)
//   PAGE_URL       検証対象のページ     (既定 http://127.0.0.1:8931/)
//   SCREENSHOT_DIR 指定するとスクリーンショットを保存する (未指定なら撮らない)
//
// 記事の内容は集約のたびに入れ替わるため、件数・検索語・ソース名・カテゴリは
// すべて feed.json と描画結果から動的に決める。ハードコードしてはいけない。
import { mkdirSync, writeFileSync } from "node:fs";

const CDP = process.env.CDP_URL || "http://127.0.0.1:9222";
const PAGE = process.env.PAGE_URL || "http://127.0.0.1:8931/";
const OUT = process.env.SCREENSHOT_DIR || "";
if (OUT) mkdirSync(OUT, { recursive: true });

const SEND_TIMEOUT_MS = 30000;

// ---------------------------------------------------------------- 期待値をフィードから求める

const feed = await (await fetch(new URL("feed.json", PAGE))).json();

// docs/index.html の normalize() と同じ条件で捨てられる記事を除く
function isRenderable(it) {
  if (!it || !it.title) return false;
  try {
    const u = new URL(it.link, PAGE);
    if (u.protocol !== "http:" && u.protocol !== "https:") return false;
  } catch {
    return false;
  }
  return !isNaN(new Date(it.published));
}

const items = (feed.items || []).filter(isRenderable);
const TOTAL = items.length;

// 一部だけにヒットする検索語を実データから選ぶ
function pickSearchTerm() {
  const hay = (it) => `${it.title} ${it.summary || ""} ${it.source || ""}`.toLowerCase();
  for (const it of items) {
    for (const word of it.title.split(/[\s、。「」『』（）()[\]/|:：,.\-—]+/)) {
      if (word.length < 4) continue;
      const term = word.toLowerCase();
      const hits = items.filter((x) => hay(x).includes(term)).length;
      if (hits > 0 && hits < TOTAL) return term;
    }
  }
  return null;
}

const SEARCH_TERM = pickSearchTerm();
console.log(`feed.json: ${feed.items.length}件 (描画対象 ${TOTAL}件) / 検索語: ${SEARCH_TERM ?? "(見つからず)"}`);
if (!TOTAL) {
  console.error("描画できる記事が feed.json に無いため検証できません");
  process.exit(1);
}

// ---------------------------------------------------------------- CDP クライアント

let ws;
let nextId = 1;
const pending = new Map();
const events = [];

function rejectAllPending(reason) {
  for (const [, p] of pending) p.reject(new Error(reason));
  pending.clear();
}

function send(method, params = {}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`CDP timeout: ${method}`));
    }, SEND_TIMEOUT_MS);
    pending.set(id, {
      resolve: (v) => { clearTimeout(timer); resolve(v); },
      reject: (e) => { clearTimeout(timer); reject(e); },
    });
    ws.send(JSON.stringify({ id, method, params }));
  });
}

function attach(socket) {
  socket.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
    } else if (msg.method) {
      events.push(msg);
    }
  };
  // 接続が切れたまま待ち続けて無言で固まらないようにする
  socket.onclose = () => rejectAllPending("CDP connection closed");
  socket.onerror = () => rejectAllPending("CDP connection error");
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
    let ok = false;
    try {
      ok = await fn();
    } catch {
      ok = false;   // 遷移中は実行コンテキストが無いので単に待つ
    }
    if (ok) return;
    if (Date.now() - started > timeout) throw new Error("timeout waiting for " + label);
    await sleep(150);
  }
}

// Page.navigate 直後の Runtime.evaluate は「遷移前のドキュメント」を見てしまう。
// 旧ドキュメントに印を付け、それが消えるまで待つことで確実に新ドキュメントを掴む。
async function gotoPage(label, ready = 'document.querySelectorAll(".item").length > 0') {
  try {
    await evaluate("window.__stale = 1");
  } catch {
    /* 初回など評価できない場合はそのまま進む */
  }
  await send("Page.navigate", { url: PAGE });
  await waitFor(async () => await evaluate(`!window.__stale && (${ready})`), label);
}

// addScriptToEvaluateOnNewDocument は積み重なるので、必ず identifier を控えて外す
const initScripts = [];
async function addInitScript(source) {
  const { identifier } = await send("Page.addScriptToEvaluateOnNewDocument", { source });
  initScripts.push(identifier);
  return identifier;
}
async function clearInitScripts() {
  while (initScripts.length) {
    await send("Page.removeScriptToEvaluateOnNewDocument", { identifier: initScripts.pop() });
  }
}

// feed.json を差し替えてから読み込ませる
function patchFeedScript(body) {
  return `
    const orig = window.fetch;
    window.fetch = async (...a) => {
      const r = await orig(...a);
      const j = await r.json();
      ${body}
      return new Response(JSON.stringify(j), { status: 200, headers: { "content-type": "application/json" } });
    };
  `;
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
const setSearch = (term) =>
  evaluate(`(()=>{const q=document.getElementById("q"); q.value=${JSON.stringify(term)}; q.dispatchEvent(new Event("input"))})()`);

// ---------------------------------------------------------------- main

const target = await (await fetch(`${CDP}/json/new?${encodeURIComponent(PAGE)}`, { method: "PUT" })).json();
ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
attach(ws);

await send("Page.enable");
await send("Runtime.enable");
await send("Log.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1000, height: 1300, deviceScaleFactor: 1, mobile: false });
await gotoPage("初期描画");
// 前回実行の保存状態を消してから初期状態を確認する
await evaluate("localStorage.clear()");
await gotoPage("初期描画(クリーン)");

// --- 初期状態
check(`初期表示件数 = ${TOTAL}`, await shown(), TOTAL);
check("日付グループが複数", await days(), (n) => n > 5);
check("ステータスに最終更新と件数", await status(), (s) => s.includes("最終更新") && s.includes(`全${TOTAL}件中 ${TOTAL}件を表示`));
check("スコアバッジのクラスが定義済みの5段階に収まる",
  await evaluate('[...new Set([...document.querySelectorAll(".score")].map(n=>n.className.replace("score ","")))].sort()'),
  (v) => v.length > 0 && v.every((c) => ["s90", "s80", "s70", "s60", "slow"].includes(c)));
check("タイトルは新規タブで開く",
  await evaluate('[...document.querySelectorAll(".title")].every(a=>a.target==="_blank"&&a.rel.includes("noopener"))'), true);
check("リンクは http(s) のみ",
  await evaluate('[...document.querySelectorAll(".title")].every(a=>a.protocol==="https:"||a.protocol==="http:")'), true);

// --- sticky の重なり
await evaluate("window.scrollTo(0, 2500)");
await sleep(400);
check("スクロール中も日付見出しがコントロールに隠れない",
  await evaluate(`(() => {
    const c = document.getElementById("controls").getBoundingClientRect();
    const heads = [...document.querySelectorAll(".day-head")].map(h => h.getBoundingClientRect());
    const stuck = heads.filter(h => h.top < c.bottom + 4 && h.bottom > 0);
    return stuck.length === 0 || stuck.every(h => h.top >= c.bottom - 1);
  })()`), true);
await evaluate("window.scrollTo(0, 0)");
await sleep(200);

// --- 検索
if (SEARCH_TERM) {
  await setSearch(SEARCH_TERM);
  await sleep(300);
  check(`検索 '${SEARCH_TERM}' で絞り込まれる`, await shown(), (n) => n > 0 && n < TOTAL);
  check("検索結果は全件が検索語を含む",
    await evaluate(`(()=>{const xs=[...document.querySelectorAll(".item")];
      return xs.length>0 && xs.every(n=>n.textContent.toLowerCase().includes(${JSON.stringify(SEARCH_TERM)}))})()`), true);
  await setSearch("");
  await sleep(300);
  check("検索クリアで全件に戻る", await shown(), TOTAL);
  await setSearch(SEARCH_TERM);
  await sleep(300);
  await gotoPage("検索後のリロード");
  check("検索語はリロードで持ち越さない", await shown(), TOTAL);
} else {
  console.log("skip 検索: 部分一致する語を実データから選べなかった");
}

// --- 最低スコア
await evaluate('(()=>{const s=document.getElementById("min-score"); s.value="70"; s.dispatchEvent(new Event("input"))})()');
await sleep(150);
check("最低スコア70で絞り込み", await shown(), (n) => n >= 0 && n < TOTAL);
check("表示された全件が70点以上",
  await evaluate('[...document.querySelectorAll(".score")].every(n=>Number(n.textContent)>=70)'), true);
check("スライダー表示値が70", await evaluate('document.getElementById("min-score-out").textContent'), "70");
await evaluate('(()=>{const s=document.getElementById("min-score"); s.value="0"; s.dispatchEvent(new Event("input"))})()');
await sleep(150);

// --- ソースチップ(名前は描画結果から取る)
const chipSource = await evaluate('document.querySelector(".chip")?.firstChild?.textContent ?? ""');
check("ソースチップが描画されている", chipSource, (v) => typeof v === "string" && v.length > 0);
if (chipSource) {
  await evaluate('document.querySelectorAll(".chip")[0].click()');
  await sleep(150);
  check(`ソース絞り込み(${chipSource})`,
    await evaluate(`(()=>{const xs=[...document.querySelectorAll(".meta .src")];
      return xs.length>0 && xs.every(n=>n.textContent===${JSON.stringify(chipSource)})})()`), true);
  check("チップが押下状態",
    await evaluate('document.querySelectorAll(".chip")[0].getAttribute("aria-pressed")'), "true");
  await evaluate('document.querySelectorAll(".chip")[0].click()');
  await sleep(150);
}

// --- カテゴリ(選択肢は描画結果から取る)
const category = await evaluate('[...document.getElementById("category").options].map(o=>o.value).filter(Boolean)[0] ?? ""');
check("カテゴリの選択肢がある", category, (v) => typeof v === "string" && v.length > 0);
if (category) {
  await evaluate(`(()=>{const c=document.getElementById("category"); c.value=${JSON.stringify(category)}; c.dispatchEvent(new Event("change"))})()`);
  await sleep(150);
  check(`カテゴリ絞り込み(${category})`,
    await evaluate(`(()=>{const xs=[...document.querySelectorAll(".meta .cat")];
      return xs.length>0 && xs.every(n=>n.textContent===${JSON.stringify(category)})})()`), true);
  await evaluate('(()=>{const c=document.getElementById("category"); c.value=""; c.dispatchEvent(new Event("change"))})()');
  await sleep(150);
}

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
  await evaluate(`(() => [...document.querySelectorAll(".day")].every(d => {
    const label = d.querySelector(".day-head .n");
    return label && label.textContent === d.querySelectorAll(".item").length + "件";
  }))()`), true);
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

// 未読のみ表示のまま既読化しても再描画されず、クリックしたカードがDOMに残る
check("未読のみ表示中の既読化で件数が変わらない", await evaluate(`(() => {
  if (document.getElementById("unread").getAttribute("aria-pressed") !== "true") return "未読のみが有効でない";
  const before = document.querySelectorAll(".item").length;
  const li = document.querySelectorAll(".item")[5];
  const id = li.dataset.id;
  li.querySelector(".mark").click();
  const after = document.querySelectorAll(".item").length;
  const same = document.querySelector('[data-id="' + CSS.escape(id) + '"]');
  return before === after && !!same && same.classList.contains("is-read");
})()`), true);

// --- リロード後の永続化
await gotoPage("リロード後の描画");
check("リロード後も未読のみが維持", await shown(), TOTAL - 2);
check("未読のみボタンが押下状態", await evaluate('document.getElementById("unread").getAttribute("aria-pressed")'), "true");
await evaluate('document.getElementById("unread").click()');
await sleep(200);
check("未読のみ解除で既読カードが戻る", await evaluate('document.querySelectorAll(".item.is-read").length'), 2);

// --- 既読を消去
await evaluate('document.getElementById("clear-read").click()');
await sleep(200);
check("既読消去で0件", await evaluate('document.querySelectorAll(".item.is-read").length'), 0);

// --- 範囲外の保存値からの復帰
await evaluate('localStorage.setItem("daily-feeds:prefs:v1", JSON.stringify({sources:[],category:"",minScore:9999,sort:"nonsense",unreadOnly:false,theme:"evil"}))');
await gotoPage("範囲外設定での描画");
check("範囲外のminScoreは0-90に丸められる", await evaluate('document.getElementById("min-score").value'), "90");
check("スライダー表示と絞り込みが一致する",
  await evaluate('Number(document.getElementById("min-score-out").textContent) === Number(document.getElementById("min-score").value)'), true);
check("不正なthemeはautoに戻る", await evaluate('document.documentElement.getAttribute("data-theme")'), null);
check("不正なsortはdateに戻る", await evaluate('document.getElementById("sort-date").getAttribute("aria-pressed")'), "true");
await evaluate("localStorage.clear()");
await gotoPage("クリーン状態に復帰");

// --- ぶら下がりインデント
check("メタ・要約・理由の左端が揃う", await evaluate(`(() => {
  const card = [...document.querySelectorAll(".card")].find(c => c.querySelector(".summary") && c.querySelector(".reason"));
  if (!card) return "要約と理由が揃ったカードが無い";
  const left = n => Math.round(n.getBoundingClientRect().left);
  return left(card.querySelector(".meta")) === left(card.querySelector(".summary"))
      && left(card.querySelector(".summary")) === left(card.querySelector(".reason"))
      && left(card.querySelector(".summary")) === left(card.querySelector(".title"));
})()`), true);

// --- 空状態
await setSearch("zzzznothingzzz");
await sleep(300);
check("該当なしの案内が出る", await evaluate('document.getElementById("notice").hidden===false && document.getElementById("notice").textContent'),
  (v) => typeof v === "string" && v.includes("条件に一致する記事がありません"));
await setSearch("");
await sleep(300);

// --- テーマ
await evaluate('document.querySelector(\'[data-theme-btn="dark"]\').click()');
await sleep(150);
check("ダーク指定でdata-theme=dark", await evaluate('document.documentElement.getAttribute("data-theme")'), "dark");
const darkBg = await evaluate("getComputedStyle(document.body).backgroundColor");
await shot("page-dark", true);
await evaluate('document.querySelector(\'[data-theme-btn="light"]\').click()');
await sleep(150);
const lightBg = await evaluate("getComputedStyle(document.body).backgroundColor");
check("ライトとダークで背景色が変わる", [lightBg, darkBg], (v) => v[0] !== v[1]);
await shot("page-light", true);
await evaluate('document.querySelector(\'[data-theme-btn="auto"]\').click()');
await sleep(150);

// --- モバイル
await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
await sleep(400);
check("横スクロールが発生しない",
  await evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), true);
await shot("page-mobile");
await send("Emulation.clearDeviceMetricsOverride");

// --- コンソールエラー
const consoleErrors = events
  .filter((e) => e.method === "Log.entryAdded" && e.params.entry.level === "error")
  .map((e) => e.params.entry.text);
check("コンソールエラーなし", consoleErrors, []);

// --- 生成側の並びが崩れていても日付グループが重複しない
await clearInitScripts();
await addInitScript(patchFeedScript("j.items.reverse();   // 日付降順の前提を確実に崩す"));
await gotoPage("逆順入力の描画");
check("入力が未ソートでも日付グループが重複しない",
  await evaluate('(()=>{const k=[...document.querySelectorAll(".day-head")].map(h=>h.firstChild.textContent);return k.length>0 && k.length===new Set(k).size})()'), true);
check("入力が未ソートでも記事は日付降順",
  await evaluate('(()=>{const t=[...document.querySelectorAll("time")].map(n=>n.dateTime);return t.every((x,i)=>i===0||t[i-1]>=x)})()'), true);

// --- 存在しないソース名が保存されていても全件消えない
await clearInitScripts();
await evaluate('localStorage.setItem("daily-feeds:prefs:v1", JSON.stringify({sources:["消えたフィード"],category:"",minScore:0,sort:"date",unreadOnly:false,theme:"auto"}))');
await gotoPage("不正なソース設定での描画");
check("存在しないソース設定は無視される", await shown(), TOTAL);
check("不正なソース設定は保存から除去される",
  await evaluate('JSON.parse(localStorage.getItem("daily-feeds:prefs:v1")).sources'), []);

// --- 記事がフィードから消えても既読が失われない
await evaluate('document.querySelector(".item .mark").click()');
await sleep(200);
const keptId = await evaluate('Object.keys(JSON.parse(localStorage.getItem("daily-feeds:read:v1")))[0]');
// 既読にした記事そのものを確実に消す
await addInitScript(patchFeedScript(`j.items = j.items.filter(x => x.id !== ${JSON.stringify(keptId)});`));
await gotoPage("既読記事が消えた状態の描画");
check("既読にした記事がフィードから消えている",
  await evaluate(`!document.querySelector('[data-id="' + CSS.escape(${JSON.stringify(keptId)}) + '"]')`), true);
check("消えた記事の既読が保持される",
  await evaluate('Object.keys(JSON.parse(localStorage.getItem("daily-feeds:read:v1") || "{}")).length'), 1);
check("保持されているのは同じid", await evaluate('Object.keys(JSON.parse(localStorage.getItem("daily-feeds:read:v1")))[0]'), keptId);

// --- 取得失敗時
await clearInitScripts();
await addInitScript('window.fetch = () => Promise.reject(new Error("simulated offline"));');
await gotoPage("取得失敗時の表示", 'document.getElementById("notice").hidden === false');
check("fetch失敗時にエラーと再読み込みボタン",
  await evaluate('document.getElementById("notice").textContent'),
  (v) => v.includes("feed.json を読み込めませんでした") && v.includes("再読み込み"));
check("エラー時もフィードへのリンクは残る", await evaluate('document.querySelectorAll("footer a").length'), (n) => n >= 3);
await evaluate('document.querySelector(\'[data-theme-btn="dark"]\').click()');
await sleep(200);
check("取得失敗時もテーマ切替が効く", await evaluate('document.documentElement.getAttribute("data-theme")'), "dark");
check("テーマ切替UIは常に見えている",
  await evaluate('!!document.querySelector(".masthead [data-theme-btn]")?.offsetParent'), true);
await shot("page-error");
await clearInitScripts();

// ---------------------------------------------------------------- 結果

const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (failed.length) {
  console.log("失敗:", failed.map((f) => f.label).join(", "));
  process.exit(1);
}
process.exit(0);
