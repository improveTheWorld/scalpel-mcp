import assert from 'node:assert/strict'
import { test } from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { apply } from '../lib/index.js'
import { createFallback } from '../lib/fallback.js'

test('Lot U - apply enregistre 8 outils (retrait de delete_unindexed au profit de delete_block)', () => {
  const tools = new Map()
  const fakeCtx = {
    tools: { register: (t) => tools.set(t.name, t) },
    inject: () => {}
  }
  apply(fakeCtx, {})
  assert.equal(tools.size, 8, 'Il doit y avoir exactement 8 outils')
  assert.equal(tools.has('delete_unindexed'), false, 'delete_unindexed ne doit plus etre enregistre')
})

test('Lot D - delete_unindexed verifie expected_hash et refuse dossier', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-del-guards-'))
  try {
    const fPath = path.join(ws, 'temp.txt')
    fs.writeFileSync(fPath, 'content', 'utf-8')

    const fallback = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' }),
      getSandboxPolicy: () => ({ resolve: () => ({ mode: 'workspace-write', workspaceRoot: ws }) })
    })

    // Hash perime -> refuse
    await assert.rejects(
      async () => {
        await fallback.deleteFile({ path: 'temp.txt', expected_hash: 'badhash' }, {}, ws)
      },
      (err) => {
        assert.match(err.message, /expected_hash|conflit/i)
        return true
      }
    )
    assert.equal(fs.existsSync(fPath), true, 'Fichier ne doit pas etre supprime si hash invalide')

    // Dossier -> refuse
    const subDir = path.join(ws, 'my_folder')
    fs.mkdirSync(subDir)
    await assert.rejects(
      async () => {
        await fallback.deleteFile({ path: 'my_folder' }, {}, ws)
      },
      (err) => {
        assert.match(err.message, /dossier|not a regular file/i)
        return true
      }
    )
    assert.equal(fs.existsSync(subDir), true, 'Dossier ne doit pas etre supprime')
  } finally {
    fs.rmSync(ws, { recursive: true, force: true })
  }
})

test('Lot D - delete_unindexed refuse si le fichier est indexe', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-del-indexed-'))
  try {
    const fPath = path.join(ws, 'main.cs')
    fs.writeFileSync(fPath, 'class Main {}', 'utf-8')

    const fallback = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'success', kind: 'symbol' }),
      getSandboxPolicy: () => ({ resolve: () => ({ mode: 'workspace-write', workspaceRoot: ws }) })
    })

    await assert.rejects(
      async () => {
        await fallback.deleteFile({ path: 'main.cs' }, {}, ws)
      },
      (err) => {
        assert.match(err.message, /indexé/i)
        return true
      }
    )
    assert.equal(fs.existsSync(fPath), true, 'Fichier indexe ne doit pas etre supprime')
  } finally {
    fs.rmSync(ws, { recursive: true, force: true })
  }
})