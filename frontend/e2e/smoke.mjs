// End-to-end smoke test of the built UI against a running server.
//
//   python tools/dev_server.py --port 5090        # seeded throwaway database
//   pnpm e2e                                        # from frontend/
//
// For every role it logs in, opens every page of its navigation and checks:
// no uncaught error, no console error, no CSP violation, no unexpected HTTP
// error, a visible page heading and no horizontal overflow; it also checks
// that pages outside the role's navigation are not reachable. Screenshots go
// to $PTS_E2E_OUT (default: <tmp>/pts-e2e). Uses the locally installed Edge
// (or Chrome with PTS_BROWSER=chrome) through playwright-core — no browser
// download needed.
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import process from 'node:process'
import { chromium } from 'playwright-core'

const BASE = process.env.PTS_BASE_URL || 'http://127.0.0.1:5090'
const OUT = process.env.PTS_E2E_OUT || path.join(os.tmpdir(), 'pts-e2e')
const CHANNEL = process.env.PTS_BROWSER === 'chrome' ? 'chrome' : 'msedge'
fs.mkdirSync(OUT, { recursive: true })

const ALL_PAGES = [
  'dashboard', 'products', 'suppliers', 'users', 'batch-gen', 'batch-entry', 'batch-trace',
  'batch-quality', 'trace', 'my-records', 'purchase-orders', 'inbound-receipts',
  'production-orders', 'scan-gun', 'inventory-sync', 'settings',
]

const ROLES = [
  {
    name: 'admin',
    username: process.env.PTS_ADMIN_USER || 'admin',
    password: process.env.PTS_ADMIN_PASSWORD || 'Dev@Admin#2026',
    landing: 'dashboard',
    pages: ALL_PAGES,
  },
  {
    name: 'warehouse',
    username: 'warehouse.demo',
    password: 'Dev@Store#2026',
    landing: 'batch-gen',
    pages: ['batch-gen', 'batch-entry', 'inbound-receipts', 'production-orders', 'scan-gun', 'batch-trace', 'my-records'],
  },
  {
    name: 'operations',
    username: 'operations.demo',
    password: 'Dev@Ops#2026',
    landing: 'products',
    pages: ['products', 'purchase-orders', 'batch-trace', 'inventory-sync'],
  },
]

const failures = []
function fail(message) {
  failures.push(message)
  console.log(`  ✗ ${message}`)
}

async function newPage(browser, viewport = { width: 1440, height: 900 }) {
  const context = await browser.newContext({ viewport })
  const page = await context.newPage()
  const events = []
  page.on('console', (message) => {
    if (message.type() === 'error') {
      events.push(`console: ${message.text()}`)
    }
  })
  page.on('pageerror', error => events.push(`pageerror: ${error.message}`))
  page.on('response', (response) => {
    const url = response.url()
    // The session probe before login is expected to answer 401.
    if (response.status() >= 400 && !url.endsWith('/api/auth/me')) {
      events.push(`http ${response.status()}: ${response.request().method()} ${url}`)
    }
  })
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (event) => {
      console.error(`CSP ${event.violatedDirective} blocked ${event.blockedURI || 'inline'} (${event.sourceFile}:${event.lineNumber}:${event.columnNumber})`)
    })
  })
  return { context, page, events }
}

function drain(events, label) {
  const relevant = events.filter(item => !/status of 401|\/api\/auth\/me/.test(item))
  for (const item of relevant) {
    fail(`${label}: ${item}`)
  }
  events.length = 0
}

async function login(page, role) {
  await page.goto(`${BASE}/#/login`, { waitUntil: 'networkidle' })
  await page.fill('input[name="username"]', role.username)
  await page.fill('input[name="password"]', role.password)
  await page.click('#login-form button[type="submit"]')
  await page.waitForURL(new RegExp(`#/${role.landing}$`), { timeout: 15000 })
}

async function checkPage(page, key, label) {
  await page.goto(`${BASE}/#/${key}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(700)
  const state = await page.evaluate(() => ({
    hash: location.hash,
    heading: document.querySelector('#app-content .text-2xl')?.textContent?.trim() ?? '',
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    notAllowed: Boolean(document.querySelector('#app-content')?.textContent?.includes('没有访问权限')),
  }))
  if (state.hash !== `#/${key}`) {
    fail(`${label}: expected #/${key}, landed on ${state.hash}`)
  }
  if (!state.heading) {
    fail(`${label}: no page heading`)
  }
  if (state.overflow) {
    fail(`${label}: horizontal overflow`)
  }
  await page.screenshot({ path: path.join(OUT, `${label.replace(/[^\w-]+/g, '_')}.png`) })
  return state
}

const browser = await chromium.launch({ channel: CHANNEL, headless: true })
try {
  for (const role of ROLES) {
    console.log(`▶ ${role.name}`)
    const { context, page, events } = await newPage(browser)
    await login(page, role)
    drain(events, `${role.name}/login`)
    for (const key of role.pages) {
      const state = await checkPage(page, key, `${role.name}-${key}`)
      drain(events, `${role.name}/${key}`)
      console.log(`  ✓ ${key} — ${state.heading}`)
    }
    // Pages outside the role's navigation are not registered for it.
    for (const key of ALL_PAGES.filter(item => !role.pages.includes(item))) {
      await page.goto(`${BASE}/#/${key}`, { waitUntil: 'networkidle' })
      await page.waitForTimeout(300)
      const text = await page.evaluate(() => document.body.textContent ?? '')
      if (!/404|找不到页面|没有访问权限/.test(text)) {
        fail(`${role.name}: ${key} should not be reachable`)
      }
      events.length = 0
    }
    await context.close()
  }

  console.log('▶ mobile layout (390×844)')
  {
    const { context, page, events } = await newPage(browser, { width: 390, height: 844 })
    await login(page, ROLES[1])
    for (const key of ['batch-entry', 'scan-gun']) {
      await checkPage(page, key, `mobile-${key}`)
      drain(events, `mobile/${key}`)
      console.log(`  ✓ ${key}`)
    }
    await context.close()
  }
}
finally {
  await browser.close()
}

console.log(failures.length ? `\n${failures.length} problem(s); screenshots in ${OUT}` : `\nall checks passed; screenshots in ${OUT}`)
process.exit(failures.length ? 1 : 0)
