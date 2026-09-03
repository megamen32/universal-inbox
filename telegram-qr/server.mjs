import http from "node:http";
import { spawnSync } from "node:child_process";
import { chmodSync, mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { TelegramClient } from "telegram";
import { StringSession } from "telegram/sessions/index.js";
import QRCode from "qrcode";

const port = Number(process.env.PORT || 18095);
const publicPrefix = (process.env.PUBLIC_PREFIX || "").replace(/\/$/, "");
const stateDir = "/var/lib/universal-inbox/telegram-qr";
const keyPath = "/var/lib/universal-inbox/secret-agent/telegram-qr.agekey";
const sessionsDir = `${stateDir}/sessions`;
const userIoUrl = process.env.UNIVERSAL_USERIO_URL || "http://127.0.0.1:18093";
// Optional non-interactive 2FA password (overpod-style): answered locally via
// SRP, never persisted. The interactive page form is the default path.
const env2faPassword = (process.env.TELEGRAM_2FA_PASSWORD || process.env.USERIO_TELEGRAM_2FA_PASSWORD || "").trim();
const slots = new Map();
let nextSlot = 1;
let promptSeq = 0;

mkdirSync(sessionsDir, { recursive: true, mode: 0o700 });
for (const file of readdirSync(sessionsDir)) {
  const match = /^account-(\d+)\.session$/.exec(file);
  if (!match) continue;
  const id = `account-${match[1]}`;
  slots.set(id, { status: "connected" });
  nextSlot = Math.max(nextSlot, Number(match[1]) + 1);
}

function secretPath(name) {
  return `${stateDir}/credentials/${name === "telegram_qr_api_id" ? "api_id" : "api_hash"}.age`;
}

function decrypt(name) {
  const result = spawnSync("age", ["--decrypt", "-i", keyPath, secretPath(name)], { encoding: "utf-8" });
  if (result.status !== 0) throw new Error("secure Telegram credential is unavailable");
  return result.stdout.trim();
}

function credentials() {
  return { apiId: Number(decrypt("telegram_qr_api_id")), apiHash: decrypt("telegram_qr_api_hash") };
}

function registerAccount(slot, user) {
  const displayName = [user.firstName, user.lastName].filter(Boolean).join(" ") || user.username || `Telegram ${user.id}`;
  const body = JSON.stringify({
    id: `telegram:${user.id}`,
    provider: "telegram",
    display_name: displayName,
    can_read: true,
    can_reply: false,
    credential_ref: `telegram-qr:${slot}`,
    enabled: true,
  });
  return new Promise((resolve, reject) => {
    const target = new URL("/v1/accounts", userIoUrl);
    const request = http.request(target, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(body),
        Authorization: `Bearer ${process.env.USERIO_API_TOKEN || ""}`,
      },
    }, (response) => {
      response.resume();
      response.on("end", () => response.statusCode === 202 ? resolve(displayName) : reject(new Error(`UserIO returned HTTP ${response.statusCode}`)));
    });
    request.once("error", reject);
    request.end(body);
  });
}

function newClient() {
  const { apiId, apiHash } = credentials();
  return new TelegramClient(new StringSession(""), apiId, apiHash, { connectionRetries: 3 });
}

// Shared 2FA password prompt: env password (non-interactive) or the page form.
// Never dead-ends: the promise resolves when the form is submitted.
function passwordCallback(slot) {
  return async (hint) => {
    if (env2faPassword) return env2faPassword;
    const state = slots.get(slot) || {};
    promptSeq += 1;
    return await new Promise((resolve) => {
      slots.set(slot, { ...state, status: "password-required", passwordHint: hint || "", passwordResolve: resolve, promptSeq });
    });
  };
}

// GramJS semantics: true stops the attempt, false retries.
function errorLogger() {
  let authErrors = 0;
  return async (error) => {
    const message = (error && (error.errorMessage || error.message)) || "unknown";
    console.error("Telegram auth error:", message);
    authErrors += 1;
    return authErrors >= 5;
  };
}

async function finishLogin(slot, client, user) {
  const session = client.session.save();
  const file = `${sessionsDir}/${slot}.session`;
  writeFileSync(file, session, { mode: 0o600 });
  chmodSync(file, 0o600);
  const displayName = [user.firstName, user.lastName].filter(Boolean).join(" ") || user.username || String(user.id);
  try {
    await registerAccount(slot, user);
    slots.set(slot, { status: "connected", name: displayName });
  } catch (error) {
    console.error("Telegram account registration error:", (error && error.message) || "unknown");
    slots.set(slot, { status: "connected-unregistered", name: displayName });
  }
}

function failSlot(slot, error) {
  console.error("Telegram login error:", (error && (error.errorMessage || error.message)) || "unknown");
  const state = slots.get(slot) || {};
  const { passwordResolve, codeResolve, ...rest } = state;
  if (passwordResolve) passwordResolve("");
  if (codeResolve) codeResolve("");
  slots.set(slot, { ...rest, status: "error" });
}

