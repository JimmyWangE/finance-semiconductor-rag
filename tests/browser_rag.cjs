const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  const root = path.join(__dirname, '../data/rag/eval/screenshots');
  fs.mkdirSync(root, {recursive: true});
  const results = [];
  try {
    for (const viewport of [{width: 1440, height: 1000}, {width: 390, height: 844}]) {
      const page = await browser.newPage({viewport});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(process.env.RAG_URL || 'http://127.0.0.1:8090');
      await page.waitForFunction(() => document.getElementById('health').textContent === '服务在线');
      await page.locator('#query').fill('中芯国际2025年第三季度产能利用率是多少？');
      await page.locator('#submit').click();
      await page.waitForFunction(() => !document.getElementById('submit').disabled, {timeout: 180000});
      assert.match(await page.locator('#answer').innerText(), /95\.8/);
      assert.equal(await page.locator('#error').innerText(), '');
      assert.ok(await page.locator('.evidence').count() > 0);
      assert.ok(await page.locator('blockquote').count() > 0);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({path: path.join(root, `answer-${viewport.width}.png`), fullPage: true});
      await page.locator('input[value=search]').check();
      await page.locator('#kind').selectOption('chart');
      await page.locator('#query').fill('DRAM价格变化');
      await page.locator('#submit').click();
      await page.waitForFunction(() => !document.getElementById('submit').disabled, {timeout: 180000});
      await page.locator('details').first().locator('summary').click();
      await page.locator('img').first().scrollIntoViewIfNeeded();
      await page.waitForFunction(() => {const img=document.querySelector('img');return img && img.complete && img.naturalWidth > 0});
      const dimensions = await page.locator('img').first().evaluate(img => ({width: img.naturalWidth, height: img.naturalHeight}));
      assert.ok(dimensions.width > 100 && dimensions.height > 100);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({path: path.join(root, `chart-${viewport.width}.png`), fullPage: true});
      assert.deepEqual(errors, []);
      results.push({viewport, image: dimensions, errors});
      await page.close();
    }
    fs.writeFileSync(path.join(root, 'browser-results.json'), JSON.stringify(results, null, 2));
    console.log(JSON.stringify(results, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error);process.exit(1)});
