#!/usr/bin/env node

/**
 * Script CLI autonome pour consigner un bilan d'évaluation Scalpel consolidé.
 * 
 * Usage :
 *   node record_scalpel_eval.mjs --file rapport.json
 *   node record_scalpel_eval.mjs --stdin < rapport.json
 *   node record_scalpel_eval.mjs --task "Refactoring" --verdict success --summary "Tout s'est bien passé"
 */

import fs from 'node:fs'
import path from 'node:path'
import { saveScalpelReport, getDefaultReportsDir } from '../lib/report.js'

function printHelp() {
  console.log(`
Usage:
  node record_scalpel_eval.mjs [options]

Options:
  --file <path>           Lit les données du rapport depuis un fichier JSON
  --stdin                 Lit les données du rapport depuis l'entrée standard JSON
  --task <texte>          Objectif de la tâche
  --verdict <v>           Verdict: success | partial | blocked | failed
  --summary <texte>       Résumé de l'expérience et du déroulement
  --dir <path>            Dossier cible personnalisé (défaut: reports/bilans_scalpel/)
  --issue <string>        Issue/blocage au format "tool=...;target=...;nature=...;error=..." (répétable)
  --success <texte>       Point fort ou succès (répétable)
  --recommendation <txt>  Recommandation (répétable)
  --help                  Affiche cette aide
`)
}

async function readStdin() {
  return new Promise((resolve, reject) => {
    let data = ''
    process.stdin.setEncoding('utf8')
    process.stdin.on('data', chunk => { data += chunk })
    process.stdin.on('end', () => resolve(data))
    process.stdin.on('error', err => reject(err))
  })
}

function parseKeyValueString(str) {
  const obj = {}
  const parts = str.split(';')
  for (const part of parts) {
    const eqIdx = part.indexOf('=')
    if (eqIdx > 0) {
      const k = part.slice(0, eqIdx).trim()
      const v = part.slice(eqIdx + 1).trim()
      obj[k] = v
    }
  }
  return obj
}

async function main() {
  const args = process.argv.slice(2)
  if (args.includes('--help') || args.includes('-h')) {
    printHelp()
    process.exit(0)
  }

  let reportData = null
  let reportsDir = null

  // Parcours des arguments
  for (let i = 0; i < args.length; i++) {
    const arg = args[i]
    if (arg === '--file' && i + 1 < args.length) {
      const filePath = path.resolve(args[++i])
      reportData = JSON.parse(fs.readFileSync(filePath, 'utf8'))
    } else if (arg === '--stdin') {
      const raw = await readStdin()
      reportData = JSON.parse(raw)
    } else if (arg === '--dir' && i + 1 < args.length) {
      reportsDir = args[++i]
    }
  }

  if (!reportData) {
    // Construction depuis les drapeaux CLI
    reportData = {
      task_objective: '',
      verdict: '',
      summary: '',
      blockages_and_refusals: [],
      reproduction_data: [],
      successes: [],
      recommendations: []
    }

    for (let i = 0; i < args.length; i++) {
      const arg = args[i]
      if (arg === '--task' && i + 1 < args.length) {
        reportData.task_objective = args[++i]
      } else if (arg === '--verdict' && i + 1 < args.length) {
        reportData.verdict = args[++i]
      } else if (arg === '--summary' && i + 1 < args.length) {
        reportData.summary = args[++i]
      } else if (arg === '--issue' && i + 1 < args.length) {
        const issueStr = args[++i]
        const parsed = parseKeyValueString(issueStr)
        reportData.blockages_and_refusals.push({
          tool: parsed.tool || 'unknown',
          target: parsed.target || 'N/A',
          nature: parsed.nature || 'refus_inattendu',
          error_message: parsed.error || parsed.error_message || '',
          workaround: parsed.workaround || ''
        })
      } else if (arg === '--success' && i + 1 < args.length) {
        reportData.successes.push(args[++i])
      } else if (arg === '--recommendation' && i + 1 < args.length) {
        reportData.recommendations.push(args[++i])
      }
    }
  }

  if (!reportData.task_objective || !reportData.verdict || !reportData.summary) {
    console.error('Erreur: --task, --verdict et --summary sont requis (ou fournir un fichier JSON valide via --file ou --stdin).')
    printHelp()
    process.exit(1)
  }

  try {
    const res = saveScalpelReport(reportData, { reportsDir })
    console.log(`[Bilan Scalpel] Rapport consolidé enregistré avec succès !`)
    console.log(`- Dossier : ${res.reportsDir}`)
    console.log(`- JSON    : ${res.jsonPath}`)
    console.log(`- Markdown: ${res.mdPath}`)
    console.log(`- Verdict : ${res.report.verdict}`)
  } catch (err) {
    console.error(`[Bilan Scalpel] Erreur lors de l'enregistrement du bilan:`, err.message)
    process.exit(1)
  }
}

main().catch(err => {
  console.error('[Bilan Scalpel] Erreur inattendue:', err)
  process.exit(1)
})