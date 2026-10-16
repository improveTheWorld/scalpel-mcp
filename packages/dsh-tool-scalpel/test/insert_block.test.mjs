import assert from 'node:assert/strict'
import { test } from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { apply } from '../lib/index.js'
import { spawn } from 'node:child_process'

const PYTHON_PATH = process.env.PYTHON_PATH || 'python'
const SCALPEL_ROOT = process.env.SCALPEL_ROOT || path.resolve(import.meta.dirname, '../../..')
const RUNNER_PY = path.resolve(import.meta.dirname, '../lib/runner.py')

function runScalpel(workspace, cmd, args) {
  return new Promise((resolvePromise, rejectPromise) => {
    const proc = spawn(PYTHON_PATH, [RUNNER_PY, workspace, cmd, JSON.stringify(args || {})], {
      cwd: SCALPEL_ROOT,
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONPATH: path.resolve(SCALPEL_ROOT, 'src')
      }
    })

    let stdout = ''
    let stderr = ''
    proc.stdout.on('data', (d) => { stdout += d.toString() })
    proc.stderr.on('data', (d) => { stderr += d.toString() })

    proc.on('close', (code) => {
      if (code !== 0) {
        return rejectPromise(new Error('Runner error (code ' + code + '): ' + (stderr || stdout)))
      }
      try {
        const json = JSON.parse(stdout.trim())
        resolvePromise(json)
      } catch (err) {
        resolvePromise(stdout.trim())
      }
    })
  })
}

test('Lot B - Schema ops dans edit_block a items.type === "object"', () => {
  const tools = new Map()
  const fakeCtx = {
    tools: {
      register: (t) => tools.set(t.name, t)
    },
    inject: () => {}
  }
  apply(fakeCtx, {})
  const editTool = tools.get('edit_block')
  assert.ok(editTool, 'edit_block doit etre enregistre')
  const opsSchema = editTool.parameters.properties.ops
  assert.equal(opsSchema.type, 'array')
  assert.ok(opsSchema.items, 'ops doit definir items')
  assert.equal(opsSchema.items.type, 'object', 'items.type doit etre "object"')
  // Dans SCALPEL_SCHEMAS (banc de reference), ops est documente dans sa description
  assert.ok(opsSchema.description.includes('insert_after'), 'insert_after doit etre documente dans la description')
  assert.ok(opsSchema.description.includes('replace'), 'replace doit etre documente dans la description')
  assert.ok(opsSchema.description.includes('delete'), 'delete doit etre documente dans la description')
})

test('Lot B - insert_block avec content envoie text + "\\n" + content au moteur (mock)', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-insert-mock-'))
  try {
    let capturedCmd = null
    let capturedArgs = null
    const mockExecScalpel = async (w, cmd, args) => {
      capturedCmd = cmd
      capturedArgs = args
      return { status: 'success' }
    }

    const tools = new Map()
    const fakeCtx = {
      tools: {
        register: (t) => tools.set(t.name, t)
      },
      inject: () => {}
    }
    apply(fakeCtx, {}, { executeScalpel: mockExecScalpel })

    const insertTool = tools.get('insert_block')
    const fakeExec = { agent: { session: { header: { cwd: ws } } } }

    const res = await insertTool.execute({
      parent: '.',
      text: 'hello.js',
      content: 'console.log("hello");'
    }, fakeExec)

    assert.equal(capturedCmd, 'insert_block')
    assert.equal(capturedArgs.text, 'hello.js')
    assert.equal(capturedArgs.content, 'console.log("hello");')
    assert.equal(capturedArgs.parent, '.')
  } finally {
    fs.rmSync(ws, { recursive: true, force: true })
  }
})

test('Lot B - insert_block avec content refuse si parent = fichier ou si text contient saut de ligne', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-insert-err-'))
  try {
    const dummyFile = path.join(ws, 'existing.txt')
    fs.writeFileSync(dummyFile, 'hello', 'utf-8')

    const tools = new Map()
    const fakeCtx = {
      tools: {
        register: (t) => tools.set(t.name, t)
      },
      inject: () => {}
    }
    apply(fakeCtx, {}, { executeScalpel: async () => ({ status: 'success' }) })

    const insertTool = tools.get('insert_block')
    const fakeExec = { agent: { session: { header: { cwd: ws } } } }

    // Parent = document ou symbole : désormais autorisé et transmis nativement au moteur
    let capturedArgs = null
    const tools2 = new Map()
    apply({
      tools: { register: (t) => tools2.set(t.name, t) },
      inject: () => {}
    }, {}, {
      executeScalpel: async (_w, _cmd, args) => {
        capturedArgs = args
        return { status: 'success' }
      }
    })
    const resDoc = await tools2.get('insert_block').execute({
      parent: 'existing.md',
      text: 'Section Title',
      content: 'body with trailing newline\n'
    }, fakeExec)
    assert.equal(capturedArgs.parent, 'existing.md')
    assert.equal(capturedArgs.text, 'Section Title')
    assert.equal(capturedArgs.content, 'body with trailing newline\n')

    // content non-string -> refusé
    await assert.rejects(
      async () => {
        await insertTool.execute({
          parent: '.',
          text: 'child.js',
          content: 12345
        }, fakeExec)
      },
      (err) => {
        assert.match(err.message, /chaîne de caractères/i)
        return true
      }
    )
  } finally {
    fs.rmSync(ws, { recursive: true, force: true })
  }
})

