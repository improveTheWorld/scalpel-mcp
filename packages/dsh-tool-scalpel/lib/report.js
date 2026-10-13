import fs from 'node:fs'
import path from 'node:path'
import { execSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

/**
 * Localise la racine du dépôt Scalpel-clean.
 */
export function getScalpelRepoRoot(startDir = __dirname) {
  let curr = path.resolve(startDir)
  for (let i = 0; i < 6; i++) {
    if (fs.existsSync(path.join(curr, 'scalpel-mcp')) && fs.existsSync(path.join(curr, '.gitignore'))) {
      return curr
    }
    const parent = path.dirname(curr)
    if (parent === curr) break
    curr = parent
  }
  return path.resolve(__dirname, '../../../..')
}

/**
 * Retourne le dossier consolidé de rapports (reports/bilans_scalpel).
 */
export function getDefaultReportsDir(customDir) {
  if (customDir) return path.resolve(customDir)
  if (process.env.SCALPEL_REPORTS_DIR) return path.resolve(process.env.SCALPEL_REPORTS_DIR)
  const repoRoot = getScalpelRepoRoot()
  return path.join(repoRoot, 'reports', 'bilans_scalpel')
}

/**
 * Capte automatiquement l'empreinte de version et le contexte de session au moment du bilan.
 */
export function captureEnvironmentFacts(options = {}) {
  const repoRoot = options.repoRoot || getScalpelRepoRoot()

  // 1. Version du preset / package dsh-tool-scalpel
  let presetVersion = 'unknown'
  try {
    const pkgPath = path.join(__dirname, '../package.json')
    if (fs.existsSync(pkgPath)) {
      const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'))
      presetVersion = pkg.version || 'unknown'
    }
  } catch {}

  // 2. Commit git et état sale
  let gitCommit = 'unknown'
  let gitDirty = false
  try {
    const stdout = execSync('git rev-parse --short HEAD', { cwd: repoRoot, stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()
    if (stdout) gitCommit = stdout
    const status = execSync('git status --porcelain', { cwd: repoRoot, stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()
    gitDirty = status.length > 0
  } catch {}

  // 3. Version DSH
  let dshVersion = process.env.DSH_VERSION || 'unknown'
  if (dshVersion === 'unknown') {
    try {
      const dshPkgPath = path.join(process.env.APPDATA || '', 'npm/node_modules/@deepseek-ai/dsh/package.json')
      if (fs.existsSync(dshPkgPath)) {
        const pkg = JSON.parse(fs.readFileSync(dshPkgPath, 'utf8'))
        dshVersion = pkg.version || 'unknown'
      }
    } catch {}
  }

  // 4. Session & exécution
  const exec = options.exec || {}
  const session = exec.agent?.session || options.session || {}
  const sessionId = session.id || options.sessionId || 'standalone'
  const sessionTitle = session.header?.title || options.sessionTitle || null
  const model = session.header?.model || session.model || options.model || 'unknown'
  const workspace = session.header?.cwd || options.workspace || process.cwd()

  return {
    timestamp: new Date().toISOString(),
    preset_version: presetVersion,
    preset_id: options.presetId || 'scalpel',
    git_commit: gitCommit,
    git_dirty: gitDirty,
    dsh_version: dshVersion,
    model,
    session_id: sessionId,
    session_title: sessionTitle,
    workspace,
    platform: process.platform,
    node_version: process.version
  }
}

/**
 * Formate le rapport en Markdown lisible et structuré.
 */
export function formatMarkdownReport(report) {
  const env = report.environment || {}
  const dateStr = env.timestamp ? env.timestamp.replace('T', ' ').replace(/\..+/, '') : new Date().toISOString()
  const verdictEmoji = {
    success: '✅ SUCCÈS',
    partial: '⚠️ PARTIEL',
    blocked: '🛑 BLOQUÉ',
    failed: '❌ ÉCHEC'
  }[report.verdict] || report.verdict.toUpperCase()

  const lines = [
    `# Bilan Scalpel — ${verdictEmoji}`,
    '',
    `> Rapport consolidé généré le **${dateStr}** pour la session \`${env.session_id || 'N/A'}\`.`,
    '',
    '## 1. Empreinte technique & Environnement',
    '',
    '| Paramètre | Valeur |',
    '| :--- | :--- |',
    `| **Preset Version** | \`${env.preset_version || 'unknown'}\` (preset: \`${env.preset_id || 'scalpel'}\`) |`,
    `| **Scalpel Git Commit** | \`${env.git_commit || 'unknown'}\`${env.git_dirty ? ' *(modifié / dirty)*' : ''} |`,
    `| **DSH Version** | \`${env.dsh_version || 'unknown'}\` |`,
    `| **Modèle LLM** | \`${env.model || 'unknown'}\` |`,
    `| **Plateforme** | \`${env.platform || 'unknown'}\` (Node ${env.node_version || 'unknown'}) |`,
    `| **Workspace** | \`${env.workspace || 'unknown'}\` |`,
    `| **Session** | \`${env.session_id || 'unknown'}\`${env.session_title ? ` — *${env.session_title}*` : ''} |`,
    '',
    '## 2. Objectif & Résumé',
    '',
    `**Objectif de la tâche :**\n${report.task_objective || 'Non spécifié.'}`,
    '',
    `**Verdict :** **${verdictEmoji}**`,
    '',
    `**Résumé :**\n${report.summary || 'Aucun résumé.'}`,
    ''
  ]

  if (report.tool_call_counts && Object.keys(report.tool_call_counts).length > 0) {
    lines.push('### Statistiques d\'usage des outils Scalpel', '', '| Outil | Nombre d\'appels |', '| :--- | ---: |')
    for (const [tool, count] of Object.entries(report.tool_call_counts)) {
      lines.push(`| \`${tool}\` | ${count} |`)
    }
    lines.push('')
  }

  // 3. Blocages et refus
  lines.push('## 3. Blocages & Refus inattendus', '')
  const issues = report.blockages_and_refusals || []
  if (issues.length === 0) {
    lines.push('*Aucun blocage ni refus inattendu constaté.*', '')
  } else {
    issues.forEach((issue, idx) => {
      lines.push(`### 3.${idx + 1} [${issue.tool || 'Outil'}] ${issue.nature || 'Refus / Anomalie'} — \`${issue.target || 'N/A'}\``, '')
      lines.push(`- **Outil concerné :** \`${issue.tool || 'N/A'}\``)
      lines.push(`- **Cible :** \`${issue.target || 'N/A'}\``)
      lines.push(`- **Nature :** \`${issue.nature || 'refus_inattendu'}\``)
      if (issue.error_message) {
        lines.push(`- **Message / Erreur :**\n  \`\`\`text\n  ${issue.error_message.trim()}\n  \`\`\``)
      }
      if (issue.context_call) {
        const callStr = typeof issue.context_call === 'string' ? issue.context_call : JSON.stringify(issue.context_call, null, 2)
        lines.push(`- **Contexte / Appel :**\n  \`\`\`json\n  ${callStr}\n  \`\`\``)
      }
      if (issue.workaround) {
        lines.push(`- **Contournement / Résolution :** ${issue.workaround}`)
      }
      lines.push('')
    })
  }

  // 4. Données de reproduction
  lines.push('## 4. Données de reproduction', '')
  const repros = report.reproduction_data || []
  if (repros.length === 0) {
    lines.push('*Aucune donnée de reproduction spécifique enregistrée.*', '')
  } else {
    repros.forEach((r, idx) => {
      lines.push(`### 4.${idx + 1} ${r.title || `Cas de reproduction #${idx + 1}`}`, '')
      if (r.target_file) lines.push(`- **Fichier cible :** \`${r.target_file}\``)
      if (r.steps) lines.push(`- **Étapes pour reproduire :**\n  ${r.steps}`)
      if (r.initial_state) {
        lines.push('- **État initial :**', '  ```text', `  ${r.initial_state}`, '  ```')
      }
      if (r.tool_call) {
        const callPayload = typeof r.tool_call === 'string' ? r.tool_call : JSON.stringify(r.tool_call, null, 2)
        lines.push('- **Appel d\'outil exact :**', '  ```json', `  ${callPayload}`, '  ```')
      }
      if (r.expected) lines.push(`- **Comportement attendu :** ${r.expected}`)
      if (r.actual) lines.push(`- **Comportement obtenu :** ${r.actual}`)
      lines.push('')
    })
  }

  // 5. Réussites
  lines.push('## 5. Réussites & Points forts', '')
  const successes = report.successes || []
  if (successes.length === 0) {
    lines.push('*Aucune remarque particulière.*', '')
  } else {
    successes.forEach(s => lines.push(`- ${s}`))
    lines.push('')
  }

  // 6. Recommandations
  lines.push('## 6. Recommandations & Axes d\'amélioration', '')
  if (report.recommendations) {
    if (Array.isArray(report.recommendations)) {
      report.recommendations.forEach(rec => lines.push(`- ${rec}`))
    } else {
      lines.push(report.recommendations)
    }
  } else {
    lines.push('*Aucune recommandation formulée.*')
  }
  lines.push('')

  return lines.join('\n')
}

/**
 * Enregistre le bilan Scalpel dans le dossier unifié reports/bilans_scalpel/.
 */
export function saveScalpelReport(inputData, options = {}) {
  if (!inputData || typeof inputData !== 'object') {
    throw new Error('Les données du bilan doivent être un objet.')
  }
  if (!inputData.task_objective || typeof inputData.task_objective !== 'string') {
    throw new Error("Le champ 'task_objective' est obligatoire.")
  }
  if (!inputData.verdict || typeof inputData.verdict !== 'string') {
    throw new Error("Le champ 'verdict' est obligatoire ('success', 'partial', 'blocked', 'failed').")
  }
  const allowedVerdicts = ['success', 'partial', 'blocked', 'failed']
  const normalizedVerdict = inputData.verdict.toLowerCase().trim()
  if (!allowedVerdicts.includes(normalizedVerdict)) {
    throw new Error(`Verdict inconnu: "${inputData.verdict}". Valeurs autorisées: ${allowedVerdicts.join(', ')}`)
  }
  if (!inputData.summary || typeof inputData.summary !== 'string') {
    throw new Error("Le champ 'summary' est obligatoire.")
  }

  const reportsDir = getDefaultReportsDir(options.reportsDir)
  fs.mkdirSync(reportsDir, { recursive: true })

  const envFacts = captureEnvironmentFacts(options)
  const reportPayload = {
    ...inputData,
    verdict: normalizedVerdict,
    environment: {
      ...envFacts,
      ...(inputData.environment || {})
    }
  }

  // Construction du nom de fichier canonique : YYYY-MM-DD_HH-mm-ss_<session_id>_<verdict>
  const now = new Date()
  const datePart = now.toISOString().replace(/[:.]/g, '-').slice(0, 19)
  const rawSession = String(reportPayload.environment.session_id || 'standalone').slice(0, 12).replace(/[^a-zA-Z0-9_-]/g, '_')
  const baseFilename = `${datePart}_${rawSession}_${normalizedVerdict}`

  const jsonFilename = `${baseFilename}.json`
  const mdFilename = `${baseFilename}.md`

  const jsonPath = path.join(reportsDir, jsonFilename)
  const mdPath = path.join(reportsDir, mdFilename)

  const mdContent = formatMarkdownReport(reportPayload)

  fs.writeFileSync(jsonPath, JSON.stringify(reportPayload, null, 2), 'utf8')
  fs.writeFileSync(mdPath, mdContent, 'utf8')

  return {
    success: true,
    reportsDir,
    baseFilename,
    jsonPath,
    mdPath,
    report: reportPayload
  }
}