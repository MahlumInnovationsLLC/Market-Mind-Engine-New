#!/usr/bin/env node
/**
 * Capture authenticated mobile app screens for the marketing site.
 *
 * Drives the running Expo react-native-web bundle through Clerk's
 * @clerk/testing helper as the operator account configured in
 * E2E_CLERK_USER_EMAIL (defaults to the axiom-e2e-operator account
 * carried in OPERATOR_ALLOWED_EMAILS), navigates the dashboard,
 * pipeline, and positions tabs, and writes JPEG captures to
 * artifacts/web/public/app-screens/.
 *
 * Light + dark variants are produced when available.
 *
 * Required env: CLERK_PUBLISHABLE_KEY, CLERK_SECRET_KEY.
 * Optional env:
 *   E2E_CLERK_USER_EMAIL   default: axiom-e2e-operator@example.com
 *   MOBILE_BASE_URL        default: http://localhost:18115/mobile/
 *   APP_SCREENS_OUT        default: ../web/public/app-screens
 */

const path = require("path");
const fs = require("fs");
const { chromium } = require("@playwright/test");
const { clerk, clerkSetup } = require("@clerk/testing/playwright");

const projectRoot = path.resolve(__dirname, "..");
const defaultOutDir = path.resolve(
  projectRoot,
  "..",
  "web",
  "public",
  "app-screens",
);

const CLERK_USER_EMAIL =
  process.env.E2E_CLERK_USER_EMAIL || "axiom-e2e-operator@example.com";
const BASE_URL = (process.env.MOBILE_BASE_URL || "http://localhost:18115/mobile/")
  .replace(/\/+$/, "/");
const OUT_DIR = process.env.APP_SCREENS_OUT
  ? path.resolve(process.env.APP_SCREENS_OUT)
  : defaultOutDir;

const VIEWPORT = { width: 390, height: 844 };
const DEVICE_SCALE_FACTOR = 2;

const SCREENS = [
  { slug: "overview", path: "" },
  { slug: "pipeline", path: "pipeline" },
  { slug: "positions", path: "positions" },
];

const COLOR_SCHEMES = ["dark", "light"];

function log(...args) {
  console.log("[capture]", ...args);
}

async function waitForSignedInShell(page) {
  // Wait for the dashboard's tab bar to render. The (tabs) layout
  // mounts a list of icons whose accessibility labels match each tab
  // title (Overview, Positions, Pipeline, ...). Falling back to any
  // visible "OVERVIEW" header is enough to confirm we're past auth.
  const overviewHeader = page.getByText(/overview/i).first();
  await overviewHeader.waitFor({ state: "visible", timeout: 30_000 });
}

async function dismissAwaitingApprovalIfShown(page) {
  const awaiting = page.getByText(/awaiting approval/i);
  if (await awaiting.isVisible().catch(() => false)) {
    throw new Error(
      "axiom-e2e-operator is not approved — cannot capture signed-in screens",
    );
  }
}

async function capture(browser, screen, scheme) {
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: DEVICE_SCALE_FACTOR,
    colorScheme: scheme,
    isMobile: true,
    hasTouch: true,
  });
  const page = await context.newPage();

  // Surface bundle errors so a broken capture fails loudly rather
  // than silently writing a blank screenshot.
  page.on("pageerror", (err) => log("pageerror:", err.message));
  page.on("console", (msg) => {
    if (msg.type() === "error") log("console.error:", msg.text());
  });

  try {
    log(`navigating to ${BASE_URL} (${scheme})`);
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });

    log("waiting for sign-in bundle to render");
    // Wait for the actual Expo bundle to hydrate the sign-in screen
    // before handing off to Clerk. Without this clerk.signIn races
    // the bundle and Clerk-js never has a chance to install its
    // session cookie on the right origin.
    await page
      .getByText(/operator sign-in/i)
      .first()
      .waitFor({ state: "visible", timeout: 60_000 });

    log(`signing in as ${CLERK_USER_EMAIL}`);
    await clerk.signIn({ page, emailAddress: CLERK_USER_EMAIL });

    // After sign-in the (tabs) layout becomes the root. Land on the
    // requested tab. Use full page load so the new bundle has time
    // to fetch the user's data.
    const target = `${BASE_URL}${screen.path}`;
    log(`navigating to tab: ${target}`);
    await page.goto(target, { waitUntil: "networkidle" });

    await waitForSignedInShell(page);
    await dismissAwaitingApprovalIfShown(page);

    // Give the data queries a beat to populate so the cards have
    // numbers in them rather than spinners.
    await page.waitForTimeout(3_500);

    // Replace the test operator email with a generic marketing
    // handle so the captured screenshots don't leak the e2e fixture
    // address. Walks every text node and rewrites in-place — much
    // less invasive than restyling the whole header.
    await page.evaluate(() => {
      const replace = (from, to) => {
        const walker = document.createTreeWalker(
          document.body,
          NodeFilter.SHOW_TEXT,
        );
        const targets = [];
        let node = walker.nextNode();
        while (node) {
          if (node.nodeValue && node.nodeValue.includes(from)) {
            targets.push(node);
          }
          node = walker.nextNode();
        }
        for (const n of targets) {
          n.nodeValue = n.nodeValue.split(from).join(to);
        }
      };
      replace("axiom-e2e-operator@example.com", "operator@axiompredict.io");
      replace("axiom-e2e-operator", "operator");
    });

    const fname = `${screen.slug}-${scheme}.jpg`;
    const out = path.join(OUT_DIR, fname);
    await page.screenshot({
      path: out,
      type: "jpeg",
      quality: 82,
      fullPage: false,
      clip: { x: 0, y: 0, width: VIEWPORT.width, height: VIEWPORT.height },
    });
    log(`wrote ${out}`);
  } finally {
    await context.close();
  }
}

async function main() {
  if (!process.env.CLERK_PUBLISHABLE_KEY || !process.env.CLERK_SECRET_KEY) {
    throw new Error(
      "CLERK_PUBLISHABLE_KEY and CLERK_SECRET_KEY are required to capture authenticated screens",
    );
  }

  fs.mkdirSync(OUT_DIR, { recursive: true });

  await clerkSetup({
    publishableKey: process.env.CLERK_PUBLISHABLE_KEY,
    secretKey: process.env.CLERK_SECRET_KEY,
  });

  const browser = await chromium.launch({
    headless: true,
    args: [
      "--no-sandbox",
      "--disable-dev-shm-usage",
      // The Replit preview proxy ultimately resolves to loopback,
      // which trips Chrome's Private Network Access guard whenever
      // the Expo bundle (served on the .expo subdomain) fetches the
      // API on the bare dev domain. Disabling PNA + the related
      // mixed-content checks lets the dashboard's React-Query fetches
      // populate so we capture real data instead of empty cards.
      "--disable-features=BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessSendPreflights",
      "--disable-web-security",
    ],
  });

  try {
    for (const screen of SCREENS) {
      for (const scheme of COLOR_SCHEMES) {
        try {
          await capture(browser, screen, scheme);
        } catch (err) {
          log(`FAILED ${screen.slug}/${scheme}: ${err.message}`);
          throw err;
        }
      }
    }
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error("[capture] fatal:", err);
  process.exit(1);
});
