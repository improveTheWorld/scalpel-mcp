import assert from 'node:assert/strict'
import { test } from 'node:test'
import fs from 'node:fs/promises'
import fsSync from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { createHash } from 'node:crypto'
import { createFallback, safeGet } from '../lib/fallback.js'
import { apply } from '../lib/index.js'

async function createTestEnv() {
  const baseTmp = os.tmpdir() || process.env.TEMP || process.env.TMP || 'C:\\Windows\\Temp'
  const tmpDir = await fs.mkdtemp(path.join(baseTmp, 'fallback-test-'))
  const cleanup = async () => {
    try { await fs.rm(tmpDir, { recursive: true, force: true }) } catch {}
  }
  return { tmpDir, cleanup }
}

test("(6) BYPASS : fichier indexé (faux read_block success kind symbol, et variante réponse string) refusé même en mode 'on'", async () => {
  const { tmpDir, cleanup } = await createTestEnv()
  try {
    await fs.writeFile(path.join(tmpDir, 'indexed.cs'), 'public class C {}')

    // Variante 1: status success kind symbol
    const fallback1 = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'success', kind: 'symbol', id: 'C.cs::C' })
    })

    await assert.rejects(
      async () => {
        await fallback1.read({ path: 'indexed.cs' }, {}, tmpDir)
      },
      /indexé par Scalpel/
    )
    await assert.rejects(
      async () => {
        await fallback1.edit({ path: 'indexed.cs', old_string: 'C', new_string: 'D' }, {}, tmpDir)
      },
      /indexé par Scalpel/
    )

    // Variante 2: réponse string (listing)
    const fallback2 = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => 'indexed.cs (10 lines, 200 bytes)\n  class C (1-10)'
    })

    await assert.rejects(
      async () => {
        await fallback2.read({ path: 'indexed.cs' }, {}, tmpDir)
      },
      /indexé par Scalpel/
    )
    await assert.rejects(
      async () => {
        await fallback2.edit({ path: 'indexed.cs', old_string: 'C', new_string: 'D' }, {}, tmpDir)
      },
      /indexé par Scalpel/
    )
  } finally {
    await cleanup()
  }
})

test('(9) edit : old_string 0 occurrence ou >1 sans replace_all => aucune écriture ; expected_hash périmé => aucune écriture ; réussite => nouveau hash, contenu exact', async () => {
  const { tmpDir, cleanup } = await createTestEnv()
  try {
    const filePath = path.join(tmpDir, 'edit_test.txt')
    const initialText = 'apple orange banana orange'
    await fs.writeFile(filePath, initialText)

    const fallback = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' })
    })

    // 0 occurrence
    await assert.rejects(
      async () => {
        await fallback.edit({ path: 'edit_test.txt', old_string: 'pineapple', new_string: 'grape' }, {}, tmpDir)
      },
      /introuvable/
    )
    assert.equal(await fs.readFile(filePath, 'utf-8'), initialText)

    // > 1 occurrence sans replace_all
    await assert.rejects(
      async () => {
        await fallback.edit({ path: 'edit_test.txt', old_string: 'orange', new_string: 'grape', replace_all: false }, {}, tmpDir)
      },
      /apparaît 2 fois/
    )
    assert.equal(await fs.readFile(filePath, 'utf-8'), initialText)

    // expected_hash périmé
    await assert.rejects(
      async () => {
        await fallback.edit({ path: 'edit_test.txt', old_string: 'apple', new_string: 'pear', expected_hash: '0000000000000000000000000000000000000000' }, {}, tmpDir)
      },
      /Conflit de version/
    )
    assert.equal(await fs.readFile(filePath, 'utf-8'), initialText)

    // Réussite
    const currentSha = createHash('sha1').update(Buffer.from(initialText, 'utf-8')).digest('hex')
    const res = await fallback.edit({ path: 'edit_test.txt', old_string: 'banana', new_string: 'cherry', expected_hash: currentSha }, {}, tmpDir)
    assert.equal(res.status, 'success')
    const expectedNewText = 'apple orange cherry orange'
    const expectedNewSha = createHash('sha1').update(Buffer.from(expectedNewText, 'utf-8')).digest('hex')
    assert.equal(res.content_hash, expectedNewSha)
    assert.equal(await fs.readFile(filePath, 'utf-8'), expectedNewText)
  } finally {
    await cleanup()
  }
})

