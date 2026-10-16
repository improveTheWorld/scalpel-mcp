import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import {
  getScalpelRepoRoot,
  getDefaultReportsDir,
  captureEnvironmentFacts,
  formatMarkdownReport,
  saveScalpelReport
} from '../lib/report.js'

test('1. getScalpelRepoRoot localise la racine du dépôt Scalpel-clean', () => {
  const root = getScalpelRepoRoot()
  assert.ok(fs.existsSync(root), 'La racine doit exister')
  assert.ok(fs.existsSync(path.join(root, 'scalpel-mcp')), 'Doit contenir scalpel-mcp')
  assert.ok(fs.existsSync(path.join(root, '.gitignore')), 'Doit contenir .gitignore')
})

test('2. getDefaultReportsDir respecte le dossier par défaut, les options et env', () => {
  const defaultDir = getDefaultReportsDir()
  assert.ok(defaultDir.endsWith(path.join('reports', 'bilans_scalpel')), 'Dossier par défaut doit finir par reports/bilans_scalpel')

  const customDir = path.join(os.tmpdir(), 'custom_reports_test')
  assert.strictEqual(getDefaultReportsDir(customDir), path.resolve(customDir))

  process.env.SCALPEL_REPORTS_DIR = customDir
  try {
    assert.strictEqual(getDefaultReportsDir(), path.resolve(customDir))
  } finally {
    delete process.env.SCALPEL_REPORTS_DIR
  }
})

test('3. captureEnvironmentFacts capture la version du preset, commit git et métadonnées', () => {
  const facts = captureEnvironmentFacts({
    sessionId: 'session_test_123',
    sessionTitle: 'Tâche de test',
    model: 'test-model-42',
    workspace: 'C:\\Test\\Workspace'
  })

  assert.ok(facts.timestamp, 'Doit avoir un timestamp ISO')
  assert.notStrictEqual(facts.preset_version, 'unknown', 'Doit lire la version du package.json (1.0.0)')
  assert.strictEqual(facts.preset_id, 'scalpel')
  assert.strictEqual(facts.session_id, 'session_test_123')
  assert.strictEqual(facts.session_title, 'Tâche de test')
  assert.strictEqual(facts.model, 'test-model-42')
  assert.strictEqual(facts.workspace, 'C:\\Test\\Workspace')
  assert.strictEqual(facts.platform, process.platform)
  assert.ok(typeof facts.git_dirty === 'boolean')
})

test('4. formatMarkdownReport génère un markdown complet avec tables et données de reproduction', () => {
  const reportData = {
    task_objective: 'Refactoring des parseurs',
    verdict: 'partial',
    summary: 'Le gros du refactoring fonctionne mais un conflit de hash est survenu.',
    tool_call_counts: {
      read_block: 12,
      edit_block: 4
    },
    blockages_and_refusals: [
      {
        tool: 'edit_block',
        target: 'src/parser.py',
        nature: 'stale_hash',
        error_message: 'Stale read: the block was modified outside.',
        context_call: { ops: [{ replace: [1, 2], content: 'def foo():' }] },
        workaround: 'Relecture via read_block puis second edit_block'
      }
    ],
    reproduction_data: [
      {
        title: 'Conflit de hash concurrent',
        target_file: 'src/parser.py',
        initial_state: 'def foo(): pass',
        tool_call: { name: 'edit_block', path: 'src/parser.py::foo', expected_hash: '1234abcd' },
        expected: 'Edition appliquée',
        actual: 'Refus Stale read',
        steps: '1. Lire le bloc\n2. Modifier le fichier hors AST\n3. Envoyer edit_block avec ancien hash'
      }
    ],
    successes: [
      'Identification immédiate des symboles avec search_symbols',
      'Édition atomique par ligne'
    ],
    ai_os_experience: 'L\'empilement des capsules et expert_push_forward a permis de travailler sans redécouverte.',
    recommendations: 'Permettre un rafraîchissement automatique du hash lors d\'un stale read sans modification réelle.',
    improvement_wishes: [
      'Ajouter une notification visuelle lors de la promotion Delta_LCA'
    ]
  }

  const md = formatMarkdownReport({
    ...reportData,
    environment: {
      timestamp: '2026-10-12T14:00:00.000Z',
      preset_version: '1.0.0',
      git_commit: '88128bf',
      git_dirty: false,
      model: 'test-model',
      session_id: 'sess_abc',
      workspace: 'C:\\Code'
    }
  })

  assert.ok(md.includes('# Bilan Scalpel — ⚠️ PARTIEL'))
  assert.ok(md.includes('| **Preset Version** | `1.0.0`'))
  assert.ok(md.includes('| **Scalpel Git Commit** | `88128bf`'))
  assert.ok(md.includes('Refactoring des parseurs'))
  assert.ok(md.includes('Stale read: the block was modified outside.'))
  assert.ok(md.includes('Conflit de hash concurrent'))
  assert.ok(md.includes('Identification immédiate des symboles'))
  assert.ok(md.includes('### Expérience d\'utilisation de l\'AI Agentic OS'))
  assert.ok(md.includes('expert_push_forward'))
  assert.ok(md.includes('### Souhaits d\'amélioration (Scalpel & AI Agentic OS)'))
  assert.ok(md.includes('promotion Delta_LCA'))
})

test('5. saveScalpelReport valide les champs obligatoires et écrit .json + .md', () => {
  const tmpDir = path.join(os.tmpdir(), `scalpel_eval_test_${Date.now()}`)

  // Rejet si champs obligatoires manquants
  assert.throws(() => saveScalpelReport({}, { reportsDir: tmpDir }), /task_objective/)
  assert.throws(() => saveScalpelReport({ task_objective: 'test' }, { reportsDir: tmpDir }), /verdict/)
  assert.throws(() => saveScalpelReport({ task_objective: 'test', verdict: 'invalid' }, { reportsDir: tmpDir }), /Verdict inconnu/)
  assert.throws(() => saveScalpelReport({ task_objective: 'test', verdict: 'success' }, { reportsDir: tmpDir }), /summary/)

  const validData = {
    task_objective: 'Validation unitaire du reporting',
    verdict: 'success',
    summary: 'Tous les tests unitaires sont passés sans accroc.',
    successes: ['Création rapide des rapports'],
    reproduction_data: []
  }

  const res = saveScalpelReport(validData, { reportsDir: tmpDir, sessionId: 'test_session_456' })
  assert.strictEqual(res.success, true)
  assert.ok(fs.existsSync(res.jsonPath), 'Fichier JSON doit être créé')
  assert.ok(fs.existsSync(res.mdPath), 'Fichier MD doit être créé')

  // Validation du contenu JSON
  const savedJson = JSON.parse(fs.readFileSync(res.jsonPath, 'utf8'))
  assert.strictEqual(savedJson.task_objective, validData.task_objective)
  assert.strictEqual(savedJson.verdict, 'success')
  assert.strictEqual(savedJson.environment.session_id, 'test_session_456')
  assert.ok(savedJson.environment.preset_version)

  // Validation du contenu MD
  const savedMd = fs.readFileSync(res.mdPath, 'utf8')
  assert.ok(savedMd.includes('# Bilan Scalpel — ✅ SUCCÈS'))
  assert.ok(savedMd.includes('test_session_456'))

  // Nettoyage
  fs.rmSync(tmpDir, { recursive: true, force: true })
})