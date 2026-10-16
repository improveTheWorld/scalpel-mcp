import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { apply } from '../lib/index.js';

function createHarness({ sandboxMode = 'workspace-write', callToolImpl } = {}) {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-u2-'));
  const tools = new Map();
  const fakeCtx = {
    tools: { register: (t) => tools.set(t.name, t) },
    inject: () => {}
  };

    const defaultCallTool = async (_ws, name, args) => {
      if (name === 'read_block') {
        const p = path.resolve(ws, args.path);
        if (!fs.existsSync(p)) {
          return { status: 'error', error: `File not found: ${args.path}` };
        }
        const buf = fs.readFileSync(p);
        const hash = crypto.createHash('sha256').update(buf).digest('hex').slice(0, 8);
        const lines = buf.toString('utf8').split(/\r?\n/);
        const width = String(lines.length).length;
        const numLines = lines.map((l, i) => `${String(i + 1).padStart(width, ' ')} | ${l}`).join('\n');
        return `${args.path} [file_text] [h:${hash}] (${lines.length} lines)\n${numLines}`;
      }
      if (name === 'edit_block') {
        return { status: 'error', error: `'${args.path}' is a whole file, not a block inside one.` };
      }
      if (name === 'delete_block') {
        return { status: 'error', error: `'${args.path}' is a whole FILE, not a block inside one, and nothing was touched.` };
      }
      return { status: 'success' };
    };

  apply(fakeCtx, {}, {
    callTool: callToolImpl || defaultCallTool,
    getSandboxPolicy: () => ({
      resolve: () => ({ mode: sandboxMode, workspaceRoot: ws })
    })
  });

  const exec = {
    agent: { session: { header: { cwd: ws }, id: 'session-u2' } }
  };

  return { ws, tools, exec, cleanup: () => fs.rmSync(ws, { recursive: true, force: true }) };
}

test('1. Lecture d\'un .ps1 via read_block -> texte numéroté et hash', async () => {
  const h = createHarness();
  try {
    const ps1Path = path.join(h.ws, 'script.ps1');
    const content = 'Write-Host "hello"\nWrite-Host "world"\n';
    fs.writeFileSync(ps1Path, content, 'utf8');

    const expectedHash = crypto.createHash('sha256').update(Buffer.from(content, 'utf8')).digest('hex').slice(0, 8);

    const readTool = h.tools.get('read_block');
    assert.ok(readTool);

    const res = await readTool.execute({ path: 'script.ps1' }, h.exec);
    assert.match(res, /script\.ps1 \[file_text\]/);
    assert.match(res, new RegExp(`\\[h:${expectedHash}\\]`));
    assert.match(res, /1 \| Write-Host "hello"/);
    assert.match(res, /2 \| Write-Host "world"/);
  } finally {
    h.cleanup();
  }
});

test('2. Édition de lignes d\'un .ps1 via edit_block avec expected_hash -> modification appliquée sur disque et réponse compacte', async () => {
  const h = createHarness();
  try {
    const ps1Path = path.join(h.ws, 'deploy.ps1');
    const content = 'line 1\nline 2\nline 3\n';
    fs.writeFileSync(ps1Path, content, 'utf8');

    const fullHash = crypto.createHash('sha256').update(Buffer.from(content, 'utf8')).digest('hex').slice(0, 8);

    const editTool = h.tools.get('edit_block');
    assert.ok(editTool);

    const res = await editTool.execute({
      path: 'deploy.ps1',
      expected_hash: fullHash,
      ops: [{ replace: [2, 2], content: 'modified line 2' }]
    }, h.exec);

    assert.match(res, /^\[ok\]/);
    assert.match(res, /modified line 2/);

    const updatedOnDisk = fs.readFileSync(ps1Path, 'utf8');
    assert.equal(updatedOnDisk, 'line 1\nmodified line 2\nline 3\n');
  } finally {
    h.cleanup();
  }
});

test('3. Conflit de version sur edit_block si mauvais expected_hash', async () => {
  const h = createHarness();
  try {
    const ps1Path = path.join(h.ws, 'config.ps1');
    const content = '$env = "dev"\n';
    fs.writeFileSync(ps1Path, content, 'utf8');

    const editTool = h.tools.get('edit_block');
    assert.ok(editTool);

    await assert.rejects(
      async () => {
        await editTool.execute({
          path: 'config.ps1',
          expected_hash: 'bad0000000000000000000000000000000000000',
          ops: [{ replace: [1, 1], content: '$env = "prod"' }]
        }, h.exec);
      },
      /Stale read: the block changed since you read it/
    );

    const unchanged = fs.readFileSync(ps1Path, 'utf8');
    assert.equal(unchanged, content);
  } finally {
    h.cleanup();
  }
});

