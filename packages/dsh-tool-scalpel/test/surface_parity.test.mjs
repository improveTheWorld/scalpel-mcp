import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { apply } from '../lib/index.js'

const PYTHON_PATH = process.env.PYTHON_PATH || 'python'
const SCALPEL_ROOT = process.env.SCALPEL_ROOT || path.resolve(import.meta.dirname, '../../..')
const DIRECT_MCP_SCRIPT = path.resolve(SCALPEL_ROOT, 'packages/dsh-tool-scalpel/scripts/direct_mcp_call.py')
const EXPORT_SCHEMAS_SCRIPT = path.resolve(SCALPEL_ROOT, 'packages/dsh-tool-scalpel/scripts/export_agent_schemas.py')

function createWitnessWorkspace() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-parity-'))
  let bigContent = 'export function big() {\n'
  for (let i = 1; i <= 300; i++) {
    bigContent += `  const v${i} = ${i}\n`
  }
  bigContent += '}\n\nexport function small() {\n  return 1\n}\n'
  fs.writeFileSync(path.join(dir, 'big.js'), bigContent)

  const notesContent = '# Notes\n\n## Alpha\nLigne de texte Alpha.\n\n## Beta\nLigne de texte Beta.\n'
  fs.writeFileSync(path.join(dir, 'notes.md'), notesContent)
  return dir
}

function createHarness(options = {}) {
  const tools = new Map()
  const ctx = {
    tools: {
      register(tool) {
        tools.set(tool.name, tool)
      }
    },
    inject: (_deps, fn) => fn(ctx),
    on: () => {},
    get: () => undefined,
    systemPrompt: {
      section: () => {},
      getSectionOrder: () => 1100
    },
    commands: { register: () => {} },
    logger: { warn: () => {} }
  }
  apply(ctx, { unindexedFallback: 'ask' }, options)
  return {
    tools,
    execFor(ws) {
      return { agent: { session: { header: { cwd: ws } } } }
    }
  }
}

test('P-a: rÃ©ponse compacte pour edit_block', async () => {
  const ws = createWitnessWorkspace()
  const { tools, execFor } = createHarness()
  const exec = execFor(ws)

  const readRes = await tools.get('read_block').execute({ path: 'big.js::big' }, exec)
  let hash
  if (typeof readRes === 'object' && readRes.content_hash) {
    hash = readRes.content_hash
  } else if (typeof readRes === 'string') {
    const m = readRes.match(/\[h:([a-f0-9]+)\]/)
    hash = m ? m[1] : 'unknown'
  }

  const editRes = await tools.get('edit_block').execute({
    path: 'big.js::big',
    ops: [{ replace: [2, 2], content: '  const v1 = 999' }],
    expected_hash: hash
  }, exec)

  assert.equal(typeof editRes, 'string', 'doit Ãªtre une chaÃ®ne')
  assert.ok(editRes.startsWith('[ok] '), 'doit commencer par [ok] ')
  assert.ok(editRes.length < 4000, `doit faire moins de 4 000 caractÃ¨res (longueur: ${editRes?.length})`)
  assert.ok(!editRes.includes('numbered_text'), 'ne doit pas contenir numbered_text')
  assert.ok(!editRes.includes('reparsed_calls'), 'ne doit pas contenir reparsed_calls')
})

test('P-b: edit_block avec edits=[...] en lot', async () => {
  const ws = createWitnessWorkspace()
  const { tools, execFor } = createHarness()
  const exec = execFor(ws)

  const r1 = await tools.get('read_block').execute({ path: 'big.js::small' }, exec)
  const r2 = await tools.get('read_block').execute({ path: 'notes.md::Notes::Alpha' }, exec)
  const hashSmall = typeof r1 === 'object' ? r1.content_hash : (r1?.match(/\[h:([a-f0-9]+)\]/) || [])[1]
  const hashAlpha = typeof r2 === 'object' ? r2.content_hash : (r2?.match(/\[h:([a-f0-9]+)\]/) || [])[1]

  await tools.get('edit_block').execute({
    edits: [
      { path: 'big.js::small', ops: [{ replace: [2, 2], content: '  return 2' }], expected_hash: hashSmall },
      { path: 'notes.md::Notes::Alpha', ops: [{ replace: [1, 1], content: 'Texte Alpha modifiÃ© en lot.' }], expected_hash: hashAlpha }
    ]
  }, exec)

  const bigDisk = fs.readFileSync(path.join(ws, 'big.js'), 'utf-8')
  const notesDisk = fs.readFileSync(path.join(ws, 'notes.md'), 'utf-8')
  assert.ok(bigDisk.includes('return 2'), 'big.js doit contenir return 2')
  assert.ok(notesDisk.includes('Texte Alpha modifiÃ© en lot.'), 'notes.md doit contenir la modif lot')
})

