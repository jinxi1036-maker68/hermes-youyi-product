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

function relevanceHints(query) {
  const text = normalizeSpace(query);
  const dictionary = [
    "项城", "托管", "晚托", "作业", "辅导", "小饭桌", "招生",
    "开学", "收心", "暑假", "寒假", "托班", "自习", "接送",
  ];
  const hints = dictionary.filter((item) => text.includes(item));
  if (text.length >= 2 && !hints.includes(text)) hints.push(text);
  return hints;
}

async function extractRows(page, platform, limit, query) {
  return await page.evaluate(({ platform, limit, query }) => {
    function compact(value) {
      return String(value || "").replace(/\s+/g, " ").trim();
    }
    function relevanceHints(queryText) {
      const text = compact(queryText);
      const dictionary = [
        "项城", "托管", "晚托", "作业", "辅导", "小饭桌", "招生",
        "开学", "收心", "暑假", "寒假", "托班", "自习", "接送",
      ];
      const hints = dictionary.filter((item) => text.includes(item));
      if (text.length >= 2 && !hints.includes(text)) hints.push(text);
      return hints;
    }
    function allowedUrl(url) {
      if (!url) return false;
      let parsed;
      try {
        parsed = new URL(url);
      } catch (_error) {
        return false;
      }
      const hostname = parsed.hostname.toLowerCase();
      const path = parsed.pathname.toLowerCase();
      const lower = url.toLowerCase();
      if (/(agree|terms|privacy|protocol|login|passport|help|legal|policy|download|creator)/.test(lower)) {
        return false;
      }
      if (platform === "xiaohongshu") {
        return hostname.endsWith("xiaohongshu.com") && (
          path.startsWith("/explore/") || path.startsWith("/user/profile/")
        );
      }
      if (platform === "douyin") {
        return hostname.endsWith("douyin.com") && (
          path.startsWith("/video/") || path.startsWith("/note/") || path.startsWith("/user/")
        );
      }
      return false;
    }
    function relevantToQuery(url, text, hints) {
      if (!hints.length) return true;
      const haystack = `${url} ${text}`.toLowerCase();
      return hints.some((hint) => haystack.includes(String(hint).toLowerCase()));
    }
    const rows = [];
    const seen = new Set();
    const hints = relevanceHints(query);
    for (const anchor of Array.from(document.querySelectorAll("a[href]"))) {
      const url = anchor.href || "";
      if (!allowedUrl(url)) continue;
      const card = anchor.closest("section, article, div") || anchor;
      const title = compact(anchor.innerText || anchor.textContent || "");
      const text = compact(card.innerText || card.textContent || title);
      if (!title && !text) continue;
      if (text.length < 6) continue;
      if (!relevantToQuery(url, text, hints)) continue;
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
    const items = await extractRows(page, platform, limit, query);
    const bodyText = normalizeSpace(await page.locator("body").innerText({ timeout: 5000 }).catch(() => ""));
    if (!items.length && /我已阅读并同意|用户协议|隐私政策|登录后|扫码登录|验证码/.test(bodyText)) {
      console.log(JSON.stringify({
        ok: false,
        platform,
        query,
        error: "login_required_or_blocked",
        error_code: "AUTH_REQUIRED",
        message: "平台页面未进入可读搜索结果，可能需要在服务器 Chromium profile 中登录或处理平台验证。",
        page_url: page.url(),
      }));
      return;
    }
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