async function startQr(slot) {
  const current = slots.get(slot);
  if (!current || ["connecting", "waiting", "connected"].includes(current.status)) return;
  slots.set(slot, { ...current, status: "connecting", mode: "qr" });
  let client;
  try {
    client = newClient();
    await client.connect();
    slots.set(slot, { ...slots.get(slot), status: "waiting", client });
    const user = await client.signInUserWithQrCode(credentials(), {
      qrCode: async ({ token }) => {
        const encoded = token.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
        const uri = `tg://login?token=${encoded}`;
        const state = slots.get(slot) || {};
        slots.set(slot, { ...state, qr: await QRCode.toDataURL(uri) });
      },
      password: passwordCallback(slot),
      onError: errorLogger(),
    });
    await finishLogin(slot, client, user);
  } catch (error) {
    failSlot(slot, error);
  } finally {
    if (client) await client.disconnect().catch(() => {});
  }
}

async function startPhone(slot) {
  const current = slots.get(slot);
  const phone = (current || {}).phone || "";
  if (!phone || ["connecting", "waiting", "code-required", "connected"].includes(current.status)) return;
  slots.set(slot, { ...current, status: "connecting", mode: "phone" });
  let client;
  try {
    client = newClient();
    await client.connect();
    const user = await client.signInUser(credentials(), {
      phoneNumber: async () => phone,
      phoneCode: async (isCodeViaApp) => {
        const state = slots.get(slot) || {};
        promptSeq += 1;
        return await new Promise((resolve) => {
          slots.set(slot, { ...state, status: "code-required", codeViaApp: !!isCodeViaApp, codeResolve: resolve, promptSeq });
        });
      },
      password: passwordCallback(slot),
      onError: errorLogger(),
    });
    await finishLogin(slot, client, user);
  } catch (error) {
    failSlot(slot, error);
  } finally {
    if (client) await client.disconnect().catch(() => {});
  }
}

function restoreSlot(slot) {
  const match = /^account-(\d+)$/.exec(slot || "");
  if (!match) return false;
  nextSlot = Math.max(nextSlot, Number(match[1]) + 1);
  if (!slots.has(slot)) {
    slots.set(slot, { status: "idle", mode: "qr" });
    void startQr(slot);
  }
  return true;
}

function readBody(req) {
  return new Promise((resolve) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => resolve(new URLSearchParams(body)));
  });
}

function publicState() {
  return [...slots.entries()].map(([id, item]) => ({
    id,
    status: item.status,
    name: item.name || "",
    qr: item.status === "waiting" && item.qr ? item.qr : "",
    phone: item.phone || "",
    mode: item.mode || "qr",
    passwordHint: item.status === "password-required" ? (item.passwordHint || "") : "",
    codeViaApp: item.status === "code-required" ? !!item.codeViaApp : false,
    promptSeq: item.promptSeq || 0,
  }));
}

