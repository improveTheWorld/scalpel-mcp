import assert from 'node:assert/strict'
import path from 'node:path'
import { test } from 'node:test'
import { executeScalpel, ensureDaemon } from '../lib/bridge.js'

test('Scalpel bridge exécute search_symbols sur dsh-boost', async () => {
  const res = await executeScalpel('C:\\CodeSource\\dsh-boost', 'search_symbols', { query: 'ChannelStore', limit: 1 })
  const text = typeof res === 'string' ? res : JSON.stringify(res)
  assert.match(text, /ChannelStore/)
})

test('Scalpel bridge exécute read_block sur Scalpel-mcp', async () => {
  const res = await executeScalpel('C:\\CodeSource\\Scalpel-mcp', 'read_block', { path: 'pyproject.toml' })
  const text = typeof res === 'string' ? res : JSON.stringify(res)
  assert.match(text, /pyproject\.toml/)
})

test('Scalpel bridge démonisé : 5 appels consécutifs ont une latence chaude < 40 ms par appel', async () => {
  const ws = path.resolve(import.meta.dirname, '../../..')
  // 1er appel (warm-up du workspace si nécessaire)
  const first = await executeScalpel(ws, 'read_block', { path: 'src/ast_graph_server/config.py' })
  const firstText = typeof first === 'string' ? first : JSON.stringify(first)
  assert.match(firstText, /ServerConfig/)

  // 4 appels chauds consécutifs (2e au 5e)
  const t0 = performance.now()
  for (let i = 0; i < 4; i++) {
    const res = await executeScalpel(ws, 'read_block', { path: 'src/ast_graph_server/config.py' })
    const text = typeof res === 'string' ? res : JSON.stringify(res)
    assert.match(text, /ServerConfig/)
  }
  const elapsedMs = performance.now() - t0
  const avgMs = elapsedMs / 4
  assert.ok(avgMs < 40, `Latence moyenne chaude attendue < 40 ms, mesurée : ${avgMs.toFixed(2)} ms`)
})

test('Scalpel bridge démonisé : rafraîchissement mtime_cache, batch edits=[...] et suppression remove_file', async (t) => {
  const fs = await import('node:fs')
  const os = await import('node:os')
  const path = await import('node:path')
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-daemon-cache-'))
  t.after(() => {
    try { fs.rmSync(ws, { recursive: true, force: true }) } catch {}
  })

  const fPath = path.join(ws, 'mod.js')
  fs.writeFileSync(fPath, 'export function alpha() {\n  return 1\n}\n', 'utf8')

  // 1. Lecture initiale via edit_block (batch edits=[...]) sur un fichier jamais lu auparavant
  const r1 = await executeScalpel(ws, 'read_block', { path: 'mod.js::alpha' })
  const text1 = typeof r1 === 'string' ? r1 : JSON.stringify(r1)
  const m1 = text1.match(/\[h:([a-f0-9]+)\]/)
  assert.ok(m1, 'doit retourner le hash de alpha')

  // 2. Modification externe sur disque -> mtime_cache doit détecter le changement et réindexer
  fs.writeFileSync(fPath, 'export function alpha() {\n  return 1\n}\nexport function beta() {\n  return 2\n}\n', 'utf8')
  const r2 = await executeScalpel(ws, 'read_block', { path: 'mod.js::beta' })
  const text2 = typeof r2 === 'string' ? r2 : JSON.stringify(r2)
  const m2 = text2.match(/\[h:([a-f0-9]+)\]/)
  assert.ok(m2, 'doit voir beta après modification externe sur disque')

  // 3. Batch edits=[...] sur un second fichier créé hors Scalpel (vérifie l'extraction de edits[].path)
  const f2Path = path.join(ws, 'second.js')
  fs.writeFileSync(f2Path, 'export function gamma() {\n  return 10\n}\n', 'utf8')
  const r3 = await executeScalpel(ws, 'read_block', { path: 'second.js::gamma' })
  const m3 = String(r3).match(/\[h:([a-f0-9]+)\]/)
  assert.ok(m3, 'doit indexer second.js')

  const editRes = await executeScalpel(ws, 'edit_block', {
    edits: [
      { path: 'mod.js::beta', ops: [{ replace: [2, 2], content: '  return 20' }], expected_hash: m2[1] },
      { path: 'second.js::gamma', ops: [{ replace: [2, 2], content: '  return 30' }], expected_hash: m3[1] }
    ]
  })
  assert.match(String(editRes), /\[ok\]/, 'batch edit_block doit réussir')

  // 4. Suppression du fichier hors Scalpel -> remove_file doit purger l'index du démon
  fs.unlinkSync(f2Path)
  const rDeleted = await executeScalpel(ws, 'read_block', { path: 'second.js::gamma' })
  const deletedText = typeof rDeleted === 'string' ? rDeleted : JSON.stringify(rDeleted)
  assert.match(deletedText, /error|not in the index|No symbol/i, 'fichier supprimé doit être purgé de l’index')

  // 5. Auto-reload du démon Python lorsque la signature de code/version change
  const procBefore = ensureDaemon()
  assert.ok(procBefore && procBefore.pid, 'démon actif avant changement')
  const runnerPath = path.resolve(import.meta.dirname, '../lib/runner.py')
  const origStat = fs.statSync(runnerPath)
  const bumpedTime = new Date(origStat.mtimeMs + 5000)
  fs.utimesSync(runnerPath, origStat.atime, bumpedTime)
  try {
    await executeScalpel(ws, 'read_block', { path: 'mod.js::alpha' })
    const procAfter = ensureDaemon()
    assert.ok(procAfter && procAfter.pid, 'nouveau démon actif après changement')
    assert.notEqual(procAfter.pid, procBefore.pid, 'ensureDaemon doit redémarrer le démon Python quand le code change')
  } finally {
    try { fs.utimesSync(runnerPath, origStat.atime, origStat.mtime) } catch {}
  }
})
