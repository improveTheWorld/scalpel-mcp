import assert from 'node:assert/strict'
import { test } from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { createHash } from 'node:crypto'
import { createFallback } from '../lib/fallback.js'
import {
  MODES,
  ESCALATION_FIELDS,
  validateEscalationArgs,
  approveEscalation,
  canonicalPath,
  writableRoots,
  sandboxDenial,
  isPathInside
} from '../lib/sandbox.js'

function makeTestHarness({ mode = 'workspace-write', workspaceRoot, approverOutcome = 'allowed-once' } = {}) {
  const ws = workspaceRoot || fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-test-ws-'))
  let requestCalls = []
  const approver = {
    request: async (opts) => {
      requestCalls.push(opts)
      return typeof approverOutcome === 'function' ? approverOutcome(opts) : approverOutcome
    }
  }
  const sandboxPolicy = {
    resolve: () => ({ mode, workspaceRoot: ws })
  }
  const fallback = createFallback({
    getMode: () => 'on',
    executeScalpel: async (workspace, cmd, args) => {
      return { status: 'error', error: 'exists on disk but is not indexed' }
    },
    getApproval: () => approver,
    getSandboxPolicy: () => sandboxPolicy
  })
  return {
    ws,
    fallback,
    approver,
    requestCalls,
    sandboxPolicy,
    cleanup: () => {
      try { fs.rmSync(ws, { recursive: true, force: true }) } catch {}
    }
  }
}

// 1. Matrice mutations x modes x emplacements
test('1. Matrice mutations: (write, edit, delete) x (3 modes) x (3 emplacements)', async () => {
  const outsideDir = path.join(os.homedir(), 'scalpel-mat-out-' + Date.now())
  fs.mkdirSync(outsideDir, { recursive: true })
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-mat-ws-'))
  const tmpLoc = os.tmpdir()

  const locations = [
    { name: 'workspaceRoot', dir: ws, inRoots: true },
    { name: 'os.tmpdir()', dir: tmpLoc, inRoots: true },
    { name: 'outside', dir: outsideDir, inRoots: false }
  ]

  const modes = ['read-only', 'workspace-write', 'danger-full-access']

  try {
    for (const m of modes) {
      for (const loc of locations) {
        const h = makeTestHarness({ mode: m, workspaceRoot: ws })
        const session = { id: `sess-${m}-${loc.name}` }
        const exec = { agent: { session }, callId: 'c1' }

        // Test WRITE (fichier absent)
        const newFile = path.join(loc.dir, `new-${m}-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`)
        const shouldWritePass = m === 'danger-full-access' || (m === 'workspace-write' && loc.inRoots)
        if (shouldWritePass) {
          const res = await h.fallback.write({ path: newFile, content: 'hello' }, exec, ws)
          assert.equal(res.status, 'success')
          assert.equal(fs.existsSync(newFile), true)
        } else {
          await assert.rejects(
            async () => { await h.fallback.write({ path: newFile, content: 'hello' }, exec, ws) },
            /sandbox: file access denied/
          )
        }

        // Test EDIT (fichier lu)
        const editFile = path.join(loc.dir, `edit-${m}-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`)
        fs.writeFileSync(editFile, 'before', 'utf-8')
        await h.fallback.read({ path: editFile }, exec, ws)

        const shouldEditPass = m === 'danger-full-access' || (m === 'workspace-write' && loc.inRoots)
        if (shouldEditPass) {
          const res = await h.fallback.edit({ path: editFile, old_string: 'before', new_string: 'after' }, exec, ws)
          assert.equal(res.status, 'success')
          assert.equal(fs.readFileSync(editFile, 'utf-8'), 'after')
        } else {
          await assert.rejects(
            async () => { await h.fallback.edit({ path: editFile, old_string: 'before', new_string: 'after' }, exec, ws) },
            /sandbox: file access denied/
          )
        }

        // Test DELETE
        const delFile = path.join(loc.dir, `del-${m}-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`)
        fs.writeFileSync(delFile, 'delete-me', 'utf-8')

        const shouldDeletePass = m === 'danger-full-access' || (m === 'workspace-write' && loc.inRoots)
        if (shouldDeletePass) {
          const res = await h.fallback.deleteFile({ path: delFile }, exec, ws)
          assert.equal(res.status, 'success')
          assert.equal(fs.existsSync(delFile), false)
        } else {
          await assert.rejects(
            async () => { await h.fallback.deleteFile({ path: delFile }, exec, ws) },
            /sandbox: file access denied/
          )
          try { fs.unlinkSync(delFile) } catch {}
        }

        assert.equal(h.requestCalls.length, 0, 'Aucun appel a approver.request quand aucune escalade demandee')
        try { fs.unlinkSync(newFile) } catch {}
        try { fs.unlinkSync(editFile) } catch {}
      }
    }
  } finally {
    try { fs.rmSync(outsideDir, { recursive: true, force: true }) } catch {}
    try { fs.rmSync(ws, { recursive: true, force: true }) } catch {}
  }
})

