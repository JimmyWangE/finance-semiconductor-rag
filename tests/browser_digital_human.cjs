const { chromium } = require('playwright')
const assert = require('node:assert/strict')

const url = process.env.RAG_URL || 'http://127.0.0.1:8091';

(async () => {
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox'],
    ...(process.env.PW_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PW_CHROMIUM_EXECUTABLE_PATH }
      : {}),
  })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    const response = await page.goto(`${url}/digital-human/`, { waitUntil: 'networkidle', timeout: 120000 })
    assert.equal(response?.status(), 200)
    await page.waitForTimeout(2500)
    assert.equal(await page.locator('#app').count(), 1)
    assert.equal(await page.locator('canvas').count() > 0, true)
    await page.screenshot({ path: process.env.RAG_SCREENSHOT || 'artifacts/digital-human.png', fullPage: true })
    assert.deepEqual(errors, [])
    console.log(JSON.stringify({ status: response.status(), canvases: await page.locator('canvas').count(), errors }))
  }
  finally {
    await browser.close()
  }
})().catch(error => {
  console.error(error)
  process.exit(1)
})
