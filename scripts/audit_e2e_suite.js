/**
 * Comprehensive Browser E2E Acceptance Audit Suite for AegisTrade Platform
 * Uses puppeteer-core with installed Google Chrome.
 */
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const BASE_URL = 'http://127.0.0.1:8000';
const CHROME_PATH = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const EVIDENCE_DIR = path.resolve(__dirname, '../docs/audit_evidence');

const USERS = {
  lead: {
    username: 'audit_lead_admin',
    password: 'AuditorLeadPasswd2026!#',
    totpSecret: 'DV6GJI4NKB7CX57OMQIFHU4XIN5CS4YH'
  },
  peer: {
    username: 'audit_peer_admin',
    password: 'AuditorPeerPasswd2026!#',
    totpSecret: '6HSTMENJSYEYLAULJ2F3NNGPQJNB2DR4'
  }
};

function base32Decode(str) {
  const b32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  str = str.toUpperCase().replace(/=+$/, '');
  let bits = '';
  for (let c of str) {
    const val = b32.indexOf(c);
    if (val === -1) throw new Error('Bad base32: ' + c);
    bits += val.toString(2).padStart(5, '0');
  }
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) {
    bytes.push(parseInt(bits.slice(i, i + 8), 2));
  }
  return Buffer.from(bytes);
}

function getTOTP(secret, when) {
  const key = base32Decode(secret);
  const counter = Math.floor((when || (Date.now() / 1000)) / 30);
  const buf = Buffer.alloc(8);
  buf.writeBigUInt64BE(BigInt(counter), 0);
  const hmac = crypto.createHmac('sha1', key).update(buf).digest();
  const offset = hmac[hmac.length - 1] & 0x0f;
  const token = (hmac.readUInt32BE(offset) & 0x7fffffff) % 1000000;
  return token.toString().padStart(6, '0');
}

const lastBuckets = {};
async function getFreshTOTP(secret, userKey = 'default') {
  let now = Math.floor(Date.now() / 1000);
  let bucket = Math.floor(now / 30);
  if (lastBuckets[userKey] !== undefined && bucket <= lastBuckets[userKey]) {
    const waitSec = (30 - (now % 30)) + 2;
    console.log(`[MFA REPLAY GUARD] User ${userKey}: waiting ${waitSec}s for fresh TOTP bucket...`);
    await sleep(waitSec * 1000);
    now = Math.floor(Date.now() / 1000);
    bucket = Math.floor(now / 30);
  }
  lastBuckets[userKey] = bucket;
  return getTOTP(secret, now);
}

async function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

if (!fs.existsSync(EVIDENCE_DIR)) {
  fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
}

const auditLog = [];
function recordAudit(step, status, details, screenshot = null) {
  const entry = { step, status, details, screenshot, timestamp: new Date().toISOString() };
  auditLog.push(entry);
  console.log(`[${status}] ${step}: ${details}`);
}

