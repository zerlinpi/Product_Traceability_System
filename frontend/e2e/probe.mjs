// Ad-hoc browser probe used while porting pages (not part of CI).
//   node e2e/probe.mjs <path-after-#> [username] [password] [screenshot.png]
import process from 'node:process'
import { chromium } from 'playwright-core'

const BASE = process.env.PTS_BASE_URL || 'http://127.0.0.1:5090'
const [route = '/', username = 'admin', password = 'Dev@Admin#2026', shot = ''] = process.argv.slice(2)

const browser = await chromium.launch({ channel: 'msedge', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
const problems = []
page.on('console', (message) => {
  if (['error', 'warning'].includes(message.type())) {
    problems.push(`[console.${message.type()}] ${message.text()}`)
  }
})
page.on('pageerror', error => problems.push(`[pageerror] ${error.message}`))
page.on('response', (response) => {
  if (response.status() >= 400) {
    problems.push(`[http ${response.status()}] ${response.request().method()} ${response.url()}`)
  }
})
await page.addInitScript(() => {
  document.addEventListener('securitypolicyviolation', (event) => {
    console.error(`CSP violation: ${event.violatedDirective} blocked ${event.blockedURI || '(inline)'} at ${event.sourceFile}:${event.lineNumber}:${event.columnNumber} sample=${event.sample}`)
  })
})

await page.goto(`${BASE}/#/login`, { waitUntil: 'networkidle' })
await page.fill('input[name="username"]', username)
await page.fill('input[name="password"]', password)
await page.click('#login-form button[type="submit"]')
await page.waitForTimeout(1500)
if (route !== '/') {
  await page.goto(`${BASE}/#${route}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1200)
}
const info = await page.evaluate(() => ({
  url: location.href,
  title: document.title,
  menu: Array.from(document.querySelectorAll('.sub-sidebar-container .menu [title], .sub-sidebar-container .menu span')).map(item => item.textContent?.trim()).filter(Boolean).slice(0, 40),
  mainMenu: Array.from(document.querySelectorAll('.main-sidebar-container .menu span')).map(item => item.textContent?.trim()).filter(Boolean),
  heading: document.querySelector('#app-content h1, #app-content .text-2xl')?.textContent?.trim(),
  overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
}))
console.log(JSON.stringify(info, null, 2))
if (shot) {
  await page.screenshot({ path: shot, fullPage: false })
}
console.log(problems.length ? problems.join('\n') : 'no console/CSP/HTTP problems')
await browser.close()