const pageShell = `<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Connect Telegram</title><style>body{margin:0;font:16px system-ui;background:#111;color:#eee}main{max-width:680px;margin:40px auto;padding:24px}.card{background:#1d1d1d;border-radius:16px;padding:20px;margin:12px 0}button{padding:12px 16px;border:0;border-radius:10px;font-weight:700;cursor:pointer}input{box-sizing:border-box}.qr{width:min(300px,100%);background:#fff;padding:12px;border-radius:12px}form.inline{display:flex;gap:8px;margin:8px 0}form.inline input{flex:1;padding:12px;border:0;border-radius:10px}form.stack input{width:100%;padding:12px;border:0;border-radius:10px;margin:8px 0}</style><main><h1>Connect Telegram <a href="/" style="float:right;font-size:15px">&#8592; back to UserIO</a></h1><form class=inline method=post action="__PREFIX__/phone" data-async=1><input name=phone type=tel placeholder="+79990001111" autocomplete=off><button type=submit>Login by phone</button></form><p><a href="__PREFIX__/new"><button>+ Telegram account (QR)</button></a></p><div id=cards></div></main><script>
var prefix = location.pathname.replace(/\\/$/, "");
var seen = new Map();
function esc(v){return String(v==null?"":v).replace(/[&<>"]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
function card(s){
  var a = function(p){ return prefix + p; };
  var html = '<div class=card id="card-'+s.id+'"><b>'+esc(s.id)+(s.mode==="phone"&&s.phone?" ("+esc(s.phone)+")":"")+'</b><p>'+esc(s.status)+(s.name?": "+esc(s.name):"")+'</p>';
  if (s.qr) html += '<img class=qr src="'+s.qr+'" alt="Telegram login QR"><p>Telegram \\u2192 Settings \\u2192 Devices \\u2192 Link Desktop Device</p>';
  if (s.status === "code-required") html += '<form class=stack method=post action="'+a('/code?slot='+encodeURIComponent(s.id))+'" data-async=1><p>Enter the login code'+(s.codeViaApp?" (sent in Telegram)":" (SMS)")+'</p><input name=code inputmode=numeric autocomplete=one-time-code autofocus><button type=submit>Confirm code</button></form>';
  if (s.status === "password-required") html += '<form class=stack method=post action="'+a('/password?slot='+encodeURIComponent(s.id))+'" data-async=1><p>2FA password'+(s.passwordHint?" (hint: "+esc(s.passwordHint)+")":"")+'</p><input type=password name=password autocomplete=off><button type=submit>Confirm</button></form>';
  if (s.status === "error") html += '<a href="'+a('/start?slot='+encodeURIComponent(s.id))+'"><button>Try again</button></a>';
  html += '</div>';
  return html;
}
function render(list){
  var box = document.getElementById("cards");
  var live = new Set(list.map(function(s){return s.id;}));
  list.forEach(function(s){
    var sig = [s.status,s.name,s.qr.slice(0,80),s.passwordHint,s.phone,s.codeViaApp?1:0,s.promptSeq,s.mode].join("|");
    if (seen.get(s.id) === sig) return;
    seen.set(s.id, sig);
    var el = document.getElementById("card-"+s.id);
    var html = card(s);
    if (el) el.outerHTML = html; else box.insertAdjacentHTML("beforeend", html);
  });
  [...seen.keys()].forEach(function(id){
    if (!live.has(id)) { var el = document.getElementById("card-"+id); if (el) el.remove(); seen.delete(id); }
  });
}
function poll(){ fetch(prefix+"/state").then(function(r){return r.json();}).then(render).catch(function(){}); }
setInterval(poll, 1500);
document.addEventListener("submit", function(e){
  var f = e.target;
  if (f.dataset.async !== "1") return;
  e.preventDefault();
  fetch(f.getAttribute("action"), {method:"POST", body:new FormData(f)}).catch(function(){}).then(poll);
});
poll();
</script>`;

http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const slot = url.searchParams.get("slot");
  if (url.pathname === "/state") {
    res.setHeader("content-type", "application/json");
    return res.end(JSON.stringify(publicState()));
  }
  if (url.pathname === "/new") {
    const created = `account-${nextSlot++}`;
    slots.set(created, { status: "idle", mode: "qr" });
    void startQr(created);
    res.writeHead(302, { Location: `${publicPrefix}/?slot=${created}` });
    return res.end();
  }
  if (url.pathname === "/phone" && req.method === "POST") {
    const params = await readBody(req);
    const phone = (params.get("phone") || "").trim();
    if (phone) {
      const created = `account-${nextSlot++}`;
      slots.set(created, { status: "idle", mode: "phone", phone });
      void startPhone(created);
      res.writeHead(302, { Location: `${publicPrefix}/?slot=${created}` });
      return res.end();
    }
    res.writeHead(302, { Location: `${publicPrefix}/` });
    return res.end();
  }
  if (url.pathname === "/code" && req.method === "POST") {
    const params = await readBody(req);
    const code = (params.get("code") || "").trim();
    const state = slots.get(slot);
    if (state && state.codeResolve) {
      const resolve = state.codeResolve;
      const { codeResolve, codeViaApp, promptSeq, ...rest } = state;
      slots.set(slot, { ...rest, status: "connecting" });
      resolve(code);
    }
    res.writeHead(302, { Location: `${publicPrefix}/?slot=${encodeURIComponent(slot || "")}` });
    return res.end();
  }
  if (url.pathname === "/password" && req.method === "POST") {
    const params = await readBody(req);
    const password = (params.get("password") || "").trim();
    const state = slots.get(slot);
    if (state && state.passwordResolve) {
      const resolve = state.passwordResolve;
      const { passwordResolve, passwordHint, promptSeq, ...rest } = state;
      slots.set(slot, { ...rest, status: "connecting" });
      resolve(password);
    }
    res.writeHead(302, { Location: `${publicPrefix}/?slot=${encodeURIComponent(slot || "")}` });
    return res.end();
  }
  if (url.pathname === "/start" && slots.has(slot)) {
    const state = slots.get(slot);
    if (state.mode === "phone" && state.phone) void startPhone(slot);
    else void startQr(slot);
    res.writeHead(302, { Location: `${publicPrefix}/?slot=${slot}` });
    return res.end();
  }
  if (url.pathname === "/" && slot) restoreSlot(slot);
  res.setHeader("content-type", "text/html; charset=utf-8");
  return res.end(pageShell.split("__PREFIX__").join(publicPrefix));
}).listen(port, "127.0.0.1");
