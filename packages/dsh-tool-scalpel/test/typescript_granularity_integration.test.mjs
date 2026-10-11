import test from 'node:test';
import assert from 'node:assert/strict';
import { executeScalpel } from '../lib/bridge.js';

test('1. Outline d\'un fichier de test pur (delete_unindexed.test.mjs) retourne ses tests', async () => {
  const ws = process.cwd();
  const filePath = 'packages/dsh-tool-scalpel/test/delete_unindexed.test.mjs';

  const res = await executeScalpel(ws, 'read_block', { path: filePath, format: 'json' });
  assert.equal(res.status, 'success', `read_block sur ${filePath} doit réussir`);
  assert.ok(Array.isArray(res.children), 'Doit retourner un tableau de children');

  const names = res.children.map((e) => e.name);
  assert.ok(
    names.includes('Lot U - apply enregistre 8 outils (retrait de delete_unindexed au profit de delete_block)'),
    'Doit inclure le premier test'
  );
  assert.ok(
    names.includes('Lot D - delete_unindexed verifie expected_hash et refuse dossier'),
    'Doit inclure le deuxième test'
  );
  assert.ok(
    names.includes('Lot D - delete_unindexed refuse si le fichier est indexe'),
    'Doit inclure le troisième test'
  );
});

test('2. read_block sur un test spécifique renvoie le corps exact et le hash', async () => {
  const ws = process.cwd();
  const testId = 'packages/dsh-tool-scalpel/test/delete_unindexed.test.mjs::Lot D - delete_unindexed refuse si le fichier est indexe';

  const res = await executeScalpel(ws, 'read_block', { path: testId, format: 'json' });
  assert.equal(res.status, 'success', `read_block sur ${testId} doit réussir`);
  assert.equal(res.kind, 'symbol');
  assert.ok(res.content_hash, 'Le bloc de test doit avoir un content_hash');
  assert.ok(res.numbered_text.includes('main.cs'), 'Le texte doit contenir le corps du test');
});

test('3. edit_block permet de modifier un test individuellement avec son expected_hash', async () => {
  const ws = process.cwd();
  const testId = 'packages/dsh-tool-scalpel/test/delete_unindexed.test.mjs::Lot D - delete_unindexed refuse si le fichier est indexe';

  // 1. Lire le bloc de test
  const readRes = await executeScalpel(ws, 'read_block', { path: testId, format: 'json' });
  assert.equal(readRes.status, 'success');
  const initialHash = readRes.content_hash;

  // 2. Modifier une ligne du test (insérer un commentaire)
  const editRes = await executeScalpel(ws, 'edit_block', {
    path: testId,
    expected_hash: initialHash,
    ops: [
      {
        insert_after: 1,
        content: "  // Commentaire ajoute par test d'integration Lot G"
      }
    ],
    format: 'json'
  });

  assert.equal(editRes.status, 'success');
  assert.ok(editRes.content_hash, 'Doit retourner un nouveau hash');
  const updatedHash = editRes.content_hash;
  assert.notEqual(updatedHash, initialHash);

  // 3. Restaurer en supprimant la ligne ajoutée
  const revertRes = await executeScalpel(ws, 'edit_block', {
    path: testId,
    expected_hash: updatedHash,
    ops: [
      {
        delete: [2, 2]
      }
    ],
    format: 'json'
  });
  assert.equal(revertRes.status, 'success');
  assert.equal(revertRes.content_hash, initialHash, 'Le hash initial doit être restauré');
});

test('4. Closures imbriquées dans fallback.js adressables individuellement', async () => {
  const ws = process.cwd();
  const innerId = 'packages/dsh-tool-scalpel/lib/fallback.js::createFallback::checkEligibility';

  const res = await executeScalpel(ws, 'read_block', { path: innerId, format: 'json' });
  assert.equal(res.status, 'success', `read_block sur ${innerId} doit réussir`);
  assert.equal(res.kind, 'symbol');
  assert.ok(res.content_hash, 'La closure doit avoir un content_hash');
  assert.ok(res.numbered_text.includes('checkEligibility'), 'Le corps doit contenir la fonction');
});