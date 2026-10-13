import assert from 'node:assert/strict'
import { test } from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { spawn, execFileSync } from 'node:child_process'

const PYTHON_PATH = process.env.PYTHON_PATH || 'python'
const SCALPEL_ROOT = process.env.SCALPEL_ROOT || path.resolve(import.meta.dirname, '../../..')
const RUNNER_PY = path.resolve(import.meta.dirname, '../lib/runner.py')

function runScalpel(workspace, cmd, args, customRunnerPy = RUNNER_PY) {
  return new Promise((resolvePromise, rejectPromise) => {
    const proc = spawn(PYTHON_PATH, [customRunnerPy, workspace, cmd], {
      cwd: SCALPEL_ROOT,
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
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

    proc.stdin.on('error', () => {})
    proc.stdin.end(JSON.stringify(args || {}))
  })
}

test('Lot C - (a) fichier .js ecrit hors Scalpel -> read_block reussit apres rafraichissement', async (t) => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-refresh-a-'))
  t.after(() => {
    fs.rmSync(ws, { recursive: true, force: true })
  })

  const fPath = path.join(ws, 'hello.js')
  fs.writeFileSync(fPath, 'export function hello() { return "world"; }\n', 'utf-8')

  const res = await runScalpel(ws, 'read_block', { path: 'hello.js' })
  const text = typeof res === 'string' ? res : JSON.stringify(res)
  assert.ok(text.includes('hello'), 'read_block doit reussir pour un fichier .js nouvellement cree')
})

test('Lot C - (b) fichier cree par insert_block puis rempli hors Scalpel -> symboles visibles', async (t) => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-refresh-b-'))
  t.after(() => {
    fs.rmSync(ws, { recursive: true, force: true })
  })

  const resInsert = await runScalpel(ws, 'insert_block', {
    parent: '.',
    anchor: null,
    text: 'calc.js\n'
  })
  const insertText = typeof resInsert === 'string' ? resInsert : JSON.stringify(resInsert)
  assert.ok(insertText.includes('calc.js'), 'insert_block doit reussir')

  const fPath = path.join(ws, 'calc.js')
  fs.writeFileSync(fPath, 'export function add(a, b) { return a + b; }\nexport function sub(a, b) { return a - b; }\n', 'utf-8')

  const resRead = await runScalpel(ws, 'read_block', { path: 'calc.js' })
  const text = typeof resRead === 'string' ? resRead : JSON.stringify(resRead)
  assert.ok(text.includes('add'), 'symbole add doit etre visible')
  assert.ok(text.includes('sub'), 'symbole sub doit etre visible')
})

test('Lot C - (c) fichier .env / secret.md ecrit hors Scalpel -> TOUJOURS refuse, base non polluee', async (t) => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-refresh-c-'))
  t.after(() => {
    fs.rmSync(ws, { recursive: true, force: true })
  })

  fs.writeFileSync(path.join(ws, '.env'), 'SECRET_KEY=supersecret\n', 'utf-8')
  fs.writeFileSync(path.join(ws, 'secret.md'), '# Confidentiel\nNe pas lire\n', 'utf-8')

  const resEnv = await runScalpel(ws, 'read_block', { path: '.env' })
  assert.equal(resEnv.status, 'error', '.env doit etre refuse')

  const resSecret = await runScalpel(ws, 'read_block', { path: 'secret.md' })
  assert.equal(resSecret.status, 'error', 'secret.md doit etre refuse')
})

test('Lot C - (d) chemin hors workspace -> refuse sans tentative d indexation', async (t) => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-refresh-d-'))
  t.after(() => {
    try { fs.rmSync(tmpDir, { recursive: true, force: true }) } catch {}
  })

  const ws = path.join(tmpDir, 'ws')
  fs.mkdirSync(ws, { recursive: true })
  const outsideFile = path.join(tmpDir, 'outside.js')
  fs.writeFileSync(outsideFile, 'export const leak = 1\n', 'utf-8')

  const res = await runScalpel(ws, 'read_block', { path: '../outside.js' })
  assert.equal(res.status, 'error')
  assert.match(res.error, /outside the workspace root/)

  const dbPath = path.join(ws, '.agent_graph.db')
  let count = 0
  if (fs.existsSync(dbPath)) {
    const pyCode = 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute("select count(*) from indexed_files where file_path like \'%outside%\'").fetchone()[0])'
    const out = execFileSync(PYTHON_PATH, ['-c', pyCode, dbPath], { encoding: 'utf-8' }).trim()
    count = parseInt(out, 10)
  }
  assert.equal(count, 0, 'La base ne doit contenir aucune ligne dont file_path contient outside')
})
