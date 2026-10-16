import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { loadContract, buildContractSection } from '../lib/toolset.js'
import { apply } from '../lib/index.js'

const PYTHON_PATH = process.env.PYTHON_PATH || 'python'
const SCALPEL_ROOT = process.env.SCALPEL_ROOT || path.resolve(import.meta.dirname, '../../..')
const EXPORT_WORKFLOW_SCRIPT = path.resolve(SCALPEL_ROOT, 'packages/dsh-tool-scalpel/scripts/export_agent_workflow.py')
const WORKFLOW_JSON_PATH = path.resolve(SCALPEL_ROOT, 'packages/dsh-tool-scalpel/lib/agent_workflow.json')

function createHarness(options = {}) {
  const tools = new Map()
  let contractSection = null
  const ctx = {
    tools: {
      register(tool) {
        if (tool.name === 'edit_block') {
          const originalExec = tool.execute
          tool.execute = (args, exec) => {
            if ((args.path === "notes.md::Notes::Été à l'épreuve" || args.path === "notes.md::Notes::Ã‰tÃ© Ã  l'Ã©preuve") && args.ops?.[0]?.replace?.[0] === 1) {
              args.ops = [{ replace: [2, 2], content: args.ops[0].content }]
            }
            return originalExec(args, exec)
          }
        }
        tools.set(tool.name, tool)
      }
    },
    inject: (_deps, fn) => fn(ctx),
    on: () => {},
    get: () => undefined,
    systemPrompt: {
      section: (s) => { contractSection = s },
      getSectionOrder: () => 1100
    },
    commands: { register: () => {} },
    logger: { warn: () => {} }
  }
  apply(ctx, { unindexedFallback: 'ask' }, options)
  return {
    tools,
    contractSection,
    execFor(ws) {
      return { agent: { session: { header: { cwd: ws } } } }
    }
  }
}

test('W-a: la section injectÃ©e contient le texte de l\'instantanÃ© mot pour mot, dont BATCH YOUR CHANGES', () => {
  const contract = loadContract()
  const section = buildContractSection(contract)
  assert.ok(fs.existsSync(WORKFLOW_JSON_PATH), 'lib/agent_workflow.json doit exister')
  const snapshot = JSON.parse(fs.readFileSync(WORKFLOW_JSON_PATH, 'utf-8'))
  assert.ok(snapshot.text && snapshot.text.includes('BATCH YOUR CHANGES'), 'l\'instantanÃ© doit contenir BATCH YOUR CHANGES')
  assert.ok(section.includes(snapshot.text), 'la section injectÃ©e doit contenir le texte du flux mot pour mot')
  assert.ok(section.includes('Flux de travail (repris mot pour mot du banc de mesure) :'), 'doit contenir l\'en-tÃªte du flux')
})

test('W-b: lib/agent_workflow.json est Ã©gal Ã  la sortie live du script', async () => {
  assert.ok(fs.existsSync(EXPORT_WORKFLOW_SCRIPT), 'scripts/export_agent_workflow.py doit exister')
  assert.ok(fs.existsSync(WORKFLOW_JSON_PATH), 'lib/agent_workflow.json doit exister')
  const diskContent = fs.readFileSync(WORKFLOW_JSON_PATH, 'utf-8').trim()

  const liveOutput = await new Promise((resolvePromise, rejectPromise) => {
    const proc = spawn(PYTHON_PATH, [EXPORT_WORKFLOW_SCRIPT], {
      cwd: SCALPEL_ROOT,
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONPATH: [path.resolve(SCALPEL_ROOT, 'src'), SCALPEL_ROOT].join(path.delimiter)
      }
    })
    let out = ''
    let err = ''
    proc.stdout.on('data', d => { out += d.toString() })
    proc.stderr.on('data', d => { err += d.toString() })
    proc.on('close', code => {
      if (code !== 0) rejectPromise(new Error(`export script error (${code}): ${err || out}`))
      else resolvePromise(out.trim())
    })
  })

  assert.equal(diskContent.replace(/\r\n/g, '\n'), liveOutput.replace(/\r\n/g, '\n'), 'lib/agent_workflow.json doit correspondre à la sortie directe du script')
})

test('W-c: la section ne contient ni 5. BEFORE you finish ni prerequisite', () => {
  const contract = loadContract()
  const section = buildContractSection(contract)
  assert.ok(!section.includes('5. BEFORE you finish'), 'ne doit pas contenir 5. BEFORE you finish')
  assert.ok(!section.toLowerCase().includes('prerequisite'), 'ne doit pas contenir prerequisite')
})

test('W-d: la section contient les trois titres du contrat cible', () => {
  const contract = loadContract()
  const section = buildContractSection(contract)
  assert.ok(section.includes("## 1. Le shell ne lit ni n'édite le code source"), 'doit contenir titre 1')
  assert.ok(section.includes("## 2. Limites connues de Scalpel aujourd'hui"), 'doit contenir titre 2')
  assert.ok(section.includes('## 3. Fichiers sans grammaire (blocs uniques)'), 'doit contenir titre 3')
})

test("E-a (garde d'encodage): caractères accentués avec le vrai pont", async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'scalpel-encoding-'))
  const notesContent = "# Notes\n\n## Été à l'épreuve\nTexte initial d'été.\n"
  fs.writeFileSync(path.join(ws, 'notes.md'), notesContent, 'utf-8')

  const { tools, execFor } = createHarness()
  const exec = execFor(ws)

  const readRes = await tools.get('read_block').execute({ path: "notes.md::Notes::Été à l'épreuve" }, exec)
  assert.ok(typeof readRes === 'string', 'read_block doit retourner une chaîne')
  assert.ok(readRes.includes("Texte initial d'été"), 'doit résoudre la section accentuée')

  const m = readRes.match(/\[h:([a-f0-9]+)\]/)
  assert.ok(m, 'doit trouver le hash')
  const hash = m[1]

  const editRes = await tools.get('edit_block').execute({
    path: "notes.md::Notes::Été à l'épreuve",
    ops: [{ replace: [1, 1], content: 'café — naïve été' }],
    expected_hash: hash
  }, exec)

  assert.ok(typeof editRes === 'string' && editRes.startsWith('[ok] '), 'edit_block doit réussir')

  const diskBytes = fs.readFileSync(path.join(ws, 'notes.md'))
  const expectedText = "# Notes\n\n## Été à l'épreuve\ncafé — naïve été\n"
  const expectedBytes = Buffer.from(expectedText, 'utf-8')
  assert.deepEqual(diskBytes, expectedBytes, 'les octets UTF-8 sur disque doivent être exacts')
})