test('4. Suppression d\'un .ps1 via delete_block avec expected_heading -> fichier supprimé', async () => {
  const h = createHarness();
  try {
    const ps1Path = path.join(h.ws, 'temp.ps1');
    fs.writeFileSync(ps1Path, 'Write-Host "remove me"', 'utf8');

    const deleteTool = h.tools.get('delete_block');
    assert.ok(deleteTool);

    // Mauvais expected_heading -> refus
    await assert.rejects(
      async () => {
        await deleteTool.execute({ path: 'temp.ps1', expected_heading: 'wrong.ps1' }, h.exec);
      },
      /expected_heading guard failed/
    );
    assert.ok(fs.existsSync(ps1Path));

    // Bon expected_heading -> succès
    const res = await deleteTool.execute({ path: 'temp.ps1', expected_heading: 'temp.ps1' }, h.exec);
    assert.match(res, /^\[ok\]/);
    assert.match(res, /\(deleted\)/);
    assert.equal(fs.existsSync(ps1Path), false);
  } finally {
    h.cleanup();
  }
});

test('5. Refus de mutation sous read-only (sécurité Lot S préservée)', async () => {
  const h = createHarness({ sandboxMode: 'read-only' });
  try {
    const ps1Path = path.join(h.ws, 'secure.ps1');
    fs.writeFileSync(ps1Path, 'Get-Date', 'utf8');

    const editTool = h.tools.get('edit_block');
    await assert.rejects(
      async () => {
        await editTool.execute({
          path: 'secure.ps1',
          ops: [{ replace: [1, 1], content: 'Set-Date' }]
        }, h.exec);
      },
      /sandbox: file access denied under read-only mode/
    );

    const deleteTool = h.tools.get('delete_block');
    await assert.rejects(
      async () => {
        await deleteTool.execute({ path: 'secure.ps1' }, h.exec);
      },
      /sandbox: file access denied under read-only mode/
    );
  } finally {
    h.cleanup();
  }
});

test('6. Vérification que read_unindexed et ses pairs n\'existent plus dans ctx.tools.register', () => {
  const h = createHarness();
  try {
    assert.equal(h.tools.size, 9, 'Exactement 9 outils Scalpel (8 AST + scalpel_report) doivent être enregistrés');
    assert.equal(h.tools.has('read_unindexed'), false, 'read_unindexed ne doit plus exister');
    assert.equal(h.tools.has('edit_unindexed'), false, 'edit_unindexed ne doit plus exister');
    assert.equal(h.tools.has('write_unindexed'), false, 'write_unindexed ne doit plus exister');
    assert.equal(h.tools.has('delete_unindexed'), false, 'delete_unindexed ne doit plus exister');

    const expected = [
      'read_block', 'edit_block', 'insert_block', 'delete_block',
      'search_symbols', 'search_documents', 'get_project_map', 'build_context'
    ];
    for (const name of expected) {
      assert.ok(h.tools.has(name), `L'outil ${name} doit être présent`);
    }
  } finally {
    h.cleanup();
  }
});