// 2. Escalade
test('2. Escalade: allowed-once, rejected, cancelled, unavailable, non strictement plus large, validation args', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-esc-ws-'))
  const outside = path.join(os.homedir(), 'scalpel-esc-out-' + Date.now())
  fs.mkdirSync(outside, { recursive: true })
  try {
    const targetFile = path.join(outside, 'target.txt')
    fs.writeFileSync(targetFile, 'content', 'utf-8')

    // 2.1 allowed-once seul rÃ©ussit, une seule demande par appel, pas de mÃ©moire
    {
      const h = makeTestHarness({ mode: 'workspace-write', workspaceRoot: ws, approverOutcome: 'allowed-once' })
      const exec = { agent: { session: { id: 's-esc-1' } }, callId: 'call-1' }
      await h.fallback.read({ path: targetFile }, exec, ws)

      const res = await h.fallback.edit({
        path: targetFile,
        old_string: 'content',
        new_string: 'updated',
        sandbox_permissions: 'danger-full-access',
        justification: 'Need access to external file for test.'
      }, exec, ws)

      assert.equal(res.status, 'success')
      assert.equal(h.requestCalls.length, 1)
      assert.equal(h.requestCalls[0].toolName, 'edit_unindexed')

      // DeuxiÃ¨me appel sans escalade -> doit de nouveau Ãªtre refusÃ© (pas de mÃ©moire d'octroi)
      await assert.rejects(
        async () => {
          await h.fallback.edit({
            path: targetFile,
            old_string: 'updated',
            new_string: 'again'
          }, exec, ws)
        },
        /sandbox: file access denied/
      )
      assert.equal(h.requestCalls.length, 1, 'Pas de demande supplÃ©mentaire sans sandbox_permissions')

      // DeuxiÃ¨me appel identique avec escalade -> deuxiÃ¨me demande
      const res2 = await h.fallback.edit({
        path: targetFile,
        old_string: 'updated',
        new_string: 'again',
        sandbox_permissions: 'danger-full-access',
        justification: 'Second edit request.'
      }, exec, ws)
      assert.equal(res2.status, 'success')
      assert.equal(h.requestCalls.length, 2, 'Deuxieme appel identique = deuxieme demande')
    }

    // 2.2 rejected, cancelled, unavailable
    for (const outcome of ['rejected', 'cancelled', 'unavailable']) {
      const h = makeTestHarness({ mode: 'workspace-write', workspaceRoot: ws, approverOutcome: outcome })
      const exec = { agent: { session: { id: `s-esc-${outcome}` } }, callId: 'call-out' }
      await h.fallback.read({ path: targetFile }, exec, ws)

      await assert.rejects(
        async () => {
          await h.fallback.edit({
            path: targetFile,
            old_string: 'again',
            new_string: 'fail',
            sandbox_permissions: 'danger-full-access',
            justification: `Testing ${outcome}`
          }, exec, ws)
        },
        (err) => {
          if (outcome === 'rejected') assert.match(err.message, /the user rejected escalating/)
          else if (outcome === 'cancelled') assert.match(err.message, /approval for escalating to "danger-full-access" was cancelled/)
          else if (outcome === 'unavailable') assert.match(err.message, /no approval channel is available/)
          return true
        }
      )
    }

    // 2.3 Mode non strictement plus large: workspace-write depuis workspace-write
    {
      const h = makeTestHarness({ mode: 'workspace-write', workspaceRoot: ws, approverOutcome: 'allowed-once' })
      const exec = { agent: { session: { id: 's-esc-same' } }, callId: 'call-same' }
      await h.fallback.read({ path: targetFile }, exec, ws)

      // DemandÃ© workspace-write depuis workspace-write : rend le mode courant sans demande, mais hors racines donc refus bac Ã  sable
      await assert.rejects(
        async () => {
          await h.fallback.edit({
            path: targetFile,
            old_string: 'again',
            new_string: 'same',
            sandbox_permissions: 'workspace-write',
            justification: 'Same mode'
          }, exec, ws)
        },
        /sandbox: file access denied under workspace-write mode/
      )
      assert.equal(h.requestCalls.length, 0, 'Aucun appel a approver.request quand mode demande == mode effectif')
    }

    // 2.4 Mode read-only non strictement plus large
    {
      const h = makeTestHarness({ mode: 'workspace-write', workspaceRoot: ws, approverOutcome: 'allowed-once' })
      const exec = { agent: { session: { id: 's-esc-narrower' } }, callId: 'call-narrower' }
      await assert.rejects(
        async () => {
          await h.fallback.edit({
            path: targetFile,
            old_string: 'again',
            new_string: 'narrow',
            sandbox_permissions: 'read-only',
            justification: 'Narrower mode'
          }, exec, ws)
        },
        /is not strictly wider/
      )
    }

    // 2.5 Validation args escalade
    {
      const h = makeTestHarness({ mode: 'workspace-write', workspaceRoot: ws })
      const exec = { agent: { session: { id: 's-val' } } }

      await assert.rejects(
        async () => {
          await h.fallback.write({ path: path.join(outside, 'x.txt'), content: 'a', sandbox_permissions: 'danger-full-access' }, exec, ws)
        },
        /invalid escalation: sandbox_permissions requires a justification/
      )
      await assert.rejects(
        async () => {
          await h.fallback.write({ path: path.join(outside, 'x.txt'), content: 'a', justification: 'some justif' }, exec, ws)
        },
        /invalid escalation: justification is only valid together with sandbox_permissions/
      )
      await assert.rejects(
        async () => {
          await h.fallback.write({ path: path.join(outside, 'x.txt'), content: 'a', sandbox_permissions: 'danger-full-access', justification: '   ' }, exec, ws)
        },
        /invalid justification: expected a non-empty sentence/
      )
    }
  } finally {
    try { fs.rmSync(ws, { recursive: true, force: true }) } catch {}
    try { fs.rmSync(outside, { recursive: true, force: true }) } catch {}
  }
})