test('P-c: search_symbols avec view=implementation', async () => {
  const ws = createWitnessWorkspace()
  const { tools, execFor } = createHarness()
  const exec = execFor(ws)

  await tools.get('read_block').execute({ path: 'big.js' }, exec)
  await tools.get('read_block').execute({ path: 'notes.md' }, exec)

  const res = await tools.get('search_symbols').execute({ query: 'small', view: 'implementation' }, exec)
  const text = typeof res === 'string' ? res : JSON.stringify(res)
  assert.ok(text.includes('return 1'), 'doit contenir return 1')
})

test('P-d: search_documents inline le texte complet de la section', async () => {
  const ws = createWitnessWorkspace()
  const { tools, execFor } = createHarness()
  const exec = execFor(ws)

  await tools.get('read_block').execute({ path: 'big.js' }, exec)
  await tools.get('read_block').execute({ path: 'notes.md' }, exec)

  const res = await tools.get('search_documents').execute({ query: 'Alpha' }, exec)
  const text = typeof res === 'string' ? res : JSON.stringify(res)
  assert.ok(text.includes('Ligne de texte Alpha.'), 'doit contenir le texte inlinÃ© de Alpha sans read_block')
})

test('P-e: gros arguments (> 32 767 caractÃ¨res)', async () => {
  const ws = createWitnessWorkspace()
  const { tools, execFor } = createHarness()
  const exec = execFor(ws)

  const r = await tools.get('read_block').execute({ path: 'big.js::small' }, exec)
  const hashSmall = typeof r === 'object' ? r.content_hash : (r?.match(/\[h:([a-f0-9]+)\]/) || [])[1]

  const bigChunk = 'X'.repeat(40000)
  await tools.get('edit_block').execute({
    path: 'big.js::small',
    ops: [{ replace: [2, 2], content: `  const data = "${bigChunk}"\n  return 1` }],
    expected_hash: hashSmall
  }, exec)

  const bigDisk = fs.readFileSync(path.join(ws, 'big.js'), 'utf-8')
  assert.ok(bigDisk.includes(bigChunk), 'le fichier sur disque doit contenir les 40 000 caractÃ¨res')
})

test('P-f: deux workspaces tÃ©moins diffÃ©rents voient chacun leur fichier', async () => {
  const ws1 = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-parity-ws1-'))
  const ws2 = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-parity-ws2-'))
  fs.writeFileSync(path.join(ws1, 'f1.js'), 'export function f1() { return 111 }\n')
  fs.writeFileSync(path.join(ws2, 'f2.js'), 'export function f2() { return 222 }\n')

  const { tools, execFor } = createHarness()
  const res1 = await tools.get('read_block').execute({ path: 'f1.js' }, execFor(ws1))
  const res2 = await tools.get('read_block').execute({ path: 'f2.js' }, execFor(ws2))

  const text1 = typeof res1 === 'string' ? res1 : JSON.stringify(res1)
  const text2 = typeof res2 === 'string' ? res2 : JSON.stringify(res2)

  assert.ok(text1.includes('f1'), 'ws1 doit voir f1')
  assert.ok(!text1.includes('f2'), 'ws1 ne doit pas voir f2')
  assert.ok(text2.includes('f2'), 'ws2 doit voir f2')
  assert.ok(!text2.includes('f1'), 'ws2 ne doit pas voir f1')
})

test('P-g: mÃªme texte que le banc (server.mcp.call_tool)', async () => {
  const ws = createWitnessWorkspace()
  const { tools, execFor } = createHarness()
  const exec = execFor(ws)

  await tools.get('read_block').execute({ path: 'big.js' }, exec)
  await tools.get('read_block').execute({ path: 'notes.md' }, exec)

  async function directCall(toolName, args) {
    return new Promise((resolvePromise, rejectPromise) => {
      const proc = spawn(PYTHON_PATH, [DIRECT_MCP_SCRIPT, ws, toolName, JSON.stringify(args || {})], {
        cwd: SCALPEL_ROOT,
        windowsHide: true,
        env: {
          ...process.env,
          PYTHONPATH: path.resolve(SCALPEL_ROOT, 'src')
        }
      })
      let out = ''
      let err = ''
      proc.stdout.on('data', d => { out += d.toString() })
      proc.stderr.on('data', d => { err += d.toString() })
      proc.on('close', code => {
        if (code !== 0) rejectPromise(new Error(`Direct call failed (${code}): ${err || out}`))
        else resolvePromise(out)
      })
    })
  }

  const pluginListing = await tools.get('read_block').execute({ path: 'big.js' }, exec)
  const benchListing = await directCall('read_block', { path: 'big.js' })
  assert.equal(pluginListing, benchListing, 'listing big.js doit Ãªtre identique')

  const pluginSmall = await tools.get('read_block').execute({ path: 'big.js::small' }, exec)
  const benchSmall = await directCall('read_block', { path: 'big.js::small' })
  assert.equal(pluginSmall, benchSmall, 'small() doit Ãªtre identique')

  const pluginMap = await tools.get('get_project_map').execute({}, exec)
  const benchMap = await directCall('get_project_map', {})
  assert.equal(pluginMap, benchMap, 'get_project_map doit Ãªtre identique')
})

