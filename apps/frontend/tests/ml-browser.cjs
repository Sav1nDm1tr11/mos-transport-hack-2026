const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const context=await browser.newContext();
  const page=await context.newPage();
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.TEST_BASE_URL);
  await page.locator('#token').fill(process.env.TEST_API_TOKEN);
  await page.locator('#connect').click();
  await page.locator('#login').waitFor({state:'hidden'});
  await page.locator('#run').fill('demo');
  await page.locator('#run-form button').click();
  await page.locator('.vehicle').filter({hasText:'demo-bus'}).click();
  await page.waitForFunction(()=>document.querySelector('.prediction-card strong')?.textContent.includes('+2,2'));
  assert.equal(await page.locator('#forecast').textContent(),'1');
  assert.match(await page.locator('.prediction-card').textContent(),/TEST-ONLY-simulator-1/);
  assert.match(await page.locator('.prediction-card').textContent(),/demo-stop/);
  await page.locator('#check-ml').click();
  await page.locator('#ml-check-result').filter({hasText:'Готова:'}).waitFor();
  // Time must expire the displayed result with both HTTP and SSE unavailable.
  await context.setOffline(true);
  await page.evaluate(()=>{const real=Date.now;Date.now=()=>real()+100000;});
  await page.locator('.prediction-card strong').filter({hasText:'Прогноз устарел'}).waitFor();
  assert.equal(await page.locator('#forecast').textContent(),'—');
  assert.deepEqual(errors,[]);
  console.log('PASS: real ML forecast, current coverage, target/model details, explicit model check, expiry during outage.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