// 3. Lecture libre
test('3. Lecture: read_unindexed hors workspace et hors racines reussit dans les 3 modes, sans approver, y compris file_text', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-read-ws-'))
  const outside = path.join(os.homedir(), 'scalpel-read-out-' + Date.now())
  fs.mkdirSync(outside, { recursive: true })

  try {
    const fPath = path.join(outside, 'unindexed.txt')
    fs.writeFileSync(fPath, 'line 1\nline 2\n', 'utf-8')

    for (const m of ['read-only', 'workspace-write', 'danger-full-access']) {
      const h = makeTestHarness({ mode: m, workspaceRoot: ws })
      const res = await h.fallback.read({ path: fPath }, {}, ws)
      assert.equal(res.lines_returned, 2)
      assert.equal(h.requestCalls.length, 0, 'La lecture ne doit jamais appeler approver')
    }

    // Fichier file_text
    const ftPath = path.join(ws, 'raw.txt')
    fs.writeFileSync(ftPath, 'raw text\n', 'utf-8')
    const fallbackFt = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'success', kind: 'file_text' }),
      getSandboxPolicy: () => ({ resolve: () => ({ mode: 'read-only', workspaceRoot: ws }) })
    })
    const resFt = await fallbackFt.read({ path: 'raw.txt' }, {}, ws)
    assert.equal(resFt.lines_returned, 1)
  } finally {
    try { fs.rmSync(ws, { recursive: true, force: true }) } catch {}
    try { fs.rmSync(outside, { recursive: true, force: true }) } catch {}
  }
})

