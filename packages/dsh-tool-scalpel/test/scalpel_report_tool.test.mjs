import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { execSync } from 'node:child_process'
import { apply } from '../lib/index.js'

test('1. apply enregistre l\'outil scalpel_report avec schéma complet', () => {
  const registered = new Map()
  const fakeCtx = {
    tools: { register: (t) => registered.set(t.name, t) },
    inject: () => {}
  }

  apply(fakeCtx, {}, {
    callTool: async () => ({ status: 'success' })
  })

  assert.ok(registered.has('scalpel_report'), "L'outil scalpel_report doit être enregistré")
  const tool = registered.get('scalpel_report')
  assert.ok(tool.description.includes('Bilan Scalpel') || tool.description.includes('bilan'))
  assert.deepStrictEqual(tool.parameters.required, ['task_objective', 'verdict', 'summary'])
  assert.ok(tool.parameters.properties.blockages_and_refusals)
  assert.ok(tool.parameters.properties.reproduction_data)
})

test('2. exécution de scalpel_report via tool execution écrit les fichiers et retourne confirmation', async () => {
  const registered = new Map()
  const fakeCtx = {
    tools: { register: (t) => registered.set(t.name, t) },
    inject: () => {}
  }

  apply(fakeCtx, {}, {
    callTool: async () => ({ status: 'success' })
  })

  const tool = registered.get('scalpel_report')
  const tmpReportsDir = path.join(os.tmpdir(), `scalpel_tool_test_${Date.now()}`)
  process.env.SCALPEL_REPORTS_DIR = tmpReportsDir

  try {
    const exec = {
      agent: {
        session: {
          id: 'test_sess_999',
          header: {
            title: 'Session d\'évaluation tool',
            model: 'test-model-dsh',
            cwd: process.cwd()
          }
        }
      }
    }

    const res = await tool.execute({
      task_objective: 'Vérifier l\'enregistrement via tool DSH',
      verdict: 'success',
      summary: 'L\'outil DSH scalpel_report a fonctionné sans accroc.',
      blockages_and_refusals: [],
      reproduction_data: [
        {
          title: 'Cas nominal',
          steps: '1. Appeler scalpel_report',
          expected: 'Fichiers écrits',
          actual: 'Fichiers écrits'
        }
      ],
      successes: ['Intégration directe dans le loop DSH']
    }, exec)

    assert.strictEqual(res.status, 'success')
    assert.strictEqual(res.verdict, 'success')
    assert.strictEqual(res.environment.session_id, 'test_sess_999')
    assert.strictEqual(res.environment.model, 'test-model-dsh')
    assert.ok(fs.existsSync(res.jsonPath), 'Fichier JSON créé')
    assert.ok(fs.existsSync(res.mdPath), 'Fichier Markdown créé')

    const mdContent = fs.readFileSync(res.mdPath, 'utf8')
    assert.ok(mdContent.includes('Vérifier l\'enregistrement via tool DSH'))
    assert.ok(mdContent.includes('test_sess_999'))
  } finally {
    delete process.env.SCALPEL_REPORTS_DIR
    fs.rmSync(tmpReportsDir, { recursive: true, force: true })
  }
})

test('3. script CLI record_scalpel_eval.mjs s\'exécute et génère un rapport', () => {
  const tmpReportsDir = path.join(os.tmpdir(), `scalpel_cli_test_${Date.now()}`)
  const scriptPath = path.resolve(import.meta.dirname, '../scripts/record_scalpel_eval.mjs')

  const cmd = `node "${scriptPath}" --task "Test CLI" --verdict "success" --summary "Execution CLI reussie" --dir "${tmpReportsDir}" --success "Test CLI reussi"`
  const stdout = execSync(cmd, { encoding: 'utf8' })

  assert.ok(stdout.includes('Rapport consolidé enregistré avec succès'))
  assert.ok(fs.existsSync(tmpReportsDir), 'Le dossier cible doit être créé')

  const files = fs.readdirSync(tmpReportsDir)
  const jsonFile = files.find(f => f.endsWith('.json'))
  const mdFile = files.find(f => f.endsWith('.md'))

  assert.ok(jsonFile, 'Un fichier .json doit être présent')
  assert.ok(mdFile, 'Un fichier .md doit être présent')

  const jsonContent = JSON.parse(fs.readFileSync(path.join(tmpReportsDir, jsonFile), 'utf8'))
  assert.strictEqual(jsonContent.task_objective, 'Test CLI')
  assert.strictEqual(jsonContent.verdict, 'success')

  fs.rmSync(tmpReportsDir, { recursive: true, force: true })
})