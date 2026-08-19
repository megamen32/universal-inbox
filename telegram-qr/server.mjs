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
const slots = new Map();
let nextSlot = 1;

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
  const result = spawnSync("age", ["--decrypt", "-i", keyPath, secretPath(name)], { encoding: "utf8" });
  if (result.status !== 0) throw new Error("secure Telegram credential is unavailable");
  return result.stdout.trim();
}

function credentials() {
  return { apiId: Number(decrypt("telegram_qr_api_id")), apiHash: decrypt("telegram_qr_api_hash") };
}

function escape(value) {
  return String(value).replace(/[&<>\"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[char]);
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

function page(content) {
  return `<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Connect Telegram</title><style>body{margin:0;font:16px system-ui;background:#111;color:#eee}main{max-width:680px;margin:40px auto;padding:24px}.card{background:#1d1d1d;border-radius:16px;padding:20px;margin:12px 0}button{padding:12px 16px;border:0;border-radius:10px;font-weight:700;cursor:pointer}.qr{width:min(300px,100%);background:#fff;padding:12px;border-radius:12px}</style><main><h1>Connect Telegram</h1>${content}</main>`;
}

async function start(slot) {
  const current = slots.get(slot);
  if (!current || ["connecting", "waiting", "connected"].includes(current.status)) return;
  slots.set(slot, { status: "connecting" });
  let client;
  try {
    client = new TelegramClient(new StringSession(""), credentials().apiId, credentials().apiHash, { connectionRetries: 3 });
    await client.connect();
    slots.set(slot, { status: "waiting", client });
    const user = await client.signInUserWithQrCode(credentials(), {
      qrCode: async ({ token }) => {
        const encoded = token.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
        const uri = `tg://login?token=${encoded}`;
        slots.set(slot, { status: "waiting", client, qr: await QRCode.toDataURL(uri) });
      },
      password: async () => {
        slots.set(slot, { status: "password-required", client });
        return new Promise(() => {});
      },
      onError: async (error) => {
        console.error("Telegram QR authentication error:", (error && error.message) || "unknown");
        return true;
      },
    });
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
  } catch (error) {
    console.error("Telegram QR session error:", (error && error.message) || "unknown");
    slots.set(slot, { status: "error" });
  } finally {
    if (client) await client.disconnect().catch(() => {});
  }
}

http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const slot = url.searchParams.get("slot");
  if (url.pathname === "/new") {
    const created = `account-${nextSlot++}`;
    slots.set(created, { status: "idle" });
    void start(created);
    res.writeHead(302, { Location: `${publicPrefix}/?slot=${created}` });
    return res.end();
  }
  if (url.pathname === "/start" && slots.has(slot)) {
    void start(slot);
    res.writeHead(302, { Location: `${publicPrefix}/?slot=${slot}` });
    return res.end();
  }
  const cards = [...slots.entries()].map(([id, item]) => {
    const refreshing = item.status === "connecting" || (item.status === "waiting" && !item.qr);
    return `<div class=card><b>${escape(id)}</b><p>${escape(item.status)}${item.name ? `: ${escape(item.name)}` : ""}</p>${item.qr ? `<img class=qr src="${item.qr}" alt="Telegram login QR"><p>Telegram → Settings → Devices → Link Desktop Device</p>` : ""}${item.status === "error" ? `<a href="/start?slot=${encodeURIComponent(id)}"><button>Try again</button></a>` : ""}${refreshing ? "<script>setTimeout(()=>location.reload(),1000)</script>" : ""}</div>`;
  }).join("");
  res.setHeader("content-type", "text/html; charset=utf-8");
  res.end(page(`<a href="/new"><button>+ Telegram account</button></a>${cards}`));
}).listen(port, "127.0.0.1");
