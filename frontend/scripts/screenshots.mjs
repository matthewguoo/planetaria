/**
 * Screenshot rig: drives the running app through its main flows (phone +
 * desktop) and writes a gallery to docs/screenshots/.
 *
 * Prereqs: the app must be running (backend :8000 + `npm run phone` or dev
 * server), and playwright available:  npm i -D playwright && npx playwright
 * install chromium.  Then:  node scripts/screenshots.mjs [baseUrl] [outDir]
 *
 * PW_CHROMIUM overrides the browser binary (e.g. a system chromium).
 */

import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";

// CJS require so a globally installed playwright (NODE_PATH) works too.
const require = createRequire(import.meta.url);
const { chromium, devices } = require("playwright");

const BASE = process.argv[2] ?? "http://localhost:4173";
const OUT = resolve(process.argv[3] ?? "../docs/screenshots");
mkdirSync(OUT, { recursive: true });

const launchOpts = process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {};
const shot = (page, name) =>
  page.screenshot({ path: `${OUT}/${name}.jpg`, type: "jpeg", quality: 82 });

const browser = await chromium.launch(launchOpts);

// ------------------------------------------------------------------ phone
{
  const ctx = await browser.newContext({ ...devices["iPhone 13"], deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  await page.goto(BASE);
  await page.waitForTimeout(8000);
  await shot(page, "01-phone-chart");

  // Theta workspace: EM cone chip on the chart + template from the tab.
  await page.tap("div.h-8 button:has-text('THETA')");
  await page.tap(".absolute button:has-text('THETA')");
  await page.waitForTimeout(400);
  await page.tap("button:has-text('IC 16Δ')").catch(() => {});
  await page.waitForTimeout(2200);
  await shot(page, "02-phone-theta");

  await page.tap("div.h-8 button:has-text('CHAIN')");
  await page.waitForTimeout(800);
  await shot(page, "03-phone-chain");

  await page.tap("div.h-8 button:has-text('POS')");
  await page.waitForTimeout(800);
  await shot(page, "04-phone-positions");

  await page.tap("nav button:has-text('TRADE')");
  await page.waitForTimeout(900);
  await shot(page, "05-phone-trade");
  await page.tap("button[aria-label='Close sheet']");

  await page.tap("nav button:has-text('ACCOUNT')");
  await page.waitForTimeout(1800);
  await shot(page, "06-phone-account");
  await page.tap("button[aria-label='Close sheet']");

  await page.tap("button[aria-label='Collapse data pane']");
  await page.waitForTimeout(400);
  await shot(page, "07-phone-fullchart");
  await ctx.close();
}

// ---------------------------------------------------------------- desktop
{
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 } });
  const page = await ctx.newPage();
  await page.goto(BASE + "?unlock");
  await page.waitForTimeout(8000);
  await page.click("button:has-text('CHAIN')").catch(() => {});
  await page.waitForTimeout(1200);
  await shot(page, "08-desktop-terminal");

  await page.click("header button:has-text('ACCOUNT')");
  await page.waitForTimeout(2500);
  await shot(page, "09-desktop-account");
  await ctx.close();
}

await browser.close();
console.log(`screenshots written to ${OUT}`);
