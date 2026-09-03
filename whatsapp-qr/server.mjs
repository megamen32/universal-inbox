import http from "node:http";
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import makeWASocket, { Browsers, DisconnectReason, fetchLatestBaileysVersion, fetchLatestWaWebVersion, useMultiFileAuthState } from "@whiskeysockets/baileys";
import QRCode from "qrcode";

const port = Number(process.env.PORT || 18096);
const publicPrefix = (process.env.PUBLIC_PREFIX || "").replace(/\/$/, "");
const stateDir = "/var/lib/universal-inbox/whatsapp-qr";
const sessionsDir = join(stateDir, "sessions");
const userIoUrl = process.env.UNIVERSAL_USERIO_URL || "http://127.0.0.1:18093";
const slots = new Map();
let nextSlot = 1;
mkdirSync(sessionsDir, { recursive: true, mode: 0o700 });
const knownSlots = [];
for (const name of readdirSync(sessionsDir, { withFileTypes: true })) {
  const match = /^account-(\d+)$/.exec(name.name);
  if (name.isDirectory() && match) { slots.set(`account-${match[1]}`, { status: "idle" }); nextSlot = Math.max(nextSlot, Number(match[1]) + 1); knownSlots.push(`account-${match[1]}`); }
}
// Reconnect every persisted session at boot; without this the slots stay "idle"
// until a human opens /?slot=... and all ingestion silently stops after a restart.
for (const slot of knownSlots) void start(slot);

const escape = (value) => String(value).replace(/[&<>\"]/g, (char) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;"}[char]));
// WhatsApp hands out anonymous @lid JIDs alongside real phone JIDs; the same
// person then splits into two conversations. Learn lid->phone mappings so
// ingested senders are normalized to one stable phone JID per person.
const lidMapPath = join(stateDir, "lid-map.json");
let lidMap = {};
try { lidMap = JSON.parse(readFileSync(lidMapPath, "utf8")) || {}; } catch { lidMap = {}; }
const learnLid = (lid, jid) => {
  if (typeof lid === "string" && lid.endsWith("@lid") && typeof jid === "string" && jid.endsWith("@s.whatsapp.net") && lidMap[lid] !== jid) {
    lidMap[lid] = jid;
    try { writeFileSync(lidMapPath, JSON.stringify(lidMap)); } catch (error) { console.error("LID map save failed", error.message); }
  }
};
const normSender = (jid) => (typeof jid === "string" && jid.endsWith("@lid") && lidMap[jid]) || jid;
const page = (content) => `<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Connect WhatsApp</title><style>body{margin:0;font:16px system-ui;background:#111;color:#eee}main{max-width:680px;margin:40px auto;padding:24px}.card{background:#1d1d1d;border-radius:16px;padding:20px;margin:12px 0}.qr{width:min(300px,100%);background:#fff;padding:12px;border-radius:12px}button{padding:12px 16px;border:0;border-radius:10px;font-weight:700;cursor:pointer}</style><main><h1>Connect WhatsApp</h1>${content}</main>`;

function postUserIo(path, payload) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(payload);
    const request = http.request(new URL(path, userIoUrl), { method: "POST", headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body), Authorization: `Bearer ${process.env.USERIO_API_TOKEN || ""}` } }, (response) => {
      response.resume();
      response.on("end", () => response.statusCode >= 200 && response.statusCode < 300 ? resolve() : reject(new Error(`UserIO returned HTTP ${response.statusCode}`)));
    });
    request.once("error", reject); request.end(body);
  });
}

async function registerAccount(slot, jid) {
  const id = jid.split(":")[0].replace(/\D/g, "") || jid;
  await postUserIo("/v1/accounts", { id: `whatsapp:${id}`, provider: "whatsapp", display_name: `WhatsApp ${id}`, can_read: true, can_reply: false, credential_ref: `whatsapp-qr:${slot}`, enabled: true });
}

async function ingest(slot, message) {
  const key = message.key || {};
  const sender = normSender(key.remoteJid) || "whatsapp";
  const body = message.message?.conversation || message.message?.extendedTextMessage?.text || message.message?.imageMessage?.caption || message.message?.videoMessage?.caption || (message.message?.imageMessage ? "[WhatsApp image]" : message.message?.videoMessage ? "[WhatsApp video]" : "");
  if (!body || key.fromMe) return;
  await postUserIo("/v1/messages", { route_id: "whatsapp-reply", message: { schema: "universal.inbox.message.v1", source: "whatsapp", message_id: `${slot}:${key.id || Date.now()}`, sender, body } });
}

