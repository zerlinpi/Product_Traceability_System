const { chromium } = require('C:/Users/皮泽霖/AppData/Local/npm-cache/_npx/fd3bca3c548369c0/node_modules/playwright');

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1488, height: 1058 }, deviceScaleFactor: 1 });
  const consoleErrors = [];
  page.on('console', (message) => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) consoleErrors.push(message.text());
  });
  page.on('pageerror', (error) => consoleErrors.push(error.message));
  page.on('response', (response) => {
    const expectedInvalidScan = response.status() === 404 && response.url().endsWith('/api/scan-gun/lookup');
    if (response.status() >= 400 && !expectedInvalidScan) {
      consoleErrors.push(`HTTP ${response.status()} ${response.url()}`);
    }
  });

  await page.goto('http://127.0.0.1:5081/#dashboard', { waitUntil: 'networkidle' });
  await page.waitForSelector('#view-dashboard.active');
  await page.screenshot({ path: 'tests/ui-dashboard-final.png', fullPage: false });

  const desktop = await page.evaluate(() => {
    const hasBadText = Array.from(document.querySelectorAll('body *')).some((item) => {
      const text = item.childElementCount ? '' : (item.textContent || '');
      return /�|锟|USER[_ ]LOGIN|Traceability|当前页面/.test(text);
    });
    const tinyText = Array.from(document.querySelectorAll('body *')).filter((item) => {
      const style = getComputedStyle(item);
      const text = item.childElementCount ? '' : (item.textContent || '').trim();
      return text && item.checkVisibility() && Number.parseFloat(style.fontSize) < 11;
    }).slice(0, 12).map((item) => ({ text: item.textContent.trim().slice(0, 60), size: getComputedStyle(item).fontSize, className: item.className }));
    return {
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      hasBadText,
      tinyText,
      sidebarWidth: document.querySelector('.sidebar').getBoundingClientRect().width,
      railWidth: document.querySelector('.sidebar-rail').getBoundingClientRect().width,
      dashboardColumns: getComputedStyle(document.querySelector('#view-dashboard')).gridTemplateColumns,
      taskRows: document.querySelectorAll('#dashboard-task-list tr').length,
      activityRows: document.querySelectorAll('#dashboard-activity .activity-item').length,
    };
  });

  await page.click('#account-button');
  const accountOpened = await page.locator('#account-popover').isVisible();
  await page.keyboard.press('Escape');
  const accountClosed = !(await page.locator('#account-popover').isVisible());

  const batchNav = page.locator('.nav-button[data-view="batch-entry"][data-role="ADMIN"]');
  if (!(await batchNav.isVisible())) {
    await page.click('#sidebar-toggle');
    await batchNav.waitFor({ state: 'visible' });
  }
  await page.click('.nav-button[data-view="batch-entry"][data-role="ADMIN"]');
  await page.waitForSelector('#view-batch-entry.active');
  await page.waitForTimeout(350);
  const batchScannerFocused = await page.evaluate(() => document.activeElement?.id === 'batch-entry-code');
  await page.screenshot({ path: 'tests/ui-batch-entry-final.png', fullPage: false });

  await page.click('.nav-button[data-view="scan-gun"][data-role="ADMIN"]');
  await page.waitForSelector('#view-scan-gun.active');
  await page.waitForTimeout(350);
  const inboundScannerFocused = await page.evaluate(() => document.activeElement?.id === 'scan-gun-code');
  await page.locator('#scan-gun-code').fill('NOT-A-PRODUCTION-CODE');
  await page.locator('#scan-gun-code').press('Enter');
  await page.waitForTimeout(350);
  const inboundErrorRefocused = await page.evaluate(() => document.activeElement?.id === 'scan-gun-code');
  await page.screenshot({ path: 'tests/ui-scan-gun-final.png', fullPage: false });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:5081/?qa=mobile#dashboard', { waitUntil: 'networkidle' });
  await page.waitForSelector('#view-dashboard.active');
  const mobileLayout = await page.evaluate(() => ({
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    offenders: Array.from(document.querySelectorAll('body *')).filter((item) => {
      if (!item.checkVisibility()) return false;
      const box = item.getBoundingClientRect();
      return box.right > document.documentElement.clientWidth + 1 || box.left < -1;
    }).slice(0, 12).map((item) => ({ tag: item.tagName, id: item.id, className: item.className, box: item.getBoundingClientRect().toJSON() })),
  }));
  await page.screenshot({ path: 'tests/ui-mobile-final.png', fullPage: false });
  await page.click('#sidebar-toggle');
  await page.waitForTimeout(250);
  const drawer = await page.locator('#app-sidebar').boundingBox();
  await page.screenshot({ path: 'tests/ui-mobile-drawer-final.png', fullPage: false });

  console.log(JSON.stringify({ desktop, accountOpened, accountClosed, batchScannerFocused, inboundScannerFocused, inboundErrorRefocused, mobileLayout, drawer, consoleErrors }));
  await Promise.race([browser.close(), new Promise((resolve) => setTimeout(resolve, 5000))]);
  process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