test('7. Intégration réelle Python (sans mock) : read_block et edit_block (unitaire + lot edits=[...]) sur .gitignore et pack.ps1', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-u2-real-'));
  const tools = new Map();
  const fakeCtx = {
    tools: { register: (t) => tools.set(t.name, t) },
    inject: () => {}
  };
  apply(fakeCtx, {}, {
    getSandboxPolicy: () => ({
      resolve: () => ({ mode: 'workspace-write', workspaceRoot: ws })
    })
  });
  const exec = {
    agent: { session: { header: { cwd: ws }, id: 'session-u2-real' } }
  };

  try {
    fs.writeFileSync(path.join(ws, '.gitignore'), 'obj/\nbin/\nscript*/\n', 'utf8');
    fs.writeFileSync(path.join(ws, 'pack.ps1'), 'param([string]$Config = "Release")\nWrite-Host $Config\n', 'utf8');

    const readTool = tools.get('read_block');
    const editTool = tools.get('edit_block');

    const rGit = await readTool.execute({ path: '.gitignore' }, exec);
    const rPack = await readTool.execute({ path: 'pack.ps1' }, exec);
    const hGit = (String(rGit).match(/\[h:([a-f0-9]{8})\]/) || [])[1];
    const hPack = (String(rPack).match(/\[h:([a-f0-9]{8})\]/) || [])[1];
    assert.ok(hGit, 'hash .gitignore présent');
    assert.ok(hPack, 'hash pack.ps1 présent');

    // Édition en lot edits=[...] sur .gitignore et pack.ps1 (cas exact de la session DataLinq)
    const batchRes = await editTool.execute({
      edits: [
        {
          path: '.gitignore',
          expected_hash: hGit,
          ops: [{ replace: [3, 3], content: 'scripts/archive/\n*.pid' }]
        },
        {
          path: 'pack.ps1',
          expected_hash: hPack,
          ops: [{ replace: [2, 2], content: 'Write-Host "Packing $Config"' }]
        }
      ]
    }, exec);

    assert.equal(typeof batchRes, 'string');
    assert.match(batchRes, /^\[ok\] applied 2\/2 edits:/);
    assert.equal(fs.readFileSync(path.join(ws, '.gitignore'), 'utf8'), 'obj/\nbin/\nscripts/archive/\n*.pid\n');
    assert.equal(fs.readFileSync(path.join(ws, 'pack.ps1'), 'utf8'), 'param([string]$Config = "Release")\nWrite-Host "Packing $Config"\n');
  } finally {
    const { closeDaemon } = await import('../lib/bridge.js');
    closeDaemon();
    try { fs.rmSync(ws, { recursive: true, force: true }); } catch {}
  }
});
test('8. Garde anti-corruption S1 : pas de repli vers file_block.js sur path contenant :: ou # ou sur whole code file / no symbol has this identifier', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-u2-guard-'));
  const outsideDir = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-u2-outside-'));
  try {
    const codeFile = path.join(ws, 'Foo.cs');
    fs.writeFileSync(codeFile, 'public class Foo { public void Bar() {} }\n', 'utf8');
    const outA = path.join(outsideDir, 'outA.txt');
    const outB = path.join(outsideDir, 'outB.txt');
    fs.writeFileSync(outA, 'hello A\n', 'utf8');
    fs.writeFileSync(outB, '\uFEFFhello B\n', 'utf8');

    let fileBlockCalled = false;
    const tools = new Map();
    const fakeCtx = {
      tools: { register: (t) => tools.set(t.name, t) },
      inject: () => {}
    };

    apply(fakeCtx, {}, {
      callTool: async (_w, name, args) => {
        if (name === 'delete_block' && args.path.includes('::')) {
          return { status: 'error', error: `'${args.path}': no symbol or markdown section has this identifier.` };
        }
        if (name === 'edit_block' && args.path && args.path.includes('::')) {
          return { status: 'error', error: `'${args.path}': no symbol or markdown section has this identifier.`, candidates: ['Foo.cs::Foo::Bar'] };
        }
        if (name === 'edit_block' && args.path === 'Foo.cs') {
          return { status: 'error', error: `'Foo.cs' is a whole code file, not a block inside one.` };
        }
        if (name === 'read_block') {
          return { status: 'error', error: `path resolves outside the workspace root` };
        }
        return { status: 'success' };
      },
      fileBlock: {
        editFileBlock: async () => { fileBlockCalled = true; return '[ok]'; },
        deleteFileBlock: async () => { fileBlockCalled = true; return '[ok]'; }
      },
      getSandboxPolicy: () => ({
        resolve: () => ({ mode: 'danger-full-access', workspaceRoot: ws })
      })
    });

    const exec = { agent: { session: { header: { cwd: ws }, id: 'session-guard' } } };

    // (a) delete_block sur symbole mal orthographié ('Foo.cs::Typo') ne doit JAMAIS appeler deleteFileBlock ni supprimer Foo.cs
    const delRes = await tools.get('delete_block').execute({ path: 'Foo.cs::Typo', expected_heading: 'Foo.cs' }, exec);
    assert.equal(fileBlockCalled, false, 'deleteFileBlock ne doit jamais être appelé quand path contient ::');
    assert.ok(fs.existsSync(codeFile), 'Foo.cs doit toujours exister sur disque');
    assert.equal(delRes.status, 'error');

    // (b) edit_block sur symbole mal orthographié ('Foo.cs::Typo') préserve les candidates AST sans appeler editFileBlock
    const editTypoRes = await tools.get('edit_block').execute({
      path: 'Foo.cs::Typo',
      expected_hash: '12345678',
      ops: [{ replace: [1, 1], content: 'x' }]
    }, exec);
    assert.equal(fileBlockCalled, false, 'editFileBlock ne doit jamais être appelé quand path contient ::');
    assert.deepEqual(editTypoRes.candidates, ['Foo.cs::Foo::Bar']);

    // (c) edit_block sur 'whole code file' ne doit pas déclencher editFileBlock
    const editWholeCodeRes = await tools.get('edit_block').execute({
      path: 'Foo.cs',
      expected_hash: '12345678',
      ops: [{ replace: [1, 1], content: 'x' }]
    }, exec);
    assert.equal(fileBlockCalled, false, 'editFileBlock ne doit pas être appelé sur whole code file');
    assert.equal(editWholeCodeRes.status, 'error');

    // (d) read_block(paths=[...]) hors racine retourne les blocs numérotés et hache sans BOM
    const readPathsRes = await tools.get('read_block').execute({
      paths: [outA, outB]
    }, exec);
    const expectedHashB = crypto.createHash('sha256').update(Buffer.from('hello B\n', 'utf8')).digest('hex').slice(0, 8);
    assert.match(readPathsRes, /1 \| hello A/);
    assert.match(readPathsRes, /1 \| hello B/);
    assert.match(readPathsRes, new RegExp(`\\[h:${expectedHashB}\\]`));
  } finally {
    try { fs.rmSync(ws, { recursive: true, force: true }); } catch {}
    try { fs.rmSync(outsideDir, { recursive: true, force: true }); } catch {}
  }
});