test('S0: mesure de l\'Ã©cart de sÃ©curitÃ© en mode read-only', { todo: 'Lot S' }, async () => {
  const ws = createWitnessWorkspace()
  const { tools, execFor } = createHarness({
    getSandboxPolicy: () => ({ mode: 'read-only', workspaceRoot: ws })
  })
  const exec = execFor(ws)

  const r = await tools.get('read_block').execute({ path: 'big.js::small' }, exec)
  const hashSmall = typeof r === 'object' ? r.content_hash : (r?.match(/\[h:([a-f0-9]+)\]/) || [])[1]

  const originalDisk = fs.readFileSync(path.join(ws, 'big.js'), 'utf-8')
  let threw = false
  try {
    const res = await tools.get('edit_block').execute({
      path: 'big.js::small',
      ops: [{ replace: [2, 2], content: '  return 99' }],
      expected_hash: hashSmall
    }, exec)
    if (typeof res === 'string' && (res.includes('[error]') || res.includes('denied') || res.includes('refused'))) {
      threw = true
    }
  } catch {
    threw = true
  }

  const diskAfter = fs.readFileSync(path.join(ws, 'big.js'), 'utf-8')
  assert.equal(diskAfter, originalDisk, 'le fichier sur disque doit Ãªtre inchangÃ©')
  assert.ok(threw, 'edit_block doit refuser la modification en mode read-only')
})

test('P-h: schÃ©mas et descriptions des 8 outils conformes Ã  SCALPEL_SCHEMAS', async () => {
  const { tools } = createHarness()
  const liveRef = await new Promise((resolvePromise, rejectPromise) => {
    const proc = spawn(PYTHON_PATH, [EXPORT_SCHEMAS_SCRIPT], {
      cwd: SCALPEL_ROOT,
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONPATH: path.resolve(SCALPEL_ROOT, 'src'),
        PYTHONIOENCODING: 'utf-8'
      }
    })
    let out = ''
    let err = ''
    proc.stdout.on('data', d => { out += d.toString() })
    proc.stderr.on('data', d => { err += d.toString() })
    proc.on('close', code => {
      if (code !== 0) rejectPromise(new Error(`export script error (${code}): ${err || out}`))
      else resolvePromise(JSON.parse(out))
    })
  })

  // (1) agent_schemas.json est Ã©gal Ã  la sortie live du script (instantanÃ© pas pÃ©rimÃ©)
  const snapshotRaw = JSON.parse(
    fs.readFileSync(path.resolve(SCALPEL_ROOT, 'packages/dsh-tool-scalpel/lib/agent_schemas.json'), 'utf-8')
  )
  assert.deepEqual(snapshotRaw, liveRef, 'l instantane agent_schemas.json doit correspondre a la sortie du script live')

  // (2) les outils enregistrÃ©s Ã©galent l'instantanÃ© plus DSH_EXTENSIONS, ni plus ni moins
  const { DSH_EXTENSIONS } = await import('../lib/index.js')
  assert.equal(tools.size, 9, '8 outils universels + scalpel_report')

  for (const refTool of snapshotRaw.tools) {
    const pluginTool = tools.get(refTool.name)
    assert.ok(pluginTool, `outil ${refTool.name} doit Ãªtre enregistrÃ©`)
    assert.equal(pluginTool.name, refTool.name)
    assert.equal(pluginTool.description, refTool.description)

    const expectedParameters = JSON.parse(JSON.stringify(refTool.parameters || {}))
    const ext = DSH_EXTENSIONS[refTool.name]
    if (ext?.properties) {
      expectedParameters.properties = {
        ...expectedParameters.properties,
        ...ext.properties
      }
    }
    assert.deepEqual(pluginTool.parameters, expectedParameters, `parametres de ${refTool.name} doivent etre conformes`)
  }
})
