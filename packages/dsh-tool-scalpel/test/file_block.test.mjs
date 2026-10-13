import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import {
  applyLineOps,
  renderCompactDiff,
  editFileBlock,
  deleteFileBlock
} from '../lib/file_block.js';

test('applyLineOps - replace, delete, insert_after', () => {
  const initial = 'line 1\nline 2\nline 3\nline 4\nline 5\n';

  // 1. replace simple
  const r1 = applyLineOps(initial, [{ replace: [2, 3], content: 'replaced 2 and 3' }]);
  assert.equal(r1.error, null);
  assert.equal(r1.text, 'line 1\nreplaced 2 and 3\nline 4\nline 5\n');

  // 2. delete simple
  const r2 = applyLineOps(initial, [{ delete: [2, 4] }]);
  assert.equal(r2.error, null);
  assert.equal(r2.text, 'line 1\nline 5\n');

  // 3. insert_after à différentes positions (0, milieu, fin)
  const r3Top = applyLineOps(initial, [{ insert_after: 0, content: 'top line' }]);
  assert.equal(r3Top.error, null);
  assert.equal(r3Top.text, 'top line\nline 1\nline 2\nline 3\nline 4\nline 5\n');

  const r3Mid = applyLineOps(initial, [{ insert_after: 2, content: 'inserted at 2' }]);
  assert.equal(r3Mid.error, null);
  assert.equal(r3Mid.text, 'line 1\nline 2\ninserted at 2\nline 3\nline 4\nline 5\n');

  const r3End = applyLineOps(initial, [{ insert_after: 5, content: 'appended line' }]);
  assert.equal(r3End.error, null);
  assert.equal(r3End.text, 'line 1\nline 2\nline 3\nline 4\nline 5\nappended line\n');

  // 4. Combinaison multi-opérations appliquée de bas en haut
  const r4 = applyLineOps(initial, [
    { insert_after: 1, content: 'after 1' },
    { delete: [3, 3] },
    { replace: [5, 5], content: 'replaced 5' }
  ]);
  assert.equal(r4.error, null);
  assert.equal(r4.text, 'line 1\nafter 1\nline 2\nline 4\nreplaced 5\n');
});

test('applyLineOps - rejet des intervalles et opérations invalides', () => {
  const initial = 'line 1\nline 2\nline 3\n';

  // replace hors limites
  assert.match(applyLineOps(initial, [{ replace: [0, 2], content: 'x' }]).error, /replace range must be inside/);
  assert.match(applyLineOps(initial, [{ replace: [1, 4], content: 'x' }]).error, /replace range must be inside/);
  assert.match(applyLineOps(initial, [{ replace: [3, 2], content: 'x' }]).error, /replace range must be inside/);

  // delete hors limites
  assert.match(applyLineOps(initial, [{ delete: [0, 2] }]).error, /delete range must be inside/);
  assert.match(applyLineOps(initial, [{ delete: [2, 5] }]).error, /delete range must be inside/);

  // insert_after hors limites
  assert.match(applyLineOps(initial, [{ insert_after: -1, content: 'x' }]).error, /insert_after must be inside/);
  assert.match(applyLineOps(initial, [{ insert_after: 4, content: 'x' }]).error, /insert_after must be inside/);

  // Deux insert_after à la même position
  assert.match(
    applyLineOps(initial, [
      { insert_after: 2, content: 'a' },
      { insert_after: 2, content: 'b' }
    ]).error,
    /another insert already targets position 2/
  );

  // Deux plages qui se chevauchent
  assert.match(
    applyLineOps(initial, [
      { replace: [1, 2], content: 'a' },
      { delete: [2, 3] }
    ]).error,
    /operations overlap/
  );

  // Opération inconnue
  assert.match(
    applyLineOps(initial, [{ foo: 'bar' }]).error,
    /expected replace, delete or insert_after/
  );
});