// 4. Observation
test('4. Observation: edit sans lecture, apres lecture, modifie, write existant, absent, sans session, apres delete', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-obs-ws-'))
  try {
    const fPath = path.join(ws, 'ignored_obs.txt')
    fs.writeFileSync(fPath, 'original\n', 'utf-8')

    const h = makeTestHarness({ mode: 'workspace-write', workspaceRoot: ws })
    const session = { id: 'session-obs' }
    const exec = { agent: { session }, callId: 'c1' }

    // 4.1 edit sans lecture -> refus
    await assert.rejects(
      async () => { await h.fallback.edit({ path: 'ignored_obs.txt', old_string: 'original', new_string: 'mod1' }, exec, ws) },
      /edit requires reading "ignored_obs.txt" first/
    )

    // 4.2 edit apres lecture -> reussit
    await h.fallback.read({ path: 'ignored_obs.txt' }, exec, ws)
    const resEdit = await h.fallback.edit({ path: 'ignored_obs.txt', old_string: 'original', new_string: 'mod1' }, exec, ws)
    assert.equal(resEdit.status, 'success')
    assert.equal(fs.readFileSync(fPath, 'utf-8'), 'mod1\n')

    // 4.3 fichier modifie entre lecture et edition -> refus
    fs.writeFileSync(fPath, 'external_change\n', 'utf-8')
    await assert.rejects(
      async () => { await h.fallback.edit({ path: 'ignored_obs.txt', old_string: 'mod1', new_string: 'mod2' }, exec, ws) },
      /the file changed since you read it; read it again/
    )

    // 4.4 write d'un fichier existant non lu -> refus
    const existFile = path.join(ws, 'exist.txt')
    fs.writeFileSync(existFile, 'existing content', 'utf-8')
    await assert.rejects(
      async () => { await h.fallback.write({ path: 'exist.txt', content: 'new content' }, exec, ws) },
      /it already exists and was not read in this session; read it first/
    )

    // write apres lecture -> reussit
    await h.fallback.read({ path: 'exist.txt' }, exec, ws)
    const resWrite = await h.fallback.write({ path: 'exist.txt', content: 'new content' }, exec, ws)
    assert.equal(resWrite.status, 'success')

    // write d'un fichier absent -> reussit
    const absentFile = path.join(ws, 'absent.txt')
    const resAbsent = await h.fallback.write({ path: 'absent.txt', content: 'created' }, exec, ws)
    assert.equal(resAbsent.status, 'success')

    // 4.5 sans session : lecture libre, creation permise, edit et write d'existant refuses
    const execNoSession = {}
    const resReadNoSess = await h.fallback.read({ path: 'absent.txt' }, execNoSession, ws)
    assert.equal(resReadNoSess.lines_returned, 1)

    const absent2 = path.join(ws, 'absent2.txt')
    const resCreateNoSess = await h.fallback.write({ path: 'absent2.txt', content: 'ok' }, execNoSession, ws)
    assert.equal(resCreateNoSess.status, 'success')

    await assert.rejects(
      async () => { await h.fallback.edit({ path: 'absent2.txt', old_string: 'ok', new_string: 'ko' }, execNoSession, ws) },
      /edit requires reading "absent2.txt" first/
    )
    await assert.rejects(
      async () => { await h.fallback.write({ path: 'absent2.txt', content: 'replace' }, execNoSession, ws) },
      /it already exists and was not read in this session; read it first/
    )

    // 4.6 apres delete, write du meme chemin reussit (absent)
    const toDel = path.join(ws, 'to_del.txt')
    fs.writeFileSync(toDel, 'bye', 'utf-8')
    await h.fallback.deleteFile({ path: 'to_del.txt' }, exec, ws)
    assert.equal(fs.existsSync(toDel), false)

    const resRecreate = await h.fallback.write({ path: 'to_del.txt', content: 'reborn' }, exec, ws)
    assert.equal(resRecreate.status, 'success')
  } finally {
    try { fs.rmSync(ws, { recursive: true, force: true }) } catch {}
  }
})

// 5. Service sandboxPolicy absent, ou resolve qui lÃ¨ve, ou mode inconnu -> refus fermÃ©
test('5. Service sandboxPolicy absent, ou resolve qui leve, ou mode inconnu -> refus ferme', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-svc-ws-'))
  try {
    const fPath = path.join(ws, 'f.txt')
    fs.writeFileSync(fPath, 'text', 'utf-8')

    // Absent
    const fbAbsent = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' }),
      getSandboxPolicy: () => undefined
    })
    await assert.rejects(
      async () => { await fbAbsent.deleteFile({ path: 'f.txt' }, {}, ws) },
      /Politique de bac à sable indisponible : opération refusée\./
    )

    // Resolve qui lÃ¨ve
    const fbThrow = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' }),
      getSandboxPolicy: () => ({ resolve: () => { throw new Error('resolve crashed') } })
    })
    await assert.rejects(
      async () => { await fbThrow.deleteFile({ path: 'f.txt' }, {}, ws) },
      /Politique de bac à sable indisponible : opération refusée\./
    )

    // Mode inconnu
    const fbInvalid = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' }),
      getSandboxPolicy: () => ({ resolve: () => ({ mode: 'super-admin', workspaceRoot: ws }) })
    })
    await assert.rejects(
      async () => { await fbInvalid.deleteFile({ path: 'f.txt' }, {}, ws) },
      /Politique de bac à sable indisponible : opération refusée\./
    )
  } finally {
    try { fs.rmSync(ws, { recursive: true, force: true }) } catch {}
  }
})

