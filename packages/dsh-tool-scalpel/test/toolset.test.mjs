import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import {
  CONTRACT_SECTION_NAME,
  SCALPEL_CONTRACT_PATH,
  TOOLSET_KEY,
  buildContractSection,
  defaultModeForPreset,
  loadContract,
  resolveMode,
  toolsetProjectionDefinition as def
} from '../lib/toolset.js'
import { apply } from '../lib/index.js'

const run = (commandId, args, name = 'toolset') => ({ type: 'command/run', data: { commandId, name, args } })
const done = (commandId, kind = 'success') => ({ type: 'command/done', data: { commandId, kind } })
const fold = (events) => events.reduce((s, e) => def.apply(s, e), def.init())

test('projection: un /toolset reussi fixe le mode, l\'etat precedent est remplace', () => {
  assert.equal(fold([run('a', 'scalpel'), done('a')]).mode, 'scalpel')
  assert.equal(fold([run('a', 'scalpel'), done('a'), run('b', 'standard'), done('b')]).mode, 'standard')
})

test('projection: le mode n\'est PAS change avant le command/done, ni par un echec', () => {
  assert.equal(fold([run('a', 'scalpel')]).mode, null)
  assert.equal(fold([run('a', 'scalpel'), done('a', 'error')]).mode, null)
  const s = fold([run('a', 'scalpel'), done('a'), run('b', 'standard'), done('b', 'error')])
  assert.equal(s.mode, 'scalpel')
  assert.equal(s.running, null)
})

test('projection: status, args vides ou inconnus et autres commandes sont sans effet (meme reference)', () => {
  const s0 = def.init()
  for (const ev of [run('a', 'status'), run('a', ''), run('a', undefined), run('a', 'bogus'), run('a', 'scalpel', 'plan'), { type: 'agent/inbox/spliced', data: {} }]) {
    assert.equal(def.apply(s0, ev), s0)
  }
  const running = def.apply(s0, run('a', ' Scalpel '))
  assert.equal(running.running.wanted, 'scalpel')
  assert.equal(def.apply(running, done('zzz')), running, 'un done d\'une autre commande est ignore')
})

test('projection: la vue garde sa reference tant que le mode ne change pas', () => {
  const s1 = fold([run('a', 'scalpel'), done('a')])
  const s2 = fold([run('a', 'scalpel'), done('a'), run('b', 'status')])
  assert.equal(def.wire.view(s1), def.wire.view(s2))
  assert.deepEqual(def.wire.view(def.init()), { mode: null })
  assert.notEqual(def.wire.view(s1), def.wire.view(def.init()))
})

test('projection: les schemas rejettent un etat ou une vue corrompus', () => {
  assert.throws(() => def.stateSchema.parse({ mode: 'bogus', running: null }))
  assert.throws(() => def.stateSchema.parse({ mode: null, running: { commandId: 1, wanted: 'scalpel' } }))
  assert.throws(() => def.stateSchema.parse(null))
  assert.doesNotThrow(() => def.stateSchema.parse(def.init()))
  assert.throws(() => def.wire.viewSchema.parse({ mode: 'x' }))
})

test('mode par defaut : preset Scalpel -> scalpel ; explicite l\'emporte', () => {
  assert.equal(defaultModeForPreset('quorum-ptc-scalpel'), 'scalpel')
  assert.equal(defaultModeForPreset('cordis-scalpel'), 'scalpel')
  assert.equal(defaultModeForPreset('ptc'), 'standard')
  assert.equal(defaultModeForPreset(null), 'standard')
  assert.equal(resolveMode('standard', 'cordis-scalpel'), 'standard')
  assert.equal(resolveMode(null, 'cordis-scalpel'), 'scalpel')
  assert.equal(resolveMode('scalpel', 'ptc'), 'scalpel')
})

test('contrat : le fichier reel est charge en entier et injecte tel quel', () => {
  const c = loadContract()
  assert.equal(c.error, undefined)
  const raw = fs.readFileSync(SCALPEL_CONTRACT_PATH, 'utf8')
  assert.equal(c.text, raw)
  const section = buildContractSection(c)
  assert.ok(section.includes(raw.trim()), 'le contrat complet, pas un resume')
  assert.ok(section.includes('edit_block(path, expected_hash, ops=[...])'))
  assert.ok(/contourner Scalpel par le shell/i.test(section))
})

test('contrat : fichier absent -> pas d\'exception (le prompt de toutes les requetes en depend), repli explicite', () => {
  const c = loadContract('C:\\does\\not\\exist.md')
  assert.ok(c.error)
  assert.equal(c.text, '')
  const section = buildContractSection(c)
  assert.ok(section.includes('illisible'))
  assert.ok(section.includes('expected_hash'))
})