test('(10) read borné : fichier de plus de limit lignes tronqué avec note ; binaire refusé', async () => {
  const { tmpDir, cleanup } = await createTestEnv()
  try {
    const fallback = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' })
    })

    // Fichier de 15 lignes, lu avec limit=5
    const multiLinePath = path.join(tmpDir, 'multiline.txt')
    const lines = Array.from({ length: 15 }, (_, i) => `Ligne ${i + 1}`).join('\n')
    await fs.writeFile(multiLinePath, lines)

    const res = await fallback.read({ path: 'multiline.txt', offset: 1, limit: 5 }, {}, tmpDir)
    assert.equal(res.lines_returned, 5)
    assert.equal(res.total_lines, 15)
    assert.equal(res.truncated, true)
    assert.ok(res.text.includes('Note de troncature'), 'Doit inclure la note de troncature')

    // Fichier binaire (contenant un octet 0x00)
    const binPath = path.join(tmpDir, 'binary.bin')
    const binBuf = Buffer.from([0x48, 0x65, 0x6c, 0x6c, 0x6f, 0x00, 0x57, 0x6f, 0x72, 0x6c, 0x64])
    await fs.writeFile(binPath, binBuf)

    await assert.rejects(
      async () => {
        await fallback.read({ path: 'binary.bin' }, {}, tmpDir)
      },
      /binaire ou n'est pas encodé en UTF-8/
    )
  } finally {
    await cleanup()
  }
})

test('(12) hintFor : ajoute repli_hint seulement pour les 5 signatures et jamais en mode off', () => {
  const fallback = createFallback({
    getMode: () => 'on',
    executeScalpel: async () => ({})
  })

  const UNINDEXED_SIGS = [
    'resolves outside the workspace root',
    'matches the workspace ignore pattern',
    'exists on disk but is not indexed',
    'and no parser claims it: the raw text is returned below',
    'is not UTF-8 text'
  ]

  for (const sig of UNINDEXED_SIGS) {
    const errorPayload = { status: 'error', id: 'x', error: `Prefix ${sig} suffix` }
    const hinted = fallback.hintFor({ ...errorPayload })
    assert.ok(hinted.repli_hint, `Doit ajouter repli_hint pour signature: ${sig}`)
    assert.ok(hinted.repli_hint.includes('read_unindexed / edit_unindexed / write_unindexed'))
  }

  const otherErr = { status: 'error', id: 'x', error: 'check the spelling' }
  const noHint = fallback.hintFor({ ...otherErr })
  assert.equal(noHint.repli_hint, undefined, 'Ne doit pas ajouter repli_hint si pas de signature')

  const successPayload = { status: 'success', id: 'x' }
  const noHintSuccess = fallback.hintFor({ ...successPayload })
  assert.equal(noHintSuccess.repli_hint, undefined, 'Ne doit pas ajouter repli_hint en cas de succès')

  // mode off
  const fallbackOff = createFallback({
    getMode: () => 'off',
    executeScalpel: async () => ({})
  })
  const hintedOff = fallbackOff.hintFor({ status: 'error', id: 'x', error: 'resolves outside the workspace root' })
  assert.equal(hintedOff.repli_hint, undefined, 'Ne doit pas ajouter repli_hint en mode off')
})

