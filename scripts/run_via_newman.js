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
let currentResult = null;
let currentAssertions = [];

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
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    fs.writeFileSync(outPath, JSON.stringify(results, null, 2), 'utf8');
    console.log(`\nWrote ${outPath} (${results.length} requests captured)`);
    const passed = results.filter((r) => r.passFail === 'PASS').length;
    console.log(`Summary: ${passed}/${results.length} passed, ${results.length - passed} failed`);
    process.exit(summary.error ? 1 : 0);
  }
);

run.on('beforeItem', function (err, args) {
  currentAssertions = [];
});

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

  currentResult = {
    step,
    folder: folderName,
    name,
    method,
    path: path_,
    fullUrl,
    requestBody: requestBody || (method === 'GET' ? '(no request body — GET)' : ''),
    responseBody: responseBody || (statusCode === 204 ? '(empty — 204 No Content)' : responseBody),
    statusCode,
    // Placeholder — finalized in 'item' below once this item's pm.test()
    // assertions (if any) have all run. Falls back to the 2xx heuristic
    // only for items with no assertions at all, so a deliberate negative
    // test (e.g. "expect 401" with a passing pm.test asserting exactly
    // that) is reported as PASS, not misread as a failure by status code.
    passFail: null,
    auth: authHeader ? authHeader : 'None (public)',
    notes: '',
  };
  results.push(currentResult);
});

run.on('assertion', function (err, args) {
  currentAssertions.push(!args.error);
});

run.on('item', function (err, args) {
  if (!currentResult) return;
  const passFail = currentAssertions.length > 0
    ? (currentAssertions.every(Boolean) ? 'PASS' : 'FAIL')
    : (currentResult.statusCode >= 200 && currentResult.statusCode < 300 ? 'PASS' : 'FAIL');
  currentResult.passFail = passFail;
  console.log(`[${currentResult.step}] ${currentResult.folder} :: ${currentResult.name} -> ${currentResult.statusCode} ${passFail}`);
  currentResult = null;
  currentAssertions = [];
});