function harness({ explicit = null, preset = null, withRestrict = true } = {}) {
  const sections = []
  const registered = []
  const handlers = {}
  const commands = {}
  let restrictCalls = 0
  let disposeCalls = 0
  const state = { explicit, preset }
  const projections = {
    register: (d) => registered.push(d),
    stateOf: (_s, key) => key === TOOLSET_KEY ? { mode: state.explicit, running: null } : state.preset
  }
  const ctx = {
    tools: { register() {} },
    inject: (_deps, fn) => fn(ctx),
    on: (ev, fn) => { handlers[ev] = fn },
    get: (name) => (name === 'sessionProjections' ? projections : undefined),
    sessionProjections: projections,
    systemPrompt: {
      section: (s) => sections.push(s),
      getSectionOrder: (n) => ({ TOOL_READ: 1100 })[n]
    },
    commands: { register: (c) => { commands[c.name] = c } },
    logger: { warn() {} }
  }
  apply(ctx, { unindexedFallback: 'ask' }, { approval: {}, askUser: async () => ({}) })
  const injected = []
  const agent = (id = 's1') => ({
    session: { id, header: { cwd: 'C:\\ws' } },
    inject: (m) => injected.push(m),
    ctx: withRestrict ? { tools: { restrict: () => { restrictCalls++; return () => { disposeCalls++ } } } } : {}
  })
  return {
    state, sections, registered, handlers, commands, injected, agent,
    counts: () => ({ restrictCalls, disposeCalls })
  }
}

test('apply : projection enregistree, section de contrat au niveau des outils plats, sans interpolation', () => {
  const h = harness()
  assert.equal(h.registered.length, 1)
  assert.equal(h.registered[0].key, TOOLSET_KEY)
  assert.equal(h.sections.length, 1)
  assert.equal(h.sections[0].name, CONTRACT_SECTION_NAME)
  assert.equal(h.sections[0].order, 1100)
  assert.equal(h.sections[0].interpolate, false)
})

test('section : contrat integral uniquement quand la session est en mode Scalpel', () => {
  const text = (h, agent) => h.sections[0].text({ agent })
  const full = fs.readFileSync(SCALPEL_CONTRACT_PATH, 'utf8').trim()

  let h = harness({ explicit: null, preset: 'ptc' })
  assert.equal(text(h, h.agent()), '', 'preset standard sans choix : rien')
  assert.equal(h.sections[0].text({}), '', 'sans agent : rien')

  h = harness({ explicit: 'scalpel', preset: 'ptc' })
  assert.ok(text(h, h.agent()).includes(full), 'choix explicite /toolset scalpel')

  h = harness({ explicit: null, preset: 'quorum-shell-scalpel' })
  assert.ok(text(h, h.agent()).includes(full), 'preset Scalpel sans /toolset : defaut')

  h = harness({ explicit: 'standard', preset: 'cordis-scalpel' })
  assert.equal(text(h, h.agent()), '', 'bascule explicite vers standard retire le contrat')

  h.state.explicit = 'scalpel'
  assert.ok(text(h, h.agent()).includes(full), 'la section suit la projection, sans cache')
})

test('pre-step : restriction reconstruite depuis le journal, idempotente, levee par standard', async () => {
  const h = harness({ explicit: 'scalpel', preset: 'ptc' })
  const a = h.agent('resume-1')
  let nexts = 0
  const next = async () => { nexts++; return 'decision' }
  assert.equal(await h.handlers['agent/pre-step']({ agent: a }, next), 'decision')
  await h.handlers['agent/pre-step']({ agent: a }, next)
  assert.deepEqual(h.counts(), { restrictCalls: 1, disposeCalls: 0 })
  h.state.explicit = 'standard'
  await h.handlers['agent/pre-step']({ agent: a }, next)
  assert.deepEqual(h.counts(), { restrictCalls: 1, disposeCalls: 1 })
  assert.equal(nexts, 3)
})

test('pre-step : une erreur de synchronisation ne bloque jamais l\'etape', async () => {
  const h = harness({ explicit: 'scalpel' })
  const a = h.agent('boom')
  a.ctx.tools.restrict = () => { throw new Error('refuse') }
  assert.equal(await h.handlers['agent/pre-step']({ agent: a }, async () => 'ok'), 'ok')
})

test('/toolset : notice v4 conforme (source.kind), contrat annonce dans le prompt, status lit la projection', async () => {
  const h = harness({ explicit: null, preset: 'ptc' })
  const a = h.agent('cmd-1')
  const res = await h.commands.toolset.handler({ agent: a, rawInput: 'scalpel' })
  assert.equal(res.kind, 'success')
  assert.equal(h.injected.length, 1)
  assert.equal(h.injected[0].source.kind, 'toolset')
  assert.equal(h.injected[0].source.form, 'notice')
  assert.ok(typeof h.injected[0].source.summary === 'string')
  assert.ok(h.injected[0].content[0].text.includes('instructions systeme'))
  assert.equal(h.counts().restrictCalls, 1)

  h.state.explicit = 'scalpel' // ce que la projection reflete apres command/done
  const status = await h.commands.toolset.handler({ agent: h.agent('cmd-2'), rawInput: 'status' })
  assert.ok(status.text.includes('Scalpel'))
  assert.ok(!status.text.includes('defaut du preset'))

  const std = await h.commands.toolset.handler({ agent: a, rawInput: 'standard' })
  assert.equal(std.kind, 'success')
  assert.equal(h.counts().disposeCalls, 1)
  assert.equal(h.injected.at(-1).source.kind, 'toolset')
})

test('/toolset scalpel : un refus de restriction est une erreur (le mode ne doit pas etre journalise comme applique)', async () => {
  const h = harness()
  const a = h.agent('deny')
  a.ctx.tools.restrict = () => { throw new Error('nope') }
  const res = await h.commands.toolset.handler({ agent: a, rawInput: 'scalpel' })
  assert.equal(res.kind, 'error')
  assert.equal(h.injected.length, 0)
})