// 6. unindexedFallback: off -> 4 outils refusent ; ask, on, absent -> actifs
test('6. unindexedFallback: off -> 4 outils refusent ; ask, on, absent -> actifs', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-modes-ws-'))
  try {
    const fPath = path.join(ws, 'mode_test.txt')
    fs.writeFileSync(fPath, 'mode test', 'utf-8')

    // off
    const fbOff = createFallback({
      getMode: () => 'off',
      executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' })
    })
    for (const op of ['read', 'edit', 'write', 'deleteFile']) {
      await assert.rejects(
        async () => {
          if (op === 'read') await fbOff.read({ path: 'mode_test.txt' }, {}, ws)
          if (op === 'edit') await fbOff.edit({ path: 'mode_test.txt', old_string: 'a', new_string: 'b' }, {}, ws)
          if (op === 'write') await fbOff.write({ path: 'mode_test.txt', content: 'c' }, {}, ws)
          if (op === 'deleteFile') await fbOff.deleteFile({ path: 'mode_test.txt' }, {}, ws)
        },
        /Repli hors index désactivé \(mode 'off'\)\./
      )
    }

    // ask, on, absent -> actifs (seul 'off' dÃ©sactive)
    for (const val of ['ask', 'on', undefined]) {
      const fbActive = createFallback({
        getMode: () => val,
        executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' }),
        getSandboxPolicy: () => ({ resolve: () => ({ mode: 'danger-full-access', workspaceRoot: ws }) })
      })
      const r = await fbActive.read({ path: 'mode_test.txt' }, {}, ws)
      assert.equal(r.lines_returned, 1)
    }
  } finally {
    try { fs.rmSync(ws, { recursive: true, force: true }) } catch {}
  }
})

// 7. Canonicalisation
test('7. Canonicalisation: \\\\?\\, alias local localhost\\c$, PhysicalDrive0, dossier', async (t) => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-canon-ws-'))
  try {
    const fPath = path.join(ws, 'indexed_sample.txt')
    fs.writeFileSync(fPath, 'some data', 'utf-8')

    const fbIndexed = createFallback({
      getMode: () => 'on',
      executeScalpel: async (_ws, cmd, args) => {
        return { status: 'success', kind: 'symbol', id: 's' }
      },
      getSandboxPolicy: () => ({ resolve: () => ({ mode: 'danger-full-access', workspaceRoot: ws }) })
    })

    if (process.platform === 'win32') {
      const driveLetter = ws[0]
      const rest = ws.slice(2)

      // \\?\C:\... d'un fichier du workspace indexÃ© -> refusÃ©
      const extendedPath = `\\\\?\\${driveLetter}:${rest}\\indexed_sample.txt`
      await assert.rejects(
        async () => { await fbIndexed.read({ path: extendedPath }, {}, ws) },
        /indexé par Scalpel/
      )

      // \\localhost\c$\... d'un fichier indexÃ© -> refusÃ©
      const adminPath = `\\\\localhost\\${driveLetter}$${rest}\\indexed_sample.txt`
      await assert.rejects(
        async () => { await fbIndexed.read({ path: adminPath }, {}, ws) },
        /indexé par Scalpel/
      )

      // Alias d'un fichier NON indexÃ© -> traitÃ© normalement
      const fbNotIndexed = createFallback({
        getMode: () => 'on',
        executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' }),
        getSandboxPolicy: () => ({ resolve: () => ({ mode: 'danger-full-access', workspaceRoot: ws }) })
      })
      const r = await fbNotIndexed.read({ path: adminPath }, {}, ws)
      assert.equal(r.lines_returned, 1)

      // \\.\PhysicalDrive0 -> not a regular file
      await assert.rejects(
        async () => { await fbNotIndexed.read({ path: '\\\\.\\PhysicalDrive0' }, {}, ws) },
        /not a regular file/
      )
    }

    // Dossier -> not a regular file
    const subDir = path.join(ws, 'sub_dir')
    fs.mkdirSync(subDir)
    const fbNorm = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'error', error: 'exists on disk but is not indexed' }),
      getSandboxPolicy: () => ({ resolve: () => ({ mode: 'danger-full-access', workspaceRoot: ws }) })
    })
    await assert.rejects(
      async () => { await fbNorm.read({ path: subDir }, {}, ws) },
      /not a regular file/
    )
  } finally {
    try { fs.rmSync(ws, { recursive: true, force: true }) } catch {}
  }
})

