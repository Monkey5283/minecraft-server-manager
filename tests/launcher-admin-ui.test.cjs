const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createHash, webcrypto } = require('node:crypto');
const vm = require('node:vm');
const path = require('node:path');

const assets = path.join(__dirname, '..', 'mc_manager', 'static');
const source = readFileSync(path.join(assets, 'launcher.js'), 'utf8');
const markup = readFileSync(path.join(assets, 'index.html'), 'utf8');

function screen() {
  const elements = new Map();
  for (const [, id] of markup.matchAll(/id="([^"]+)"/g)) {
    elements.set(id, { value: '', textContent: '', disabled: false, hidden: false,
      files: [], handlers: {}, addEventListener(name, handler) { this.handlers[name] = handler; },
      querySelectorAll() { return [...elements.values()]; }, close() {} });
  }
  const calls = [];
  vm.runInNewContext(source, {
    document: { getElementById: id => elements.get(id) },
    window: { addEventListener() {} }, URL, crypto: webcrypto, Uint8Array,
    api: async route => { calls.push(route); return { authenticated: true, admin: true }; },
  });
  const field = name => elements.get(`launcher-${name}`);
  field('file-base').value = 'https://packs.example.test/releases/1';
  field('files').value = 'previous draft';
  return { field, calls, import: () => field('folder-import').handlers.change() };
}

function file(name, content = 'pack bytes') {
  const bytes = Buffer.from(content);
  return { webkitRelativePath: `Instance/${name}`, size: bytes.length,
    arrayBuffer: async () => Uint8Array.from(bytes).buffer };
}

test('folder import hashes managed client files without uploading or including worlds/accounts', async () => {
  const ui = screen();
  ui.field('folder-import').files = [file('mods/a b.jar'), file('config/example.json', '{}'),
    file('saves/world.dat'), file('accounts.json'), file('logs/latest.log')];
  await ui.import();
  const manifest = JSON.parse(ui.field('files').value);
  assert.deepEqual(manifest.map(f => f.path), ['mods/a b.jar', 'config/example.json']);
  assert.equal(manifest[0].url, 'https://packs.example.test/releases/1/mods/a%20b.jar');
  assert.equal(manifest[0].sha256, createHash('sha256').update('pack bytes').digest('hex'));
  assert.equal(manifest[0].size, 10);
  assert.match(ui.field('file-count').textContent, /3 other files excluded/);
  assert.deepEqual(ui.calls, ['/api/session']);
  assert.equal(ui.field('files').disabled, false);
});

test('invalid hosting URLs preserve the draft and report an error', async () => {
  for (const base of ['http://packs.example.test/', 'https://user:secret@packs.example.test/',
    'https://packs.example.test/?token=secret', 'https://packs.example.test/#fragment']) {
    const ui = screen();
    ui.field('file-base').value = base;
    ui.field('folder-import').files = [file('mods/a.jar')];
    await ui.import();
    assert.equal(ui.field('files').value, 'previous draft');
    assert.match(ui.field('notice').textContent, /HTTPS base URL/);
  }
});

test('oversized files are rejected before allocating their contents', async () => {
  const ui = screen();
  ui.field('folder-import').files = [{ webkitRelativePath: 'Instance/mods/large.jar',
    size: 256 * 1048576 + 1, arrayBuffer() { assert.fail('must not read oversized file'); } }];
  await ui.import();
  assert.equal(ui.field('files').value, 'previous draft');
  assert.match(ui.field('notice').textContent, /256 MiB/);
});

test('a folder containing only personal data cannot replace the release draft', async () => {
  const ui = screen();
  ui.field('folder-import').files = [file('saves/world.dat'), file('screenshots/private.png')];
  await ui.import();
  assert.equal(ui.field('files').value, 'previous draft');
  assert.match(ui.field('notice').textContent, /No managed client files/);
});