test('(13) apply : enregistre 8 outils, relit le mode dynamiquement, et FLAT_TOOLS ne contient ni pwsh ni bash', async () => {
  const registeredTools = new Map()
  let commandRegistered = null

  const fakeCtx = {
    tools: {
      register: (t) => registeredTools.set(t.name, t)
    },
    inject: (deps, callback) => {
      if (deps.includes('commands')) {
        callback({
          commands: {
            register: (cmd) => { commandRegistered = cmd }
          }
        })
      }
    }
  }

  let currentMode = 'on'
  apply(fakeCtx, { unindexedFallback: currentMode })

  assert.equal(registeredTools.size, 8, 'Il doit y avoir exactement 8 outils universels enregistrés')
  assert.ok(registeredTools.has('read_block'))
  assert.ok(registeredTools.has('edit_block'))
  assert.ok(registeredTools.has('insert_block'))
  assert.ok(registeredTools.has('delete_block'))
  assert.equal(registeredTools.has('read_unindexed'), false)
  assert.equal(registeredTools.has('delete_unindexed'), false)
})

test('(D1-3) discordance simulée (moteur répond outside pour chemin canonique dans workspace) => refus', async () => {
  const { tmpDir, cleanup } = await createTestEnv()
  try {
    const subFile = path.join(tmpDir, 'inside.txt')
    await fs.writeFile(subFile, 'hello')

    const fallback = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({
        status: 'error',
        error: "path 'inside.txt' resolves outside the workspace root"
      })
    })

    await assert.rejects(
      async () => {
        await fallback.read({ path: 'inside.txt' }, {}, tmpDir)
      },
      /Discordance détectée/
    )
  } finally {
    await cleanup()
  }
})

test('(D4) signature is not UTF-8 text : éligible pour edit/write, hintFor actif, refus pour read', async () => {
  const { tmpDir, cleanup } = await createTestEnv()
  try {
    const fb = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({
        status: 'error',
        error: "'bad.txt' is not UTF-8 text: cannot decode as text."
      })
    })

    const hinted = fb.hintFor({
      status: 'error',
      id: 'bad.txt',
      error: "'bad.txt' is not UTF-8 text: cannot decode as text."
    })
    assert.ok(hinted.repli_hint, 'hintFor doit ajouter repli_hint pour is not UTF-8 text')

    const fPath = path.join(tmpDir, 'binary_file.dat')
    await fs.writeFile(fPath, Buffer.from([0x00, 0x01, 0x02, 0xff]))

    await assert.rejects(
      async () => {
        await fb.read({ path: 'binary_file.dat' }, {}, tmpDir)
      },
      /binaire ou n'est pas encodé en UTF-8/
    )

    const res = await fb.write({ path: 'binary_file.dat', content: 'new utf8 content' }, {}, tmpDir)
    assert.equal(res.status, 'success', 'write_unindexed doit réussir pour écraser avec du texte UTF-8 valide')
  } finally {
    await cleanup()
  }
})

test('(D5-a) edit_unindexed : conversion CRLF/LF dans les deux sens et préservation BOM', async () => {
  const { tmpDir, cleanup } = await createTestEnv()
  try {
    const fallback = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' })
    })

    // 1. Fichier CRLF avec old_string en LF pur
    const crlfFile = path.join(tmpDir, 'crlf.txt')
    await fs.writeFile(crlfFile, 'line1\r\nline2\r\nline3')
    await fallback.edit({ path: 'crlf.txt', old_string: 'line1\nline2', new_string: 'LINE1\nLINE2' }, {}, tmpDir)
    const crlfContent = await fs.readFile(crlfFile, 'utf-8')
    assert.equal(crlfContent, 'LINE1\r\nLINE2\r\nline3')

    // 2. Fichier LF pur avec old_string en CRLF
    const lfFile = path.join(tmpDir, 'lf.txt')
    await fs.writeFile(lfFile, 'lineA\nlineB\nlineC')
    await fallback.edit({ path: 'lf.txt', old_string: 'lineA\r\nlineB', new_string: 'LINEA\r\nLINEB' }, {}, tmpDir)
    const lfContent = await fs.readFile(lfFile, 'utf-8')
    assert.equal(lfContent, 'LINEA\nLINEB\nlineC')

    // 3. Fichier avec BOM UTF-8
    const bomFile = path.join(tmpDir, 'bom.txt')
    await fs.writeFile(bomFile, '\uFEFFheader\r\nvalue')
    await fallback.edit({ path: 'bom.txt', old_string: 'value', new_string: 'NEW_VALUE' }, {}, tmpDir)
    const bomContent = await fs.readFile(bomFile, 'utf-8')
    assert.ok(bomContent.startsWith('\uFEFF'), 'BOM UTF-8 doit être préservé')
    assert.equal(bomContent, '\uFEFFheader\r\nNEW_VALUE')
  } finally {
    await cleanup()
  }
})

