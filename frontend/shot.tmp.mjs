import { chromium } from "playwright";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
page.on("console", (m) => { if (m.type() === "error") console.log("PAGE ERROR:", m.text()); });
await page.goto("http://localhost:5173/", { waitUntil: "load", timeout: 60000 });
await page.waitForTimeout(7000);
await page.selectOption("select[aria-label='Strategy preset']", "iron_butterfly");
await page.waitForTimeout(3000);

const count = () => page.locator("[aria-label^='Remove leg']").count();
console.log("legs after template:", await count());

const btn = page.locator("[aria-label='Remove leg 2']");
console.log("remove-2 disabled:", await btn.isDisabled());
await btn.click();
await page.waitForTimeout(1000);
console.log("legs 1s after remove:", await count());

await page.waitForTimeout(11000); // across a 10s chain refresh
console.log("legs 12s after remove:", await count());

await browser.close();
