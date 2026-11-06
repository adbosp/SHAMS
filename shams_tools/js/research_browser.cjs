"use strict";

const fs = require("node:fs");
const config = JSON.parse(fs.readFileSync(0, "utf8"));

async function main() {
  const { chromium } = require(config.module);
  const browser = await chromium.launch({
    headless: true,
    chromiumSandbox: true,
    executablePath: config.executable || undefined,
    timeout: config.timeout,
    args: [
      "--proxy-server=http://127.0.0.1:1",
      "--proxy-bypass-list=<-loopback>",
      "--host-resolver-rules=MAP * ~NOTFOUND",
      "--disable-quic",
      "--disable-background-networking",
      "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    ],
  });
  try {
    const context = await browser.newContext({ serviceWorkers: "block", acceptDownloads: false, permissions: [] });
    await context.routeWebSocket("**/*", route => route.close());
    const origin = new URL(config.url).origin;
    await context.route("**/*", async route => {
      const request = route.request();
      try {
        if (request.method() !== "GET" || new URL(request.url()).origin !== origin ||
            !["document", "script", "xhr", "fetch"].includes(request.resourceType())) {
          return await route.abort();
        }
        const response = await fetch(config.broker, {
          method: "POST", body: JSON.stringify({ url: request.url() }), redirect: "error",
          signal: AbortSignal.timeout(config.timeout),
        });
        if (!response.ok) return await route.abort();
        const result = await response.json();
        if (result.url !== request.url()) {
          return await route.fulfill({ status: 302, headers: { location: result.url }, body: "" });
        }
        await route.fulfill({ status: result.status, contentType: result.mime,
          body: Buffer.from(result.body, "base64") });
      } catch {
        await route.abort().catch(() => {});
      }
    });
    const page = await context.newPage();
    const response = await page.goto(config.url, { waitUntil: "load", timeout: config.timeout });
    if (!response || !response.ok()) throw new Error("browser navigation failed");
    await page.waitForTimeout(500);
    const output = await page.evaluate(() => ({
      url: location.href,
      title: document.title,
      text: (document.querySelector("main") || document.body).innerText.trim().slice(0, 4800),
    }));
    if (new URL(output.url).origin !== origin) throw new Error("browser left the original origin");
    process.stdout.write(JSON.stringify(output));
  } finally {
    await browser.close();
  }
}
main().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