// 8. Garde d'Ã©ligibilitÃ©: fichier indexÃ© refusÃ© en danger-full-access
test('8. Garde d eligibilite: un fichier indexe reste refuse en danger-full-access', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-guard-ws-'))
  try {
    const fPath = path.join(ws, 'main.cs')
    fs.writeFileSync(fPath, 'class Main {}', 'utf-8')

    const fb = createFallback({
      getMode: () => 'on',
      executeScalpel: async () => ({ status: 'success', kind: 'symbol' }),
      getSandboxPolicy: () => ({ resolve: () => ({ mode: 'danger-full-access', workspaceRoot: ws }) })
    })

    await assert.rejects(
      async () => { await fb.read({ path: 'main.cs' }, {}, ws) },
      /indexé par Scalpel/
    )
    await assert.rejects(
      async () => { await fb.edit({ path: 'main.cs', old_string: 'Main', new_string: 'App' }, {}, ws) },
      /indexé par Scalpel/
    )
    await assert.rejects(
      async () => { await fb.deleteFile({ path: 'main.cs' }, {}, ws) },
      /indexé par Scalpel/
    )
  } finally {
    try { fs.rmSync(ws, { recursive: true, force: true }) } catch {}
  }
})

// 9. ParitÃ© avec dsh-sandbox
test('9. Parite: approveEscalation et validateEscalationArgs avec dsh-sandbox', async (t) => {
  const harnessPath = process.env.DSH_HARNESS_SANDBOX_PATH || ''
  let dshSandbox
  try {
    dshSandbox = await import(harnessPath)
  } catch (err) {
    t.skip(`dsh-sandbox non trouve a ${harnessPath}: ${err.message}`)
    return
  }

  assert.equal(typeof dshSandbox.validateEscalationArgs, 'function')
  assert.equal(typeof validateEscalationArgs, 'function')

  for (const [args, shouldThrow] of [
    [[undefined, undefined], false],
    [['danger-full-access', 'justif'], false],
    [['danger-full-access', undefined], true],
    [[undefined, 'justif'], true],
    [['danger-full-access', '   '], true]
  ]) {
    let scalpelErr = null
    let harnessErr = null
    try { validateEscalationArgs(...args) } catch (e) { scalpelErr = e.message }
    try { dshSandbox.validateEscalationArgs(...args) } catch (e) { harnessErr = e.message }
    assert.equal(scalpelErr, harnessErr, `Validation mismatch for args ${JSON.stringify(args)}`)
  }

  const dummyAgent = { id: 'agent-1' }
  for (const outcome of ['allowed-once', 'rejected', 'cancelled', 'unavailable']) {
    const approver = { request: async () => outcome }
    let scalpelRes = null, scalpelErr = null
    let harnessRes = null, harnessErr = null

    try {
      scalpelRes = await approveEscalation(
        { requestedMode: 'danger-full-access', effectiveMode: 'workspace-write', justification: 'test', subject: 'operation' },
        { approver, agent: dummyAgent, callId: 'c', toolName: 'testTool' }
      )
    } catch (e) { scalpelErr = e.message }

    try {
      harnessRes = await dshSandbox.approveEscalation(
        { requestedMode: 'danger-full-access', effectiveMode: 'workspace-write', justification: 'test', subject: 'operation' },
        { approver, agent: dummyAgent, callId: 'c', toolName: 'testTool' }
      )
    } catch (e) { harnessErr = e.message }

    assert.equal(scalpelRes, harnessRes, `Outcome mismatch for ${outcome}`)
    assert.equal(scalpelErr, harnessErr, `Error mismatch for ${outcome}`)
  }
})
