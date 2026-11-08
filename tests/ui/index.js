'use strict';
// Entry point so that `node --test tests/ui/` works on Node >= 22 (where a bare directory argument is resolved as a
// module instead of being searched). It loads every *.test.mjs sibling; `node --test tests/ui/*.test.mjs` also works.
const { readdirSync } = require('node:fs');
const { join } = require('node:path');
const { pathToFileURL } = require('node:url');

const files = readdirSync(__dirname)
  .filter((f) => f.endsWith('.test.mjs'))
  .sort();
(async () => {
  for (const f of files) await import(pathToFileURL(join(__dirname, f)).href);
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
