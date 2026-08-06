/**
 * Run a Postman collection LIVE via Newman (Postman's official CLI collection
 * runner — the same execution engine as the Postman desktop app's Collection
 * Runner) and capture full, unredacted request/response pairs.
 *
 * Usage:
 *   node scripts/run_via_newman.js <collection.json> <results-out.json>
 */
'use strict';

const fs = require('fs');
const path = require('path');
const newman = require('newman');

const collectionPath = process.argv[2];
const outPath = process.argv[3];

if (!collectionPath || !outPath) {
  console.error('Usage: node run_via_newman.js <collection.json> <results-out.json>');
  process.exit(2);
}

const results = [];
let step = 0;

const run = newman.run(
  {
    collection: JSON.parse(fs.readFileSync(collectionPath, 'utf8')),
    reporters: ['cli'],
    insecure: false,
    timeoutRequest: 90000,
  },
  function (err, summary) {
    if (err) {
      console.error('Newman run error:', err);
      process.exit(1);
    }
    const failedCount = summary.run.failures.length;
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    fs.writeFileSync(outPath, JSON.stringify(results, null, 2), 'utf8');
    console.log(`\nWrote ${outPath} (${results.length} requests captured)`);
    const passed = results.filter((r) => r.passFail === 'PASS').length;
    console.log(`Summary: ${passed}/${results.length} passed, ${results.length - passed} failed`);
    process.exit(summary.error ? 1 : 0);
  }
);

run.on('beforeItem', function (err, args) {});

run.on('request', function (err, args) {
  if (err) {
    console.error('Request error:', err);
    return;
  }
  step += 1;
  const parent = args.item.parent && args.item.parent();
  const folderName = (parent && parent.name) ? parent.name : (args.cursor && args.cursor.ref) || '';
  const name = args.item.name;
  const req = args.request;
  const res = args.response;

  const method = req.method;
  const fullUrl = req.url.toString();
  let path_ = fullUrl;
  try {
    const u = new URL(fullUrl);
    path_ = u.pathname;
  } catch (e) {}

  let requestBody = '';
  if (req.body && req.body.mode === 'raw' && req.body.raw) {
    requestBody = req.body.raw;
  }

  const authHeader = req.headers.get('Authorization') || null;

  const statusCode = res ? res.code : 0;
  const responseBody = res ? res.stream.toString('utf8') : '';
  const passFail = statusCode >= 200 && statusCode < 300 ? 'PASS' : 'FAIL';

  results.push({
    step,
    folder: folderName,
    name,
    method,
    path: path_,
    fullUrl,
    requestBody: requestBody || (method === 'GET' ? '(no request body — GET)' : ''),
    responseBody: responseBody || (statusCode === 204 ? '(empty — 204 No Content)' : responseBody),
    statusCode,
    passFail,
    auth: authHeader ? authHeader : 'None (public)',
    notes: '',
  });

  console.log(`[${step}] ${folderName} :: ${name} -> ${statusCode} ${passFail}`);
});