test('(D5-b) préservation du mode de fichier (chmod)', async (t) => {
  if (process.platform === 'win32') {
    t.skip('chmod de mode Unix non pertinent sous Windows')
    return
  }
  const { tmpDir, cleanup } = await createTestEnv()
  try {
    const fallback = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' })
    })

    const scriptFile = path.join(tmpDir, 'script.sh')
    await fs.writeFile(scriptFile, '#!/bin/sh\necho hi\n')
    await fs.chmod(scriptFile, 0o755)

    await fallback.edit({ path: 'script.sh', old_string: 'hi', new_string: 'hello' }, {}, tmpDir)

    const st = await fs.stat(scriptFile)
    assert.equal(st.mode & 0o777, 0o755, 'Le mode de fichier 0755 doit être préservé')
  } finally {
    await cleanup()
  }
})

test('(D5-c) read_unindexed en flux sur fichier volumineux (5 Mo) : 10 lignes rendues et sha1 exact', async () => {
  const { tmpDir, cleanup } = await createTestEnv()
  try {
    const fallback = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' })
    })

    const bigFile = path.join(tmpDir, 'big.txt')
    const lineContent = 'A'.repeat(99) + '\n' // 100 octets par ligne
    const totalLines = 50000 // 5 Mo
    const ws = fsSync.createWriteStream(bigFile)
    for (let i = 0; i < totalLines; i++) {
      ws.write(lineContent)
    }
    await new Promise((resolve) => ws.end(resolve))

    const wholeBuf = await fs.readFile(bigFile)
    const expectedSha1 = createHash('sha1').update(wholeBuf).digest('hex')

    const res = await fallback.read({ path: 'big.txt', offset: 100, limit: 10 }, {}, tmpDir)

    assert.equal(res.lines_returned, 10)
    assert.equal(res.total_lines, totalLines)
    assert.equal(res.truncated, true)
    assert.equal(res.content_hash, expectedSha1, 'Le SHA1 doit être calculé sur TOUT le fichier via le flux')
    assert.ok(res.text.includes('100: '), 'Doit commencer à la ligne 100')
    assert.ok(res.text.includes('109: '), 'Doit inclure la ligne 109')
    assert.ok(res.text.includes('Note de troncature'))
  } finally {
    await cleanup()
  }
})

test('(R2) non-régression proxy Cordis : apply ne doit jamais lever sous un proxy qui interdit l accès direct aux services non injectés', () => {
  const registeredTools = new Map()
  const forbiddenAccesses = []

  const rawCtx = {
    tools: {
      register: (t) => registeredTools.set(t.name, t)
    },
    inject: (_deps, cb) => {}
  }

  const proxyCtx = new Proxy(rawCtx, {
    get(target, prop) {
      if (prop in target) return target[prop]
      forbiddenAccesses.push(prop)
      throw new Error(`Accès direct interdit au service non injecté : ${String(prop)}`)
    }
  })

  assert.doesNotThrow(() => {
    apply(proxyCtx, {})
  })
  assert.equal(registeredTools.size, 8, '8 outils doivent être enregistrés sans accès direct')
})

test('(Dette-1) safeGet est exporté par fallback.js et gère les erreurs', () => {
  assert.equal(typeof safeGet, 'function')
  assert.equal(safeGet(null, 'foo'), undefined)
  assert.equal(safeGet({}, 'foo'), undefined)
  assert.equal(safeGet({ get: () => 'val' }, 'foo'), 'val')
  assert.equal(safeGet({ get: () => { throw new Error('boom') } }, 'foo'), undefined)
})

