import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { runMutation, VERDICTS } from '../scripts/mutation_proof.mjs'

// Fixture : un module, un test qui garde `ok`, et une fonction `dead` que aucun test ne garde.
function makeFixture(testSource) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'mutproof-'))
  fs.mkdirSync(path.join(root, 'lib'))
  fs.mkdirSync(path.join(root, 'test'))
  fs.writeFileSync(path.join(root, 'package.json'), '{"type":"module"}')
  fs.writeFileSync(path.join(root, 'lib', 'a.js'), 'export const ok = () => 1\nexport const dead = () => 10\n')
  fs.writeFileSync(path.join(root, 'test', 'a.test.mjs'), testSource)
  return root
}

const GOOD_TEST = `import test from 'node:test'
import assert from 'node:assert/strict'
import { ok } from '../lib/a.js'
test('ok vaut 1', () => { assert.equal(ok(), 1) })
`

function mutate(root, over) {
  return runMutation({ root, file: 'lib/a.js', test: 'test/a.test.mjs', find: 'ok = () => 1', replace: 'ok = () => 2', ...over })
}

test('mutation proof : un test qui garde le code tue le mutant, et le fichier est restaure', () => {
  const root = makeFixture(GOOD_TEST)
  const before = fs.readFileSync(path.join(root, 'lib', 'a.js'))
  const res = mutate(root)
  assert.equal(res.verdict, VERDICTS.KILLED)
  assert.ok(Buffer.compare(before, fs.readFileSync(path.join(root, 'lib', 'a.js'))) === 0, 'fichier restaure octet pour octet')
})

test('mutation proof : un mutant qui survit est declare SURVECU (le test ne prouve rien)', () => {
  const root = makeFixture(GOOD_TEST)
  const res = mutate(root, { find: 'dead = () => 10', replace: 'dead = () => 99' })
  assert.equal(res.verdict, VERDICTS.SURVIVED)
})

test('mutation proof : motif absent ou ambigu -> NON_DECLENCHE, jamais une devinette', () => {
  const root = makeFixture(GOOD_TEST)
  assert.equal(mutate(root, { find: 'introuvable' }).verdict, VERDICTS.NOT_TRIGGERED)
  const ambiguous = mutate(root, { find: '() =>' })
  assert.equal(ambiguous.verdict, VERDICTS.NOT_TRIGGERED)
  assert.match(ambiguous.reason, /ambigu/)
})

test('mutation proof : base deja rouge -> NON_DECLENCHE', () => {
  const root = makeFixture(GOOD_TEST.replace('ok(), 1', 'ok(), 5'))
  const res = mutate(root)
  assert.equal(res.verdict, VERDICTS.NOT_TRIGGERED)
  assert.match(res.reason, /base echoue/)
})

test('mutation proof : une mutation qui casse le chargement (SyntaxError) ne compte PAS comme mutant tue', () => {
  const root = makeFixture(GOOD_TEST)
  const res = mutate(root, { replace: 'ok = => (' })
  assert.equal(res.verdict, VERDICTS.NOT_TRIGGERED)
  assert.match(res.reason, /chargement/)
})