test('editFileBlock - détection de conflit de hash (Stale read)', async () => {
  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), 'file-block-test-'));
  const filePath = path.join(tmpDir, 'sample.txt');
  const initialContent = 'first line\nsecond line\nthird line\n';
  await fs.writeFile(filePath, initialContent, 'utf8');

  const correctHash = crypto.createHash('sha256').update(Buffer.from(initialContent, 'utf8')).digest('hex').slice(0, 8);
  const staleHash = 'deadbeef';

  // Tentative avec mauvais hash -> doit échouer
  await assert.rejects(
    () => editFileBlock({
      absPath: filePath,
      ops: [{ replace: [1, 1], content: 'modified line' }],
      expectedHash: staleHash
    }),
    /Stale read: the block changed since you read it/
  );

  // Le fichier ne doit pas avoir bougé
  const currentContent = await fs.readFile(filePath, 'utf8');
  assert.equal(currentContent, initialContent);

  // Tentative avec hash correct -> doit réussir
  const result = await editFileBlock({
    absPath: filePath,
    ops: [{ replace: [1, 1], content: 'modified line' }],
    expectedHash: correctHash
  });
  assert.match(result, /^\[ok\]/);
  assert.match(result, /modified line/);

  const updatedContent = await fs.readFile(filePath, 'utf8');
  assert.equal(updatedContent, 'modified line\nsecond line\nthird line\n');

  await fs.rm(tmpDir, { recursive: true, force: true });
});

test('editFileBlock - écriture atomique et préservation des fins de ligne (CRLF et LF)', async () => {
  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), 'file-block-crlf-'));

  // Test avec CRLF
  const crlfPath = path.join(tmpDir, 'test_crlf.ps1');
  const crlfContent = 'Write-Host "one"\r\nWrite-Host "two"\r\nWrite-Host "three"\r\n';
  await fs.writeFile(crlfPath, crlfContent, 'utf8');

  await editFileBlock({
    absPath: crlfPath,
    ops: [{ replace: [2, 2], content: 'Write-Host "modified two"' }]
  });

  const savedCrlf = await fs.readFile(crlfPath, 'utf8');
  assert.equal(savedCrlf, 'Write-Host "one"\r\nWrite-Host "modified two"\r\nWrite-Host "three"\r\n');
  assert.ok(savedCrlf.includes('\r\n'));

  // Test avec LF
  const lfPath = path.join(tmpDir, 'test_lf.sh');
  const lfContent = 'echo "one"\necho "two"\necho "three"\n';
  await fs.writeFile(lfPath, lfContent, 'utf8');

  await editFileBlock({
    absPath: lfPath,
    ops: [{ replace: [2, 2], content: 'echo "modified two"' }]
  });

  const savedLf = await fs.readFile(lfPath, 'utf8');
  assert.equal(savedLf, 'echo "one"\necho "modified two"\necho "three"\n');
  assert.ok(!savedLf.includes('\r\n'));

  // Test avec UTF-8 BOM
  const bomPath = path.join(tmpDir, 'test_bom.txt');
  const bomContent = '\uFEFFalpha\r\nbeta\r\n';
  await fs.writeFile(bomPath, bomContent, 'utf8');

  await editFileBlock({
    absPath: bomPath,
    ops: [{ replace: [1, 1], content: 'ALPHA' }]
  });

  const savedBom = await fs.readFile(bomPath);
  assert.equal(savedBom[0], 0xEF);
  assert.equal(savedBom[1], 0xBB);
  assert.equal(savedBom[2], 0xBF);
  assert.equal(savedBom.toString('utf8'), '\uFEFFALPHA\r\nbeta\r\n');

  await fs.rm(tmpDir, { recursive: true, force: true });
});

test('renderCompactDiff - format de sortie', () => {
  const oldText = 'line 1\nline 2\nline 3\n';
  const newText = 'line 1\nmodified 2\nline 3\n';
  const newHash = crypto.createHash('sha1').update(newText).digest('hex');

  const res = renderCompactDiff('test.txt', oldText, newText, newHash);
  assert.match(res, /^\[ok\] test\.txt \[h:[a-f0-9]{8}\] \(3 lines\)/);
  assert.match(res, /\+ 2 \| modified 2/);
  assert.match(res, /-   \| line 2/);
});

test('deleteFileBlock - suppression et contrôle expected_heading', async () => {
  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), 'file-block-del-'));
  const filePath = path.join(tmpDir, 'target.txt');
  await fs.writeFile(filePath, 'hello', 'utf8');

  // Mauvais expected_heading
  await assert.rejects(
    () => deleteFileBlock({ absPath: filePath, expectedHeading: 'wrong.txt' }),
    /expected_heading guard failed/
  );

  // Bon expected_heading
  const res = await deleteFileBlock({ absPath: filePath, expectedHeading: 'target.txt' });
  assert.equal(res, `[ok] ${filePath} (deleted)`);

  // Vérifier suppression physique
  await assert.rejects(() => fs.stat(filePath), /ENOENT/);

  // Tentative sur dossier
  await assert.rejects(
    () => deleteFileBlock({ absPath: tmpDir }),
    /Cannot delete directory/
  );

  await fs.rm(tmpDir, { recursive: true, force: true });
});