test('Lot B - Integration reelle: 1000 octets crees par insert_block(content) et relus par read_block', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-insert-integ-'))
  try {
    const subDir = path.join(ws, 'sub')
    fs.mkdirSync(subDir)

    // Création d'un contenu JS valide d'exactement 1000 octets AVEC saut de ligne final (\n) préservé !
    let content = 'export function demo() {\n'
    while (Buffer.byteLength(content, 'utf-8') < 960) {
      content += '  const val = 123456789;\n'
    }
    const needed = 998 - Buffer.byteLength(content, 'utf-8')
    if (needed > 0) {
      content += '// ' + 'x'.repeat(needed - 4) + '\n'
    }
    content += '}\n'
    assert.equal(Buffer.byteLength(content, 'utf-8'), 1000)

    const tools = new Map()
    const fakeCtx = {
      tools: {
        register: (t) => tools.set(t.name, t)
      },
      inject: () => {}
    }
    apply(fakeCtx, {}, { executeScalpel: (w, cmd, args) => runScalpel(w, cmd, args) })

    const insertTool = tools.get('insert_block')
    const fakeExec = { agent: { session: { header: { cwd: ws } } } }

    const insRes = await insertTool.execute({
      parent: 'sub',
      text: 'a.mjs',
      content: content
    }, fakeExec)
    assert.equal(insRes.status, 'success')
    assert.equal(insRes.bytes, Buffer.byteLength(content, 'utf-8'))

    // Relecture via read_block
    const readRes = await runScalpel(ws, 'read_block', { path: 'sub/a.mjs', format: 'json' })
    assert.equal(readRes.status, 'success')
    // Vérification physique sur disque : le '\n' final est intégralement préservé
    const diskBytes = fs.readFileSync(path.join(subDir, 'a.mjs'), 'utf-8')
    assert.equal(diskBytes, content)
    assert.ok(diskBytes.endsWith('}\n'))
  } finally {
    fs.rmSync(ws, { recursive: true, force: true })
  }
})

test('Lot B - Integration reelle: runner.py supporte ops en chaine JSON stringifiee', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-ops-string-'))
  try {
    const fPath = path.join(ws, 'mod.js')
    fs.writeFileSync(fPath, 'function greet() {\n  return "hi";\n}\n', 'utf-8')

    // Initial read
    const read1 = await runScalpel(ws, 'read_block', { path: 'mod.js::greet', format: 'json' })
    assert.equal(read1.status, 'success')
    const hash = read1.content_hash

    // Edit avec ops contenant une chaîne JSON stringifiée
    const stringOp = JSON.stringify({
      insert_after: 1,
      content: '  console.log("hello");'
    })
    const editRes = await runScalpel(ws, 'edit_block', {
      path: 'mod.js::greet',
      expected_hash: hash,
      ops: [stringOp]
    })
    const editStr = typeof editRes === 'string' ? editRes : (editRes?.numbered_text || JSON.stringify(editRes))
    assert.ok(editStr.includes('console.log("hello")'))
  } finally {
    fs.rmSync(ws, { recursive: true, force: true })
  }
})
test('Lot B - Integration reelle: runner.py supporte ops, edits, inserts, paths stringifies et pre-indexe inserts', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-json-strings-'))
  try {
    const fPath = path.join(ws, 'mod.js')
    fs.writeFileSync(fPath, 'export function greet() {\n  return "hi";\n}\n', 'utf-8')

    // paths stringifié
    const readPaths = await runScalpel(ws, 'read_block', {
      paths: JSON.stringify(['mod.js::greet']),
      format: 'json'
    })
    assert.ok(Array.isArray(readPaths.blocks))
    assert.equal(readPaths.blocks[0].status, 'success')

    // inserts stringifié sur un fichier créé hors Scalpel (teste aussi la pré-indexation de inserts dans candidates)
    const docPath = path.join(ws, 'guide.md')
    fs.writeFileSync(docPath, '# Guide\n\nIntro.\n', 'utf-8')
    const insRes = await runScalpel(ws, 'insert_block', {
      inserts: JSON.stringify([
        { parent: 'guide.md::Guide', text: '## SubSection\nContent here.' }
      ])
    })
    assert.equal(insRes.status, 'success')
    assert.equal(insRes.applied, 1)
  } finally {
    fs.rmSync(ws, { recursive: true, force: true })
  }
})
