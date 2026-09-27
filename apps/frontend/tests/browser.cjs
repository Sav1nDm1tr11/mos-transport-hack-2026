// Run against a disposable backend: TEST_BASE_URL and TEST_API_TOKEN are required.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
(async()=>{
 const base=process.env.TEST_BASE_URL,token=process.env.TEST_API_TOKEN;
 if(!base||!token)throw new Error('Use a disposable backend; set TEST_BASE_URL and TEST_API_TOKEN');
 const api=async(path,method='GET',body)=>{const r=await fetch(base+'/api/v1/'+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});assert.ok(r.ok,`${path}: ${r.status}`);return r.json();};
 const run='ui-'+Date.now();
 const buses=[['test-102','1102',55.755,37.617,34],['test-024','1024',55.772,37.645,22],['test-008','1008',55.738,37.597,18]];
 for(const [id,unit,lat,lon,speed] of buses){await api('devices/'+unit,'PUT',{tr_id:id});const t=new Date(Date.now()-(id==='test-008'?600000:0)).toISOString();await api('telemetry','POST',{run_id:run,event_id:run+id,source:'scenario',tr_id:id,unit_id:unit,event_time:t,receive_time:new Date().toISOString(),ingested_at:new Date().toISOString(),lat,lon,speed_kmh:speed,location_valid:true});}
 const browser=await chromium.launch({headless:true});
 try {
 const context=await browser.newContext({viewport:{width:1512,height:982}});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base);await page.locator('#token').fill('invalid-token');await page.locator('#connect').click();await page.getByText('Неверный токен доступа',{exact:true}).waitFor();
 await page.locator('#token').fill(token);await page.locator('#connect').click();await page.locator('#login').waitFor({state:'hidden'});
 await page.locator('#run').fill(run);await page.locator('#run-form button').click();await page.locator('.vehicle').filter({hasText:'test-102'}).waitFor();
 assert.equal(await page.locator('#total').textContent(),'3');assert.equal(await page.locator('#fresh').textContent(),'2');
 await page.locator('.vehicle').filter({hasText:'test-102'}).click();await page.locator('#details h3').filter({hasText:'test-102'}).waitFor();await page.locator('.prediction-card strong').filter({hasText:'Недоступен'}).waitFor();
 await page.locator('#search').fill('024');assert.equal(await page.locator('.vehicle').count(),1);await page.locator('#search').fill('');
 await page.locator('[data-filter="stale"]').click();assert.equal(await page.locator('.vehicle').count(),1);await page.locator('[data-filter="all"]').click();
 // Real API -> worker -> SSE -> browser, faster than the 15-second poll.
 const before=Date.now(),t=new Date().toISOString();await api('telemetry','POST',{run_id:run,event_id:run+'update',source:'scenario',tr_id:'test-102',unit_id:'1102',event_time:t,receive_time:t,ingested_at:t,lat:55.759,lon:37.63,speed_kmh:47,location_valid:true});
 await page.waitForFunction(()=>document.querySelector('.speed strong')?.textContent.includes('47'),{},{timeout:9000});assert.ok(Date.now()-before<10000);
 await page.locator('#focus-vehicle').click();
 await page.locator('.tab[data-page="predictions"]').click();await page.locator('#predictions-page').waitFor({state:'visible'});
 const downloadPromise=page.waitForEvent('download');await page.locator('#export').click();const download=await downloadPromise;assert.equal(download.suggestedFilename(),'transport-predictions.csv');
 await page.locator('.tab[data-page="issues"]').click();await page.locator('#issues-page').waitFor({state:'visible'});
 await page.locator('.tab[data-page="monitor"]').click();
 await page.waitForFunction(()=>![...document.querySelectorAll('.tiles img')].some(i=>!i.complete),{},{timeout:15000}).catch(()=>{});
 if(process.env.SCREENSHOT_DIR)await page.screenshot({path:process.env.SCREENSHOT_DIR+'/dashboard-desktop.png',fullPage:true});
 await context.setOffline(true);await page.locator('#refresh').click();await page.locator('#notice').waitFor({state:'visible'});assert.match(await page.locator('#notice').textContent(),/последний/);
 await context.setOffline(false);await page.locator('#refresh').click();await page.locator('#notice').waitFor({state:'hidden'});assert.notEqual(await page.locator('#connection').textContent(),'Нет связи');
 await page.setViewportSize({width:390,height:844});await page.waitForTimeout(300);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.waitForFunction(()=>![...document.querySelectorAll('.tiles img')].some(i=>!i.complete),{},{timeout:15000}).catch(()=>{});
 if(process.env.SCREENSHOT_DIR)await page.screenshot({path:process.env.SCREENSHOT_DIR+'/dashboard-mobile.png',fullPage:true});
 await page.locator('#logout').click();await page.locator('#login').waitFor({state:'visible'});assert.equal(await page.locator('.vehicle').count(),0);assert.equal(await page.locator('#token').inputValue(),'');assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);
 // A server resync arriving during a snapshot must reopen SSE, even if that snapshot fails.
 for(const failSnapshot of [false,true]) {
   const resyncPage=await context.newPage();let snapshots=0,streams=0,release;
   const busy=new Promise(resolve=>{release=resolve;});
   await resyncPage.route('**/api/v1/dashboard?**',async route=>{
     snapshots++;if(snapshots===2){release();await new Promise(r=>setTimeout(r,300));if(failSnapshot){await route.fulfill({status:503,contentType:'application/json',body:'{"error_code":"storage_unavailable"}'});return;}}
     await route.continue();
   });
   await resyncPage.route('**/api/v1/events?**',async route=>{
     streams++;if(streams===1){await busy;await route.fulfill({status:200,contentType:'text/event-stream',body:'event: resync_required\ndata: {}\n\n'});}else await route.continue();
   });
   await resyncPage.goto(base);await resyncPage.locator('#token').fill(token);await resyncPage.locator('#connect').click();await resyncPage.locator('#login').waitFor({state:'hidden'});
   await resyncPage.locator('#refresh').click();
   for(let i=0;i<70&&streams<2;i++)await new Promise(r=>setTimeout(r,100));
   assert.ok(streams>=2,`SSE must reopen after resync during busy refresh (failure=${failSnapshot})`);
   await resyncPage.close();
 }
 assert.deepEqual(errors,[]);console.log('PASS: auth, snapshot, selection, search, freshness filter, real SSE update, history, diagnostics, CSV, offline/recovery, mobile layout, logout, concurrent SSE resync and failed-resync recovery.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
