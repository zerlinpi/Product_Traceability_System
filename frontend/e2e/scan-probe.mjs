// Scan-gun flow probe: batch entry + scan-gun stock-in, driven by keyboard only.
import process from 'node:process'
import { chromium } from 'playwright-core'

const BASE = process.env.PTS_BASE_URL || 'http://127.0.0.1:5090'
const browser = await chromium.launch({ channel: 'msedge', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
const problems = []
page.on('console', (message) => {
  if (['error', 'warning'].includes(message.type()) && !message.text().includes('401')) {
    problems.push(`[console.${message.type()}] ${message.text()}`)
  }
})
page.on('pageerror', error => problems.push(`[pageerror] ${error.message}`))

await page.goto(`${BASE}/#/login`, { waitUntil: 'networkidle' })
await page.fill('input[name="username"]', 'warehouse.demo')
await page.fill('input[name="password"]', 'Dev@Store#2026')
await page.keyboard.press('Enter')
await page.waitForURL(/#\/batch-gen/, { timeout: 10000 })

const batches = await page.evaluate(async () => (await (await fetch('/api/production-batches')).json()).data)
const pending = batches.find(item => !item.registered)
console.log('unregistered batch:', pending?.batchCode)

await page.goto(`${BASE}/#/batch-entry`, { waitUntil: 'networkidle' })
await page.waitForTimeout(600)
const focusedId = await page.evaluate(() => document.activeElement?.closest('#batch-entry-code') ? 'batch-entry-code' : document.activeElement?.tagName)
console.log('focus on entry:', focusedId)

// Simulate a scan gun: focus moved to a button, then the gun types + Enter.
await page.focus('#batch-entry-reset')
await page.keyboard.type(pending.batchCode, { delay: 5 })
await page.keyboard.press('NumpadEnter')
await page.waitForTimeout(1200)
const feedback = await page.textContent('#batch-entry-feedback')
const focusAfter = await page.evaluate(() => Boolean(document.activeElement?.closest('#batch-entry-code')))
console.log('feedback after scan:', feedback?.replace(/\s+/g, ' ').trim(), '| refocused:', focusAfter)

// Duplicate scan must be rejected and focus must come back.
await page.keyboard.type(pending.batchCode, { delay: 5 })
await page.keyboard.press('Enter')
await page.waitForTimeout(1200)
console.log('feedback duplicate:', (await page.textContent('#batch-entry-feedback'))?.replace(/\s+/g, ' ').trim(), '| refocused:', await page.evaluate(() => Boolean(document.activeElement?.closest('#batch-entry-code'))))

// Scan-gun stock-in for the seeded production order.
const orders = await page.evaluate(async () => (await (await fetch('/api/production-orders')).json()).data)
const orderCode = orders[0]?.productionQrCode
console.log('production QR:', orderCode)
await page.goto(`${BASE}/#/scan-gun`, { waitUntil: 'networkidle' })
await page.waitForTimeout(600)
console.log('focus on scan-gun:', await page.evaluate(() => Boolean(document.activeElement?.closest('#scan-gun-code'))))
await page.keyboard.type(orderCode, { delay: 5 })
await page.keyboard.press('Enter')
await page.waitForTimeout(1200)
console.log('lookup feedback:', (await page.textContent('#scan-gun-feedback'))?.replace(/\s+/g, ' ').trim())
console.log('focus on quantity:', await page.evaluate(() => Boolean(document.activeElement?.closest('#scan-gun-quantity'))))
const quantityValue = await page.evaluate(() => document.querySelector('#scan-gun-quantity input')?.value)
console.log('quantity prefill:', quantityValue)
await page.keyboard.press('Enter')
await page.waitForTimeout(1500)
console.log('after inbound focus on code:', await page.evaluate(() => Boolean(document.activeElement?.closest('#scan-gun-code'))))
console.log('records rows:', await page.locator('#scan-gun-record-table .el-table__row').count())
await page.screenshot({ path: process.env.SHOT || 'scan-gun.png' })

console.log(problems.length ? problems.join('\n') : 'no console problems')
await browser.close()
