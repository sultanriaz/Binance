const puppeteer = require('puppeteer-core');
const path = require('path');
const crypto = require('crypto');

function base32Decode(str) {
  const b32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  str = str.toUpperCase().replace(/=+$/, '');
  let bits = '';
  for (let c of str) bits += b32.indexOf(c).toString(2).padStart(5, '0');
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}

function getTOTP(secret, offset = 0) {
  const key = base32Decode(secret);
  const counter = Math.floor(((Date.now() / 1000) + offset) / 30);
  const buf = Buffer.alloc(8);
  buf.writeBigUInt64BE(BigInt(counter), 0);
  const hmac = crypto.createHmac('sha1', key).update(buf).digest();
  const offsetByte = hmac[hmac.length - 1] & 0x0f;
  return ((hmac.readUInt32BE(offsetByte) & 0x7fffffff) % 1000000).toString().padStart(6, '0');
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--window-size=1920,1080'],
    defaultViewport: { width: 1920, height: 1080 }
  });
  const page = await browser.newPage();
  await page.goto('http://127.0.0.1:8000/', { waitUntil: 'networkidle2' });
  await page.type('#user', 'audit_lead_admin');
  await page.type('#password', 'AuditorLeadPasswd2026!#');
  await page.type('#otp', getTOTP('DV6GJI4NKB7CX57OMQIFHU4XIN5CS4YH', 30));
  await page.click('#loginForm button');
  await page.waitForSelector('#app:not(.hidden)', { timeout: 8000 });
  await new Promise(r => setTimeout(r, 1000));

  // Navigate to Research Lab
  await page.click('nav button[data-page="research"]');
  await new Promise(r => setTimeout(r, 1000));
  await page.screenshot({ path: path.join(__dirname, '../docs/audit_evidence/15_research_shadow_model_active.png') });
  console.log('Saved 15_research_shadow_model_active.png');

  // Test clicking Forecast button in UI
  const forecastBtn = await page.$('#modelRows button');
  if (forecastBtn) {
    await forecastBtn.click();
    await new Promise(r => setTimeout(r, 600));
    const msg = await page.$eval('#researchMessage', el => el.textContent);
    console.log('Forecast button UI output:', msg);
  }

  await browser.close();
})();

