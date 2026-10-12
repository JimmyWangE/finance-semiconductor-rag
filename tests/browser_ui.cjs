const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
(async()=>{
  const base=process.env.RAG_URL||'http://127.0.0.1:8091';
  const output=process.env.RAG_SCREENSHOTS;assert.ok(output);fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const checks=[];
  try{
    for(const viewport of [{width:1440,height:1000},{width:1024,height:768},{width:768,height:1024},{width:375,height:812},{width:844,height:390}]){
      const page=await browser.newPage({viewport,reducedMotion:'reduce'});const errors=[];page.on('pageerror',e=>errors.push(e.message));
      await page.goto(base);await page.waitForFunction(()=>document.getElementById('health').textContent==='服务在线');
      await page.waitForFunction(()=>!document.getElementById('submit').disabled);
      assert.equal(await page.locator('.suggestion').count(),4);
      await page.locator('.suggestion').first().click();assert.match(await page.locator('#query').inputValue(),/中芯国际/);
      await page.locator('.filter-panel>summary').click();
      await page.locator('#org').selectOption({label:'东海证券'});assert.equal(await page.locator('#filter-count').innerText(),'1');
      await page.locator('.filter-panel>summary').click();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.screenshot({path:path.join(output,`research-${viewport.width}.png`),fullPage:true});
      const asset=await page.request.get(base+'/ui/icons/microchip.svg');assert.equal(asset.status(),200);
      if(viewport.width===375){await page.locator('#toggle-history').click();assert.equal(await page.locator('#toggle-history').getAttribute('aria-expanded'),'true');assert.equal(await page.locator('.sidebar').isVisible(),true);await page.keyboard.press('Escape');assert.equal(await page.locator('.sidebar').isVisible(),false)}
      await page.goto(base+'/library');await page.waitForFunction(()=>document.getElementById('stat-documents').textContent!=='—');
      await page.locator('#tab-corpus').click();await page.locator('#document-search').fill('东海证券');
      await page.waitForFunction(()=>[...document.querySelectorAll('#document-rows tr')].every(row=>row.textContent.includes('东海证券')));
      assert.ok(await page.locator('#document-rows tr').count()>0);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.screenshot({path:path.join(output,`library-${viewport.width}.png`),fullPage:true});
      assert.deepEqual(errors,[]);checks.push({viewport,errors});await page.close();
    }
    fs.writeFileSync(path.join(output,'responsive-results.json'),JSON.stringify(checks,null,2));console.log(JSON.stringify(checks));
  }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
