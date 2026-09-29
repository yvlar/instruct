// Synthetic installation created AND restored by backend/tests/browser_demo.py.
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
const browser = await chromium.launch();
const page = await browser.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
try {
  // Vite and the backend may still be starting in CI.
  await page.goto('http://localhost:3000');
  await page.getByRole('button', {name: 'Se connecter'}).waitFor();
  await page.getByLabel('Identifiant', {exact:true}).fill('alice');
  await page.getByLabel('Mot de passe', {exact:true}).fill('Synthetic-password-123');
  await page.getByRole('button', {name:'Se connecter'}).click();
  await page.getByRole('button', {name:'Déconnexion'}).waitFor();
  await page.getByLabel('Votre question').fill('Quelle est la pression de la pompe?');
  await page.getByRole('button', {name:'Rechercher', exact:true}).click();
  const source = page.getByRole('link', {name:'maintenance/pompe.pdf — page 1'});
  await source.waitFor();
  assert(!(await page.locator('body').innerText()).includes('BETA'));
  const url = await source.getAttribute('href');
  const pdf = await page.context().request.get(`http://localhost:3000${url}`);
  assert.equal(pdf.status(),200);
  assert((await pdf.body()).subarray(0,4).equals(Buffer.from('%PDF')));
  // Actually follow the cited link in a browser tab (PDF viewer varies by Chromium).
  const [opened] = await Promise.all([
    page.context().waitForEvent('response', {predicate:r=>r.url().includes(url.split('#')[0])}),
    source.click(),
  ]);
  assert.equal(opened.status(),200);
  assert(opened.headers()['content-type'].includes('application/pdf'));
  for (const tab of page.context().pages()) if (tab !== page) await tab.close();
  await page.getByRole('button', {name:'Documents', exact:true}).click();
  await page.getByRole('heading', {name:'maintenance/pompe.pdf'}).waitFor();
  assert(!(await page.locator('body').innerText()).includes('production/vanne.pdf'));
  // UUID is supplied by the admin session in a separate browser context.
  const admin = await browser.newContext();
  const adminPage = await admin.newPage();
  await adminPage.goto('http://localhost:3000');
  await adminPage.getByLabel('Identifiant', {exact:true}).fill('admin');
  await adminPage.getByLabel('Mot de passe', {exact:true}).fill('Synthetic-password-123');
  await adminPage.getByRole('button', {name:'Se connecter'}).click();
  await adminPage.getByRole('button', {name:'Administration', exact:true}).waitFor();
  const documents = await (await admin.request.get('http://localhost:3000/api/documents')).json();
  const forbidden = documents.find(d=>d.path==='production/vanne.pdf');
  const deniedTab = await page.context().newPage();
  const denied = await deniedTab.goto(`http://localhost:3000/api/documents/${forbidden.id}/file`);
  assert.equal(denied.status(),404);
  assert(!(await deniedTab.locator('body').innerText()).includes('BETA'));
  await deniedTab.close();
  await adminPage.getByRole('button', {name:'Administration', exact:true}).click();
  await adminPage.getByRole('heading', {name:'Journal d’audit'}).waitFor();
  await admin.close();
  await page.getByRole('button', {name:'Déconnexion'}).click();
  await page.getByRole('button', {name:'Se connecter'}).waitFor();
  assert.equal((await page.context().request.get(`http://localhost:3000${url}`)).status(),401);
  assert.deepEqual(errors,[]);
  console.log('PASS: restored installation → login → authorized search → cited PDF → forbidden direct URL → logout; admin screen; no page errors.');
} finally {
  await browser.close();
}
