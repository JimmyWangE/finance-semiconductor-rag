const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');

(async()=>{
  const url=process.env.RAG_URL||'http://127.0.0.1:8091';
  const output=process.env.RAG_SCREENSHOTS;
  assert.ok(output);fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  try{
    await page.goto(url);await page.waitForFunction(()=>document.getElementById('health').textContent==='服务在线');
    async function send(query){
      await page.locator('#query').fill(query);
      const answer=page.waitForResponse(r=>r.url()===url+'/ask'&&r.request().method()==='POST',{timeout:180000});
      await page.locator('#submit').click();const r=await answer;assert.equal(r.status(),200);const data=await r.json();
      await page.waitForFunction(()=>!document.getElementById('submit').disabled,null,{timeout:180000});
      assert.equal(await page.locator('#error').innerText(),'');return data;
    }
    const first=await send('中芯国际2025年第三季度产能利用率是多少？');
    assert.match(first.answer,/95\.8/);
    const second=await send('它同期的营业收入是多少亿美元？');assert.match(second.answer,/23\.82/);
    assert.equal(await page.locator('.turn').count(),2);
    await page.screenshot({path:path.join(output,'conversation-desktop.png'),fullPage:true});
    await page.reload();await page.waitForFunction(()=>document.querySelectorAll('.turn').length===2);
    await page.locator('#rename-conversation').click();await page.locator('#rename-title').fill('晶圆代工研究');
    await page.locator('#rename-form button[type=submit]').click();
    await page.waitForFunction(()=>document.getElementById('conversation-title').textContent==='晶圆代工研究');
    await page.locator('#new-conversation').click();await page.waitForFunction(()=>document.querySelectorAll('.turn').length===0&&!document.getElementById('new-conversation').disabled);
    const ambiguous=await send('它的营收是多少？');assert.equal(ambiguous.status,'needs_clarification');
    await page.locator(`.session[data-id="${first.conversation_id}"]`).click();
    await page.waitForFunction(()=>document.querySelectorAll('.turn').length===2);
    await page.locator('.turn').first().getByRole('button',{name:'查看依据'}).click();
    assert.match(await page.locator('#source-count').innerText(),/第 1 轮/);
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:path.join(output,'conversation-mobile.png'),fullPage:true});
    const foreign=await browser.newContext();const other=await foreign.newPage();await other.goto(url);
    await other.waitForFunction(()=>document.getElementById('health').textContent==='服务在线');
    assert.equal(await other.locator('.session').count(),0);await foreign.close();
    page.once('dialog',dialog=>dialog.accept());await page.locator('#delete-conversation').click();
    await page.waitForFunction(()=>document.getElementById('conversation-title').textContent==='新会话');
    assert.equal(await page.locator(`.session[data-id="${first.conversation_id}"]`).count(),0);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({checks:['followup','reload','rename','new','clarification','switch','per_turn_citations','mobile','browser_isolation','delete'],errors}));
  }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
