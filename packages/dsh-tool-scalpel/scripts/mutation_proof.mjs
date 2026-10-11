#!/usr/bin/env node
// Preuve de mutation a trois etats (PASS / FAIL / non declenche, jamais une devinette).
//
// Principe : une assertion n'est prouvee que si le test ECHOUE quand on casse le code qu'elle garde.
//   MUTANT_TUE      le test echoue sous la mutation (la preuve tient)           -> code de sortie 0
//   MUTANT_SURVECU  le test passe quand meme (le test ne prouve rien)           -> code de sortie 1
//   NON_DECLENCHE   preuve indecidable : motif absent ou ambigu, base deja rouge,
//                   ou mutation qui casse le chargement du module (pas une assertion) -> code de sortie 2
//
// Usage : node scripts/mutation_proof.mjs <spec.json> [--root <dossier>]
//   spec.json : { "file": "lib/x.js", "find": "texte exact", "replace": "texte mute", "test": "test/x.test.mjs" }
// Le fichier est modifie sur place puis RESTAURE octet pour octet (verifie par sha1), meme en cas d'echec.
import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

export const VERDICTS = Object.freeze({
  KILLED: 'MUTANT_TUE',
  SURVIVED: 'MUTANT_SURVECU',
  NOT_TRIGGERED: 'NON_DECLENCHE'
})

const EXIT_CODES = { [VERDICTS.KILLED]: 0, [VERDICTS.SURVIVED]: 1, [VERDICTS.NOT_TRIGGERED]: 2 }
// Erreurs de chargement : la mutation a casse le module, pas une assertion. Elles ne prouvent rien.
const LOAD_ERRORS = ['SyntaxError', 'ERR_MODULE_NOT_FOUND', 'ReferenceError', 'ERR_REQUIRE_ESM']

function sha1(buf) {
  return createHash('sha1').update(buf).digest('hex')
}

function countOccurrences(text, needle) {
  if (needle === '') return 0
  let count = 0
  let pos = 0
  while ((pos = text.indexOf(needle, pos)) !== -1) {
    count++
    pos += needle.length
  }
  return count
}

function runTest(root, testPath) {
  const env = { ...process.env }
  // Lance depuis un test node:test, cette variable ferait parler le fils en v8-serialize.
  delete env.NODE_TEST_CONTEXT
  const res = spawnSync(
    process.execPath,
    ['--test', '--experimental-test-isolation=none', '--test-reporter=tap', testPath],
    { cwd: root, env, encoding: 'utf8', timeout: 300000 }
  )
  const output = `${res.stdout || ''}${res.stderr || ''}`
  const failMatch = output.match(/^# fail (\d+)/m)
  return {
    exitCode: res.status,
    failedTests: failMatch ? Number(failMatch[1]) : null,
    output
  }
}

export function runMutation({ root, file, find, replace, test }) {
  const absFile = path.resolve(root, file)
  const original = fs.readFileSync(absFile)
  const hashBefore = sha1(original)
  const text = original.toString('utf8')

  const count = countOccurrences(text, find)
  if (count === 0) {
    return { verdict: VERDICTS.NOT_TRIGGERED, reason: 'motif absent du fichier : rien a muter' }
  }
  if (count > 1) {
    return { verdict: VERDICTS.NOT_TRIGGERED, reason: `motif ambigu : ${count} occurrences, precisez-le` }
  }

  const baseline = runTest(root, test)
  if (baseline.exitCode !== 0) {
    return {
      verdict: VERDICTS.NOT_TRIGGERED,
      reason: 'la base echoue deja sans mutation : la preuve est indecidable',
      head: baseline.output.slice(0, 600)
    }
  }

  let mutated
  try {
    fs.writeFileSync(absFile, text.replace(find, () => replace))
    mutated = runTest(root, test)
  } finally {
    fs.writeFileSync(absFile, original)
    if (sha1(fs.readFileSync(absFile)) !== hashBefore) {
      throw new Error(`RESTAURATION ECHOUEE pour ${absFile} : restaurez-le avec git checkout`)
    }
  }

  if (mutated.exitCode === 0) {
    return { verdict: VERDICTS.SURVIVED, reason: 'le test passe malgre la mutation : il ne prouve rien' }
  }
  const loadError = LOAD_ERRORS.find(name => mutated.output.includes(name))
  if (loadError || !mutated.failedTests) {
    return {
      verdict: VERDICTS.NOT_TRIGGERED,
      reason: `la mutation casse le chargement (${loadError || 'aucun test en echec'}), pas une assertion`,
      head: mutated.output.slice(0, 600)
    }
  }
  return {
    verdict: VERDICTS.KILLED,
    reason: `${mutated.failedTests} test(s) en echec sous la mutation`,
    head: mutated.output.split('\n').filter(line => /^not ok /.test(line)).slice(0, 5).join('\n')
  }
}

function main(argv) {
  const specPath = argv[0]
  const rootIdx = argv.indexOf('--root')
  const root = path.resolve(rootIdx >= 0 ? argv[rootIdx + 1] : path.join(path.dirname(fileURLToPath(import.meta.url)), '..'))
  if (!specPath) {
    console.error('Usage : node scripts/mutation_proof.mjs <spec.json> [--root <dossier>]')
    return 2
  }
  const spec = JSON.parse(fs.readFileSync(path.resolve(specPath), 'utf8').replace(/^\uFEFF/, '')) // PowerShell 5.1 ecrit un BOM
  const result = runMutation({ root, ...spec })
  console.log(`${result.verdict} - ${result.reason}`)
  if (result.head) console.log(result.head)
  return EXIT_CODES[result.verdict]
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv.slice(2))
}