test('(Dette-2) edit_unindexed supporte les fins de ligne mixtes (CRLF et LF)', async () => {
  const { tmpDir, cleanup } = await createTestEnv()
  try {
    const fallback = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' })
    })

    const mixedFile = path.join(tmpDir, 'mixed.txt')
    await fs.writeFile(mixedFile, 'line1\r\nline2\nline3\r\nline4')

    await fallback.edit({ path: 'mixed.txt', old_string: 'line2', new_string: 'LINE2' }, {}, tmpDir)
    assert.equal(await fs.readFile(mixedFile, 'utf-8'), 'line1\r\nLINE2\nline3\r\nline4')

    await fallback.edit({ path: 'mixed.txt', old_string: 'line3\r\nline4', new_string: 'LINE3\r\nLINE4' }, {}, tmpDir)
    assert.equal(await fs.readFile(mixedFile, 'utf-8'), 'line1\r\nLINE2\nLINE3\r\nLINE4')
  } finally {
    await cleanup()
  }
})

test('(LotA-F5) "the parser recognised no container" est eligible au repli et recoit repli_hint', async () => {
  const fallback = createFallback({
    getMode: () => 'on',
    executeScalpel: async () => ({
      status: 'error',
      error: "'dummy.test.js' is indexed, but the parser recognised no container (class, interface, namespace, struct, record or enum) in it, so there is no whole file to read."
    })
  })

  const hinted = fallback.hintFor({
    status: 'error',
    id: 'dummy.test.js',
    error: "'dummy.test.js' is indexed, but the parser recognised no container (class, interface, namespace, struct, record or enum) in it, so there is no whole file to read."
  })
  assert.ok(hinted.repli_hint, 'hintFor doit ajouter repli_hint pour parser recognised no container')
})

test('(LotA-F7) edit_unindexed avec expected_count : echec si count != expected_count (0 octet ecrit, hash inchange)', async () => {
  const { tmpDir, cleanup } = await createTestEnv()
  try {
    const filePath = path.join(tmpDir, 'file.txt')
    const initialText = 'foo bar foo baz foo'
    await fs.writeFile(filePath, initialText)
    const initialSha = createHash('sha1').update(Buffer.from(initialText, 'utf-8')).digest('hex')

    const fallback = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' })
    })

    await assert.rejects(
      async () => {
        await fallback.edit({
          path: 'file.txt',
          old_string: 'foo',
          new_string: 'qux',
          replace_all: true,
          expected_count: 2
        }, {}, tmpDir)
      },
      (err) => {
        assert.match(err.message, /Nombre d'occurrences inattendu|attendue/)
        return true
      }
    )

    const afterText = await fs.readFile(filePath, 'utf-8')
    const afterSha = createHash('sha1').update(Buffer.from(afterText, 'utf-8')).digest('hex')
    assert.equal(afterText, initialText, '0 octet ne doit etre ecrit')
    assert.equal(afterSha, initialSha, 'Le hash sha1 doit etre inchange')
  } finally {
    await cleanup()
  }
})

test('(LotA-F2) hintFor remplace actionable_hint si "is a FILE, not a block that can take a child"', () => {
  const fallback = createFallback({
    getMode: () => 'on',
    executeScalpel: async () => ({})
  })

  const payload = {
    status: 'error',
    id: 'myfile.js',
    error: "'myfile.js' is a FILE, not a block that can take a child.",
    actionable_hint: "insert_block(parent='myfile.js') creates one."
  }

  const hinted = fallback.hintFor({ ...payload })
  assert.ok(hinted.actionable_hint, 'actionable_hint doit exister')
  assert.equal(hinted.actionable_hint.includes('creates one'), false, 'Ne doit plus contenir creates one')
  assert.ok(hinted.actionable_hint.includes('insert_after'), 'Doit orienter vers insert_after')
})