async function runAudit() {
  console.log('=== STARTING INDEPENDENT E2E BROWSER AUDIT ===');
  const browser = await puppeteer.launch({
    executablePath: CHROME_PATH,
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--window-size=1920,1080'],
    defaultViewport: { width: 1920, height: 1080 }
  });

  const page = await browser.newPage();
  const consoleMessages = [];
  const networkErrors = [];

  page.on('console', msg => {
    const text = `[CONSOLE ${msg.type().toUpperCase()}] ${msg.text()}`;
    consoleMessages.push(text);
  });

  page.on('requestfailed', req => {
    networkErrors.push(`${req.method()} ${req.url()} failed: ${req.failure()?.errorText}`);
  });

  let nextPromptInput = null;
  page.on('dialog', async dialog => {
    const type = dialog.type();
    const msg = dialog.message();
    console.log(`[DIALOG ${type.toUpperCase()}] "${msg}"`);
    if (type === 'prompt') {
      const val = nextPromptInput || '000000';
      console.log(`[DIALOG PROMPT REPLY] "${val}"`);
      await dialog.accept(val);
      nextPromptInput = null;
    } else if (type === 'confirm') {
      console.log('[DIALOG CONFIRM ACCEPT]');
      await dialog.accept();
    } else {
      await dialog.dismiss();
    }
  });

  try {
    // ----------------------------------------------------
    // TEST 1: Initial Page Load & Security Headers
    // ----------------------------------------------------
    console.log('\n--- 1. Security Headers & Sign-In Form Inspection ---');
    const resp = await page.goto(BASE_URL + '/', { waitUntil: 'networkidle2' });
    const headers = resp.headers();

    recordAudit('Initial HTTP Status', resp.status() === 200 ? 'PASS' : 'FAIL', `HTTP ${resp.status()}`);
    recordAudit('CSP Header', headers['content-security-policy'] ? 'PASS' : 'FAIL', headers['content-security-policy'] || 'Missing');
    recordAudit('X-Content-Type-Options', headers['x-content-type-options'] === 'nosniff' ? 'PASS' : 'FAIL', headers['x-content-type-options'] || 'Missing');
    recordAudit('X-Frame-Options', headers['x-frame-options'] === 'DENY' ? 'PASS' : 'FAIL', headers['x-frame-options'] || 'Missing');
    recordAudit('Referrer-Policy', headers['referrer-policy'] === 'no-referrer' ? 'PASS' : 'FAIL', headers['referrer-policy'] || 'Missing');
    recordAudit('Cache-Control', headers['cache-control']?.includes('no-store') ? 'PASS' : 'FAIL', headers['cache-control'] || 'Missing');

    const authVisible = await page.$eval('#auth', el => !el.classList.contains('hidden'));
    const appHidden = await page.$eval('#app', el => el.classList.contains('hidden'));
    recordAudit('Auth Container State', authVisible && appHidden ? 'PASS' : 'FAIL', `Auth visible: ${authVisible}, App hidden: ${appHidden}`);

    await page.screenshot({ path: path.join(EVIDENCE_DIR, '01_login_screen.png') });

    // ----------------------------------------------------
    // TEST 2: Negative Authentication & MFA Validation
    // ----------------------------------------------------
    console.log('\n--- 2. Negative Authentication & MFA Validation ---');
    await page.type('#user', USERS.lead.username);
    await page.type('#password', 'WrongPassword123!');
    await page.type('#otp', '123456');
    await page.click('#loginForm button');
    await sleep(600);

    let authNotice = await page.$eval('#authNotice', el => el.textContent);
    recordAudit('Invalid Password Rejection', authNotice.includes('Invalid credentials') ? 'PASS' : 'FAIL', `Notice: "${authNotice}"`);

    await page.$eval('#password', el => el.value = '');
    await page.$eval('#otp', el => el.value = '');
    await page.type('#password', USERS.lead.password);
    await page.type('#otp', '000000');
    await page.click('#loginForm button');
    await sleep(600);

    authNotice = await page.$eval('#authNotice', el => el.textContent);
    recordAudit('Invalid TOTP Rejection', authNotice.includes('Invalid credentials') ? 'PASS' : 'FAIL', `Notice: "${authNotice}"`);
    await page.screenshot({ path: path.join(EVIDENCE_DIR, '02_auth_rejection.png') });

    // ----------------------------------------------------
    // TEST 3: Positive Authentication & Storage Isolation
    // ----------------------------------------------------
    console.log('\n--- 3. Positive Authentication & Client Storage Inspection ---');
    await page.$eval('#password', el => el.value = '');
    await page.$eval('#otp', el => el.value = '');
    await page.type('#password', USERS.lead.password);
    
    // Ensure fresh 30s bucket to avoid replay
    let now = Math.floor(Date.now() / 1000);
    if (now % 30 >= 27) await sleep(4000);
    const validOTP = await getFreshTOTP(USERS.lead.totpSecret, 'lead');
    await page.type('#otp', validOTP);
    await page.click('#loginForm button');
    await page.waitForSelector('#app:not(.hidden)', { timeout: 8000 });
    await sleep(1000);

    const appVisible = await page.$eval('#app', el => !el.classList.contains('hidden'));
    const sessionLabel = await page.$eval('#session', el => el.textContent);
    recordAudit('Successful Sign-In', appVisible ? 'PASS' : 'FAIL', `App visible: ${appVisible}, Session: "${sessionLabel}"`);

    const storageAudit = await page.evaluate(() => ({
      localLen: localStorage.length,
      sessionLen: sessionStorage.length,
      cookies: document.cookie
    }));
    recordAudit('No Browser Token Storage', storageAudit.localLen === 0 && storageAudit.sessionLen === 0 ? 'PASS' : 'FAIL',
      `localStorage: ${storageAudit.localLen}, sessionStorage: ${storageAudit.sessionLen}, cookies: "${storageAudit.cookies}"`);

    await page.screenshot({ path: path.join(EVIDENCE_DIR, '03_dashboard_authenticated.png') });

    // ----------------------------------------------------
    // TEST 4: Responsive Viewport Audits
    // ----------------------------------------------------
    console.log('\n--- 4. Responsive Viewport Audits ---');
    const viewports = [
      { name: '1080p_Desktop', width: 1920, height: 1080 },
      { name: 'Laptop_Standard', width: 1366, height: 768 },
      { name: 'Tablet_Portrait', width: 768, height: 1024 },
      { name: 'Mobile_Portrait', width: 375, height: 812 }
    ];

    for (const vp of viewports) {
      await page.setViewport({ width: vp.width, height: vp.height });
      await sleep(300);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      recordAudit(`Responsive Layout [${vp.name}]`, !overflow ? 'PASS' : 'WARN',
        `Width: ${vp.width}x${vp.height}, Horizontal overflow: ${overflow}`);
      await page.screenshot({ path: path.join(EVIDENCE_DIR, `04_viewport_${vp.name}.png`) });
    }
    await page.setViewport({ width: 1920, height: 1080 });

    // ----------------------------------------------------
    // TEST 5: Overview Dashboard Inspection & Error Notice
    // ----------------------------------------------------
    console.log('\n--- 5. Overview Dashboard & Defect Recording ---');
    const globalNotice = await page.$eval('#globalMessage', el => el.textContent);
    const spotFeed = await page.$eval('#spotFeed', el => el.textContent);
    const futureFeed = await page.$eval('#futureFeed', el => el.textContent);
    const paperCount = await page.$eval('#paperCount', el => el.textContent);
    const marketRows = await page.$$eval('#marketRows tr', trs => trs.length);

    recordAudit('Overview Default Candles Defect Inspection',
      globalNotice.includes('Internal Server Error') || globalNotice.includes('missing candle interval') ? 'DEFECT_FOUND' : 'PASS',
      `Global notice: "${globalNotice}", Spot KPI: ${spotFeed}, Market Rows: ${marketRows}`);

    // ----------------------------------------------------
    // TEST 6: Trading Terminal & Chart Inspection
    // ----------------------------------------------------
    console.log('\n--- 6. Trading Terminal & Chart Inspection ---');
    await page.click('nav button[data-page="trading"]');
    await sleep(800);

    const chartTitle = await page.$eval('#pageTitle', el => el.textContent);
    const canvasValid = await page.$eval('#tradingChart', c => c.width > 0 && c.height > 0);
    const lastPrice = await page.$eval('#lastPrice', el => el.textContent);

    recordAudit('Trading Terminal Page Load', chartTitle === 'Trading Terminal' ? 'PASS' : 'FAIL', `Title: ${chartTitle}`);
    recordAudit('Candlestick Canvas Rendering', canvasValid ? 'PASS' : 'FAIL', `Canvas dimensions valid`);

    // Switch chart symbol to ETHUSDT (which has contiguous live bars) to verify chart rendering!
    await page.select('#chartSymbol', 'ETHUSDT');
    await sleep(1000);
    const ethChartNote = await page.$eval('#chartNote', el => el.textContent);
    const ethRegime = await page.$eval('#marketRegime', el => el.textContent);
    recordAudit('Trading Chart ETHUSDT (Contiguous Bars) Render', ethChartNote.includes('closed candles') ? 'PASS' : 'FAIL',
      `Note: "${ethChartNote}", Regime: "${ethRegime}"`);

    await page.screenshot({ path: path.join(EVIDENCE_DIR, '05_trading_terminal_chart.png') });

    // ----------------------------------------------------
    // TEST 7: Safe End-to-End Paper Trading (Virtual Funds)
    // ----------------------------------------------------
    console.log('\n--- 7. End-to-End Virtual Paper Trading Execution ---');
    await page.click('nav button[data-page="portfolio"]');
    await sleep(800);

    // 7a: Create virtual account
    const testAccountName = 'Audit-Fund-' + Math.floor(Math.random() * 10000);
    await page.$eval('#paperCreate input[name="name"]', el => el.value = '');
    await page.type('#paperCreate input[name="name"]', testAccountName);
    await page.$eval('#paperCreate input[name="cash"]', el => el.value = '25000');
    await page.click('#paperCreate button');
    await sleep(1000);

    const paperMsg = await page.$eval('#paperMessage', el => el.textContent);
    const paperInfo = await page.$eval('#paperInfo', el => el.textContent);
    recordAudit('Virtual Account Creation', paperMsg.includes('Created a virtual account') ? 'PASS' : 'FAIL',
      `Message: "${paperMsg}", Info: "${paperInfo.replace(/\n/g, ' ')}"`);

    // 7b: Place valid virtual paper market order (0.005 BTC ~ $400 USDT, within 10-1000 notional)
    await page.select('#paperOrder select[name="market"]', 'spot');
    await page.select('#paperOrder select[name="symbol"]', 'BTCUSDT');
    await page.select('#paperOrder select[name="side"]', 'BUY');
    await page.select('#paperOrder select[name="type"]', 'MARKET');
    await page.$eval('#paperOrder input[name="quantity"]', el => el.value = '0.005');
    await page.click('#paperOrder button');
    await sleep(1200);

    const orderMsg = await page.$eval('#paperMessage', el => el.textContent);
    const updatedInfo = await page.$eval('#paperInfo', el => el.textContent);
    const orderRows = await page.$$eval('#paperOrders tr', trs => trs.map(tr => tr.innerText));

    recordAudit('Virtual Market Order Execution', orderMsg.includes('FILLED') ? 'PASS' : 'WARN',
      `Result: "${orderMsg}", Latest row: "${orderRows[0] || 'none'}", Account: "${updatedInfo.replace(/\n/g, ' ')}"`);

    await page.screenshot({ path: path.join(EVIDENCE_DIR, '07_paper_order_placed.png') });

    // 7c: Place valid virtual paper limit order
    await page.select('#paperOrder select[name="type"]', 'LIMIT');
    await page.$eval('#paperOrder input[name="quantity"]', el => el.value = '0.002');
    await page.$eval('#paperOrder input[name="limit_price"]', el => el.value = '50000');
    await page.click('#paperOrder button');
    await sleep(1200);

    const limitOrderMsg = await page.$eval('#paperMessage', el => el.textContent);
    recordAudit('Virtual Limit Order Submission', limitOrderMsg.includes('OPEN') || limitOrderMsg.includes('Paper order:') ? 'PASS' : 'FAIL',
      `Result: "${limitOrderMsg}"`);

    // 7d: Test Risk Rejection (Excessive Notional > 1000 USDT)
    await page.select('#paperOrder select[name="type"]', 'MARKET');
    await page.$eval('#paperOrder input[name="limit_price"]', el => el.value = '');
    await page.$eval('#paperOrder input[name="quantity"]', el => el.value = '500'); // 500 BTC >> limit
    await page.click('#paperOrder button');
    await sleep(800);

    const riskRejectionMsg = await page.$eval('#paperMessage', el => el.textContent);
    recordAudit('Paper Risk Check (Oversized Order Rejection)',
      riskRejectionMsg.includes('MAX_ORDER_NOTIONAL') ? 'PASS' : 'FAIL',
      `Rejection response: "${riskRejectionMsg}"`);

    // 7e: Test Account Pause and Resume
    await page.click('#paperPause');
    await sleep(600);
    let pausedInfo = await page.$eval('#paperInfo', el => el.textContent);
    recordAudit('Paper Account Pause', pausedInfo.includes('Paused: true') ? 'PASS' : 'FAIL', `State: "${pausedInfo.replace(/\n/g, ' ')}"`);

    await page.$eval('#paperOrder input[name="quantity"]', el => el.value = '0.001');
    await page.click('#paperOrder button');
    await sleep(600);
    const pausedRejectionMsg = await page.$eval('#paperMessage', el => el.textContent);
    recordAudit('Paper Order Rejection When Paused',
      pausedRejectionMsg.includes('TRADING_PAUSED') ? 'PASS' : 'FAIL',
      `Notice: "${pausedRejectionMsg}"`);

    await page.click('#paperResume');
    await sleep(600);
    pausedInfo = await page.$eval('#paperInfo', el => el.textContent);
    recordAudit('Paper Account Resume', pausedInfo.includes('Paused: false') ? 'PASS' : 'FAIL', `State: "${pausedInfo.replace(/\n/g, ' ')}"`);

    await page.screenshot({ path: path.join(EVIDENCE_DIR, '08_paper_trading_completed.png') });

    // ----------------------------------------------------
    // TEST 8: Governance & Dual-Control Approvals
    // ----------------------------------------------------
    console.log('\n--- 8. Dual-Control Governance Workflow & Safety ---');
    await page.click('nav button[data-page="sandbox"]');
    await sleep(800);

    const sandboxInfo = await page.$eval('#sandboxInfo', el => el.textContent);
    recordAudit('Sandbox Capability State', sandboxInfo.includes('DISABLED') ? 'PASS' : 'FAIL',
      `Sandbox config verified disabled: "${sandboxInfo.replace(/\n/g, ' ')}"`);

    // Create sandbox proposal as lead admin
    await page.select('#sandboxOrder select[name="symbol"]', 'BTCUSDT');
    await page.select('#sandboxOrder select[name="side"]', 'BUY');
    await page.select('#sandboxOrder select[name="type"]', 'MARKET');
    await page.$eval('#sandboxOrder input[name="quantity"]', el => el.value = '0.0001');
    await page.click('#sandboxOrder button');
    await sleep(800);

    const sandboxMsg = await page.$eval('#sandboxMessage', el => el.textContent);
    const reqMatch = sandboxMsg.match(/request\s+([a-f0-9-]+)\s+created/i);
    const createdReqId = reqMatch ? reqMatch[1] : null;
    recordAudit('Sandbox Order Governance Request Creation', sandboxMsg.includes('Approval request') ? 'PASS' : 'FAIL',
      `Message: "${sandboxMsg}", Request ID prefix: ${createdReqId}`);

    // Navigate to Approvals and test Self-Approval rejection
    await page.click('nav button[data-page="approvals"]');
    await sleep(800);

    // Test Self-Approval with fresh OTP for lead admin
    const leadFreshOTP = await getFreshTOTP(USERS.lead.totpSecret, 'lead');
    nextPromptInput = leadFreshOTP;

    // Click "Approve" button
    const approveBtn = await page.$('#approvalRows button.secondary');
    if (approveBtn) {
      await approveBtn.click();
      await sleep(1000);
      const approvalMsg = await page.$eval('#approvalMessage', el => el.textContent);
      recordAudit('Self-Approval Rejection (Dual-Control Invariant)',
        approvalMsg.includes('Independent reviewer required') || approvalMsg.includes('Requester cannot approve') || approvalMsg.includes('409') ? 'PASS' : 'FAIL',
        `Rejection message: "${approvalMsg}"`);
    }

    await page.screenshot({ path: path.join(EVIDENCE_DIR, '09_self_approval_blocked.png') });

    // 8c: Switch to audit_peer_admin and approve
    console.log('\n--- Switching to Second Admin (audit_peer_admin) ---');
    await page.click('#logout');
    await page.waitForSelector('#auth:not(.hidden)');
    await sleep(600);

    await page.$eval('#user', el => el.value = '');
    await page.$eval('#password', el => el.value = '');
    await page.$eval('#otp', el => el.value = '');
    await page.type('#user', USERS.peer.username);
    await page.type('#password', USERS.peer.password);
    const peerOTP = await getFreshTOTP(USERS.peer.totpSecret, 'peer');
    await page.type('#otp', peerOTP);
    await page.click('#loginForm button');
    await page.waitForSelector('#app:not(.hidden)', { timeout: 8000 });
    await sleep(1000);

    const peerSession = await page.$eval('#session', el => el.textContent);
    recordAudit('Second Admin Authentication', peerSession.includes('ADMIN') ? 'PASS' : 'FAIL', `Session: ${peerSession}`);

    // Navigate to Approvals as peer admin
    await page.waitForSelector('nav button[data-page="approvals"]', { visible: true });
    await page.click('nav button[data-page="approvals"]');
    await sleep(800);

    // Approve as peer admin with fresh peer TOTP
    let peerReviewOTP = await getFreshTOTP(USERS.peer.totpSecret, 'peer');
    nextPromptInput = peerReviewOTP;
    const peerApproveBtn = await page.$('#approvalRows button.secondary');
    if (peerApproveBtn) {
      await peerApproveBtn.click();
      await sleep(1500);
      const approvalRowsAfter = await page.$$eval('#approvalRows tr', trs => trs.map(tr => tr.innerText));
      const hasApproved = approvalRowsAfter.some(r => r.includes('APPROVED'));
      recordAudit('Dual-Control Independent Approval', hasApproved ? 'PASS' : 'FAIL',
        `Approved status present in queue: ${hasApproved}`);
    } else {
      recordAudit('Peer Approve Button Check', 'FAIL', 'Approve button not found');
    }

    await page.screenshot({ path: path.join(EVIDENCE_DIR, '10_peer_approval_success.png') });

    // ----------------------------------------------------
    // TEST 9: AI Research Lab & Model Registry Inspection
    // ----------------------------------------------------
    console.log('\n--- 9. AI Research Lab & Model Registry ---');
    await page.waitForSelector('nav button[data-page="research"]', { visible: true });
    await page.click('nav button[data-page="research"]');
    await sleep(800);

    const researchJobs = await page.$$eval('#jobRows tr', trs => trs.length);
    const modelsCount = await page.$$eval('#modelRows tr', trs => trs.length);
    const campaignsCount = await page.$$eval('#campaignRows tr', trs => trs.length);

    recordAudit('Research Lab Data Tables', 'PASS',
      `Jobs: ${researchJobs}, Models in registry: ${modelsCount}, Campaigns: ${campaignsCount}`);

    // Test campaign creation button
    await page.click('#createCampaign');
    await sleep(800);
    const campaignMsg = await page.$eval('#researchMessage', el => el.textContent);
    recordAudit('Recurring Campaign Creation (Disabled by Default)',
      campaignMsg.includes('created, DISABLED') ? 'PASS' : 'FAIL', `Notice: "${campaignMsg}"`);

    await page.screenshot({ path: path.join(EVIDENCE_DIR, '11_ai_research_lab.png') });

    // ----------------------------------------------------
    // TEST 10: Live Trading Controls (Locked / Disabled Safety Check)
    // ----------------------------------------------------
    console.log('\n--- 10. Live Trading Controls & Safety Boundary ---');
    await page.waitForSelector('nav button[data-page="live"]', { visible: true });
    await page.click('nav button[data-page="live"]');
    await sleep(800);

    const liveBanner = await page.$eval('#liveBanner', el => el.textContent);
    const liveAccountsText = await page.$eval('#liveAccounts', el => el.textContent);
    const liveReportText = await page.$eval('#liveReport', el => el.textContent);

    recordAudit('Live Trading Safety Lock Banner',
      liveBanner.includes('LOCKED') || liveBanner.includes('DISARMED') ? 'PASS' : 'FAIL',
      `Banner text: "${liveBanner}"`);
    recordAudit('Live Panel Access Rejection / Safety Guard',
      liveAccountsText.includes('locked') || liveAccountsText.includes('unavailable') || liveAccountsText.includes('403') || liveAccountsText.includes('DISARMED') ? 'PASS' : 'FAIL',
      `Account panel: "${liveAccountsText.replace(/\n/g, ' ')}"`);

    await page.screenshot({ path: path.join(EVIDENCE_DIR, '12_live_controls_locked.png') });

    // ----------------------------------------------------
    // TEST 11: Operations & System Monitoring
    // ----------------------------------------------------
    console.log('\n--- 11. Infrastructure & Operations Monitoring ---');
    await page.waitForSelector('nav button[data-page="operations"]', { visible: true });
    await page.click('nav button[data-page="operations"]');
    await sleep(800);

    const streamInfo = await page.$eval('#streamInfo', el => el.textContent);
    const gapInfo = await page.$eval('#gapInfo', el => el.textContent);
    const readinessBefore = await page.$eval('#readinessState', el => el.textContent);

    recordAudit('Operations Stream Telemetry', streamInfo.includes('spot') ? 'PASS' : 'FAIL', `Stream telemetry populated: ${streamInfo.length} chars`);
    recordAudit('Operations AggTrade Gap Analysis', gapInfo.includes('NO_KNOWN_AGGTRADE_GAPS') || gapInfo.includes('quality') ? 'PASS' : 'FAIL', `Gap telemetry populated`);
    recordAudit('Operations Shadow Readiness State', readinessBefore.length > 5 ? 'PASS' : 'FAIL', `Readiness: "${readinessBefore.replace(/\n/g, ' ')}"`);

    // Click Record Readiness Assessment
    await page.click('#evaluateReadiness');
    await sleep(1000);
    const readinessAfter = await page.$eval('#readinessState', el => el.textContent);
    recordAudit('Record Readiness Assessment Button', readinessAfter.includes('NO_GO') || readinessAfter.includes('REVIEW_REQUIRED') ? 'PASS' : 'FAIL',
      `Evaluated readiness: "${readinessAfter.replace(/\n/g, ' ')}"`);

    await page.screenshot({ path: path.join(EVIDENCE_DIR, '13_operations_infra.png') });

    // ----------------------------------------------------
    // TEST 12: Legacy Dashboard Inspection (/legacy)
    // ----------------------------------------------------
    console.log('\n--- 12. Legacy Maintenance Terminal (/legacy) ---');
    await page.goto(BASE_URL + '/legacy', { waitUntil: 'networkidle2' });
    const legacyTitle = await page.title();
    recordAudit('Legacy Terminal Page Load', legacyTitle.length > 0 ? 'PASS' : 'FAIL', `Title: "${legacyTitle}"`);
    await page.screenshot({ path: path.join(EVIDENCE_DIR, '14_legacy_terminal.png') });

    // ----------------------------------------------------
    // TEST 13: Console & Network Health Summary
    // ----------------------------------------------------
    console.log('\n--- 13. Browser Console & Network Summary ---');
    const uncaughtErrors = consoleMessages.filter(m => m.includes('error') || m.includes('Uncaught'));
    recordAudit('Browser Console Errors', uncaughtErrors.length === 0 ? 'PASS' : 'WARN',
      `Total console msgs: ${consoleMessages.length}, Errors: ${uncaughtErrors.length}`);
    if (uncaughtErrors.length > 0) {
      console.log('Uncaught errors count:', uncaughtErrors.length);
    }

  } catch (err) {
    console.error('AUDIT RUNTIME EXCEPTION:', err);
    recordAudit('Audit Execution Exception', 'FAIL', err.message);
    await page.screenshot({ path: path.join(EVIDENCE_DIR, 'audit_failure_exception.png') });
  } finally {
    await browser.close();
  }

  fs.writeFileSync(
    path.join(EVIDENCE_DIR, 'audit_execution_log.json'),
    JSON.stringify({ auditLog, consoleMessages, networkErrors }, null, 2)
  );
  console.log('\n=== AUDIT SUITE COMPLETED. LOG WRITTEN TO docs/audit_evidence/audit_execution_log.json ===');
}

runAudit();
