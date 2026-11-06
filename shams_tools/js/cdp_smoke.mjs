// SHAMS web smoke test over the Chrome DevTools Protocol (Node 22+, built-in WebSocket; no packages).
// usage: node cdp_smoke.mjs <browser> <url> <seconds> <screenshot.png> <profileDir>
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import net from "node:net";

const [browser, url, secondsArg, shotPath, profile] = process.argv.slice(2);
const seconds = Number(secondsArg || 8);
const out = { errors: [], console: [], screenshot: false };

const freePort = () => new Promise((res) => {
  const s = net.createServer();
  s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => res(p)); });
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const port = await freePort();
const args = ["--headless=new", "--disable-gpu", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
  "--no-proxy-server", "--no-first-run", "--no-default-browser-check", `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`, "--window-size=1280,720", "about:blank"];
if (process.platform !== "win32") args.unshift("--no-sandbox");
const proc = spawn(browser, args, { stdio: "ignore" });
const finish = (code) => {
  try { proc.kill(); } catch {}
  process.stdout.write(JSON.stringify(out));
  process.exit(code);
};
setTimeout(() => { out.errors.push("smoke test timed out"); finish(0); }, (seconds + 60) * 1000);

let wsUrl = null;
for (let i = 0; i < 100 && !wsUrl; i++) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const page = list.find((t) => t.type === "page");
    if (page) wsUrl = page.webSocketDebuggerUrl;
  } catch {}
  if (!wsUrl) await sleep(200);
}
if (!wsUrl) { out.errors.push("could not reach the browser's DevTools endpoint"); finish(0); }

const ws = new WebSocket(wsUrl);
let id = 0;
const pending = new Map();
const send = (method, params = {}) => new Promise((res) => {
  const n = ++id; pending.set(n, res); ws.send(JSON.stringify({ id: n, method, params }));
});
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result || m); pending.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown") {
    const d = m.params.exceptionDetails;
    out.errors.push(((d.exception && d.exception.description) || d.text || "exception").slice(0, 500));
  } else if (m.method === "Runtime.consoleAPICalled") {
    const text = m.params.args.map((a) => a.value ?? a.description ?? "").join(" ").slice(0, 500);
    out.console.push(`${m.params.type}: ${text}`);
    if (m.params.type === "error") out.errors.push(text);
  } else if (m.method === "Log.entryAdded" && m.params.entry.level === "error") {
    const e = m.params.entry;
    out.errors.push(`${e.source}: ${e.text}${e.url ? " (" + e.url + ")" : ""}`.slice(0, 500));
  }
};
await new Promise((r) => { ws.onopen = r; });
await send("Runtime.enable");
await send("Log.enable");
await send("Page.enable");
await send("Page.navigate", { url });
await sleep(seconds * 1000);
const shot = await send("Page.captureScreenshot", { format: "png" });
if (shot && shot.data) { writeFileSync(shotPath, Buffer.from(shot.data, "base64")); out.screenshot = true; }
ws.close();
finish(0);
