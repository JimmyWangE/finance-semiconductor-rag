const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');

(async()=>{
  const base=process.env.RAG_URL||'http://127.0.0.1:8090';
  const output=process.env.RAG_SCREENSHOTS;
  assert.ok(output);
  fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const checks=[];
  try{
    for(const viewport of [{width:1440,height:1000},{width:375,height:812}]){
      const context=await browser.newContext({viewport,reducedMotion:'reduce',locale:'zh-CN'});
      const page=await context.newPage();
      const errors=[];
      page.on('pageerror',error=>errors.push(error.message));
      await page.goto(base+'/chat');
      await page.waitForFunction(()=>document.getElementById('health').textContent==='服务在线');
      await page.waitForFunction(()=>!document.getElementById('submit').disabled);
      assert.equal(await page.locator('.primary-nav .nav-item').count(),3);
      assert.equal(await page.locator('.suggestion').count(),4);
      assert.equal(await page.locator('.mode-boundary').innerText(),'不连接研报库');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      const geometry=await page.$eval('.general-main',node=>({
        width:node.getBoundingClientRect().width,
        viewport:innerWidth,
        threadHeight:document.getElementById('thread').getBoundingClientRect().height,
      }));
      assert.ok(geometry.width<=geometry.viewport&&geometry.threadHeight>200);
      if(viewport.width===375){
        assert.equal(await page.locator('.sidebar').isVisible(),false);
        await page.locator('#toggle-history').click();
        assert.equal(await page.locator('.sidebar').isVisible(),true);
        assert.equal(await page.locator('#toggle-history').getAttribute('aria-expanded'),'true');
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('.sidebar').isVisible(),false);
      }
      await page.screenshot({path:path.join(output,`general-${viewport.width}.png`),fullPage:true});
      if(viewport.width===1440){
        await page.locator('#query').fill('请只回答：通用工作台可用');
        const answer=page.waitForResponse(response=>response.url()===base+'/general/ask/stream'&&response.request().method()==='POST',{timeout:180000});
        await page.locator('#submit').click();
        const response=await answer;
        assert.equal(response.status(),200);
        assert.match(response.headers()['content-type'],/application\/x-ndjson/);
        await page.waitForFunction(()=>!document.getElementById('submit').disabled,null,{timeout:180000});
        assert.equal(await page.locator('.turn').count(),1);
        assert.equal(await page.locator('#error').innerText(),'');
        await page.screenshot({path:path.join(output,'general-answered.png'),fullPage:true});
        page.once('dialog',dialog=>dialog.accept());
        await page.locator('#delete-conversation').click();
        await page.waitForFunction(()=>document.getElementById('conversation-title').textContent==='新会话');
      }
      assert.deepEqual(errors,[]);
      checks.push({viewport,geometry,errors});
      await context.close();
    }
    fs.writeFileSync(path.join(output,'general-results.json'),JSON.stringify(checks,null,2));
    console.log(JSON.stringify(checks));
  }finally{
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exit(1)});
