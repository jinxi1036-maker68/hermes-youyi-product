#!/usr/bin/env node
"use strict";

if (process.env.NODE_PATH) {
  require("module").Module._initPaths();
}

function readArgs(argv) {
  const result = {};
  for (let i = 2; i < argv.length; i += 1) {
    const item = argv[i];
    if (!item.startsWith("--")) continue;
    const key = item.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith("--")) {
      result[key] = "true";
    } else {
      result[key] = next;
      i += 1;
    }
  }
  return result;
}

function searchUrl(platform, query) {
  const encoded = encodeURIComponent(query);
  if (platform === "xiaohongshu") {
    return `https://www.xiaohongshu.com/search_result?keyword=${encoded}&source=web_search_result_notes`;
  }
  if (platform === "douyin") {
    return `https://www.douyin.com/search/${encoded}?type=general`;
  }
  throw new Error(`Unsupported platform: ${platform}`);
}

function normalizeSpace(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

async function extractRows(page, platform, limit) {
  return await page.evaluate(({ platform, limit }) => {
    function compact(value) {
      return String(value || "").replace(/\s+/g, " ").trim();
    }
    function allowedUrl(url) {
      if (!url) return false;
      if (platform === "xiaohongshu") return /xiaohongshu\.com/.test(url);
      if (platform === "douyin") return /douyin\.com/.test(url);
      return false;
    }
    const rows = [];
    const seen = new Set();
    for (const anchor of Array.from(document.querySelectorAll("a[href]"))) {
      const url = anchor.href || "";
      if (!allowedUrl(url)) continue;
      const card = anchor.closest("section, article, div") || anchor;
      const title = compact(anchor.innerText || anchor.textContent || "");
      const text = compact(card.innerText || card.textContent || title);
      if (!title && !text) continue;
      if (text.length < 6) continue;
      const key = `${url}|${title}`.slice(0, 300);
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({
        id: url,
        url,
        title: title.slice(0, 180) || text.slice(0, 120),
        content: text.slice(0, 700),
        author: "",
        platform,
      });
      if (rows.length >= limit) break;
    }
    return rows;
  }, { platform, limit });
}

async function main() {
  const args = readArgs(process.argv);
  const platform = String(args.platform || "").trim();
  const query = String(args.query || "").trim();
  const limit = Math.max(1, Math.min(Number(args.limit || 10), 20));
  const profileDir = String(args["profile-dir"] || "/opt/hermes-youyi/social-research/chromium-profile");
  const executablePath = String(args.executable || process.env.HERMES_SOCIAL_CHROMIUM || "/usr/bin/chromium-browser");

  if (!["xiaohongshu", "douyin"].includes(platform) || !query) {
    console.log(JSON.stringify({ ok: false, error: "invalid_arguments", error_code: "INVALID_ARGUMENTS", message: "platform and query are required" }));
    return;
  }

  let chromium;
  try {
    ({ chromium } = require("playwright-core"));
  } catch (firstError) {
    try {
      ({ chromium } = require("playwright"));
    } catch (secondError) {
      console.log(JSON.stringify({ ok: false, error: "playwright_missing", error_code: "PLAYWRIGHT_MISSING", message: String(secondError && secondError.message || firstError.message || secondError) }));
      return;
    }
  }

  let context;
  try {
    context = await chromium.launchPersistentContext(profileDir, {
      executablePath,
      headless: false,
      viewport: { width: 1280, height: 900 },
      locale: "zh-CN",
      args: [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
      ],
    });
    const page = await context.newPage();
    const url = searchUrl(platform, query);
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
    await page.waitForTimeout(5000);
    for (let i = 0; i < 2; i += 1) {
      await page.mouse.wheel(0, 700);
      await page.waitForTimeout(1500);
    }
    const items = await extractRows(page, platform, limit);
    console.log(JSON.stringify({
      ok: true,
      platform,
      query,
      page_url: page.url(),
      collected_at: new Date().toISOString(),
      items: items.map((item) => ({
        ...item,
        title: normalizeSpace(item.title).slice(0, 180),
        content: normalizeSpace(item.content).slice(0, 700),
      })),
    }));
  } catch (error) {
    console.log(JSON.stringify({ ok: false, error: "browser_extract_failed", error_code: "BROWSER_EXTRACT_FAILED", message: String(error && error.message || error).slice(0, 1000) }));
    process.exitCode = 1;
  } finally {
    if (context) await context.close();
  }
}

main();
