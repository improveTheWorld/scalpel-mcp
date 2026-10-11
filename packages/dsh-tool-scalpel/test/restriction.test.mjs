import test from 'node:test'
import assert from 'node:assert/strict'
import { TOOLSET_KEY } from '../lib/toolset.js'
import { apply } from '../lib/index.js'

// Cas reel du 11/10/2026 : `/toolset scalpel` -> "tools.restrict() names unknown global tools
// "read", "edit", "write", "grep", "glob"". dsh-tools REFUSE de masquer un nom qu'il ne connait
// pas, et un preset qui ne monte pas les outils plats n'en connait aucun. Ce faux service
// reproduit ce contrat : restrict leve pour tout nom hors de `known`, get ne voit que `known`.
function fakeTools(known, { withGet = true } = {}) {
  const calls = []
  const tools = {
    restrict: ({ deny }) => {
      calls.push([...deny])
      const unknown = deny.filter(n => !known.includes(n))
      if (unknown.length) throw new Error(`tools.restrict() names unknown global tools ${unknown.map(n => `"${n}"`).join(', ')}`)
      return () => {}
    }
  }
  if (withGet) tools.get = (name) => (known.includes(name) ? { name } : undefined)
  return { tools, calls }
}

// Les restrictions actives sont gardees au niveau du module, par id de session : chaque test
// doit avoir un id unique, sinon il herite de l'etat du precedent.
let sessionCounter = 0

function harness() {
  const commands = {}
  const projections = { register() {}, stateOf: (_s, key) => (key === TOOLSET_KEY ? { mode: null, running: null } : null) }
  const ctx = {
    tools: { register() {} },
    inject: (_deps, fn) => fn(ctx),
    on() {},
    get: (name) => (name === 'sessionProjections' ? projections : undefined),
    sessionProjections: projections,
    systemPrompt: { section() {}, getSectionOrder: () => 1100 },
    commands: { register: (c) => { commands[c.name] = c } },
    logger: { warn() {} }
  }
  apply(ctx, {}, {})
  const agent = (tools) => ({ session: { id: `restrict-${++sessionCounter}`, header: { cwd: 'C:\\ws' } }, inject() {}, ctx: { tools } })
  return { commands, agent }
}

test('restriction : preset SANS outils plats -> /toolset scalpel reussit sans rien masquer (bug du 11/10)', async () => {
  const h = harness()
  const { tools, calls } = fakeTools([])
  const a = h.agent(tools)
  const res = await h.commands.toolset.handler({ agent: a, rawInput: 'scalpel' })
  assert.equal(res.kind, 'success', res.text)
  assert.deepEqual(calls, [], 'aucun appel a restrict quand aucun outil plat n est visible')
  const back = await h.commands.toolset.handler({ agent: a, rawInput: 'standard' })
  assert.equal(back.kind, 'success', back.text)
})

test('restriction : seuls les outils plats VISIBLES sont masques', async () => {
  const h = harness()
  const { tools, calls } = fakeTools(['read', 'grep'])
  const res = await h.commands.toolset.handler({ agent: h.agent(tools), rawInput: 'scalpel' })
  assert.equal(res.kind, 'success', res.text)
  assert.deepEqual(calls, [['read', 'grep']])
})

test('restriction : les cinq outils plats presents -> les cinq masques', async () => {
  const h = harness()
  const all = ['read', 'edit', 'write', 'grep', 'glob']
  const { tools, calls } = fakeTools(all)
  const res = await h.commands.toolset.handler({ agent: h.agent(tools), rawInput: 'scalpel' })
  assert.equal(res.kind, 'success', res.text)
  assert.deepEqual(calls, [all])
})

test('restriction : ancienne API sans tools.get -> liste complete, comportement inchange', async () => {
  const h = harness()
  const all = ['read', 'edit', 'write', 'grep', 'glob']
  const { tools, calls } = fakeTools(all, { withGet: false })
  const res = await h.commands.toolset.handler({ agent: h.agent(tools), rawInput: 'scalpel' })
  assert.equal(res.kind, 'success', res.text)
  assert.deepEqual(calls, [all])
})