async function start(slot) {
  const current = slots.get(slot);
  if (!current || ["connecting", "waiting", "connected"].includes(current.status)) return;
  slots.set(slot, { status: "connecting" });
  const auth = await useMultiFileAuthState(join(sessionsDir, slot));
  const fallbackVersion = [2, 3000, 1042466098];
  const versionResult = await Promise.race([
    fetchLatestWaWebVersion(),
    new Promise((resolve) => setTimeout(() => resolve({ version: fallbackVersion }), 5000)),
  ]);
  const version = versionResult?.version || (await fetchLatestBaileysVersion()).version || fallbackVersion;
  const socket = makeWASocket({ version, auth: auth.state, browser: Browsers.ubuntu("Universal UserIO"), printQRInTerminal: false, syncFullHistory: true });
  slots.set(slot, { status: "waiting", socket });
  socket.ev.on("creds.update", auth.saveCreds);
  socket.ev.on("contacts.upsert", (contacts) => {
    for (const contact of contacts || []) learnLid(contact.lid, contact.jid || (typeof contact.id === "string" && contact.id.endsWith("@s.whatsapp.net") ? contact.id : undefined));
  });
  socket.ev.on("chats.phoneNumberShare", ({ lid, jid }) => learnLid(lid, jid));
  socket.ev.on("connection.update", async ({ connection, lastDisconnect, qr }) => {
    const item = slots.get(slot) || {};
    if (qr) slots.set(slot, { ...item, status: "waiting", qr: await QRCode.toDataURL(qr), socket });
    if (connection === "open") {
      const jid = socket.user?.id || "whatsapp";
      try { await registerAccount(slot, jid); slots.set(slot, { status: "connected", name: jid, socket }); }
      catch (error) { console.error("WhatsApp account registration failed", error.message); slots.set(slot, { status: "connected-unregistered", name: jid, socket }); }
    }
    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code !== DisconnectReason.loggedOut) { slots.set(slot, { status: "idle" }); setTimeout(() => start(slot), 3000); }
      else slots.set(slot, { status: "logged-out" });
    }
  });
  socket.ev.on("messages.upsert", async ({ messages, type }) => {
    if (!["notify", "append"].includes(type)) return;
    for (const message of messages) { try { await ingest(slot, message); } catch (error) { console.error("WhatsApp ingest failed", error.message); } }
  });
  socket.ev.on("messaging-history.set", async ({ messages }) => {
    for (const message of messages || []) { try { await ingest(slot, message); } catch (error) { console.error("WhatsApp history ingest failed", error.message); } }
  });
  socket.ev.on("chats.upsert", async (chats) => {
    for (const chat of chats || []) {
      if (chat.lastMessage) { try { await ingest(slot, chat.lastMessage); } catch (error) { console.error("WhatsApp chat ingest failed", error.message); } }
    }
  });
}

function restore(slot) { if (!/^account-\d+$/.test(slot)) return false; if (!slots.has(slot)) slots.set(slot, { status: "idle" }); void start(slot); return true; }

http.createServer((request, response) => {
  const url = new URL(request.url, `http://${request.headers.host}`); const selected = url.searchParams.get("slot");
  if (url.pathname === "/new") { const slot = `account-${nextSlot++}`; slots.set(slot, { status: "idle" }); void start(slot); response.writeHead(302, { Location: `${publicPrefix}/?slot=${slot}` }); return response.end(); }
  if (url.pathname === "/start" && slots.has(selected)) { void start(selected); response.writeHead(302, { Location: `${publicPrefix}/?slot=${selected}` }); return response.end(); }
  if (url.pathname === "/" && selected) restore(selected);
  const cards = [...slots.entries()].map(([id, item]) => `<div class=card><b>${escape(id)}</b><p>${escape(item.status)}${item.name ? `: ${escape(item.name)}` : ""}</p>${item.qr ? `<img class=qr src="${item.qr}" alt="WhatsApp login QR"><p>WhatsApp → Settings → Linked devices → Link a device</p>` : ""}${item.status === "error" ? `<a href="/start?slot=${encodeURIComponent(id)}"><button>Try again</button></a>` : ""}${["connecting", "waiting"].includes(item.status) ? "<script>setTimeout(()=>location.reload(),1000)</script>" : ""}</div>`).join("");
  response.setHeader("content-type", "text/html; charset=utf-8"); response.end(page(`<a href="/new"><button>+ WhatsApp account</button></a>${cards}`));
}).listen(port, "127.0.0.1");
