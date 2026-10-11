import assert from 'node:assert/strict'
import { test } from 'node:test'
import { executeScalpel } from '../lib/bridge.js'

test('Scalpel bridge exécute search_symbols sur dsh-boost', async () => {
  const res = await executeScalpel('C:\\CodeSource\\dsh-boost', 'search_symbols', { query: 'ChannelStore', limit: 1 })
  const text = typeof res === 'string' ? res : JSON.stringify(res)
  assert.match(text, /ChannelStore/)
})

test('Scalpel bridge exécute read_block sur Scalpel-mcp', async () => {
  const res = await executeScalpel('C:\\CodeSource\\Scalpel-mcp', 'read_block', { path: 'pyproject.toml' })
  const text = typeof res === 'string' ? res : JSON.stringify(res)
  assert.match(text, /pyproject\.toml/)
})
