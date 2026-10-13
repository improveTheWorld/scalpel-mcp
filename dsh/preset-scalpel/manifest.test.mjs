import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const patch = readFileSync(new URL('./cordis.patch.yml', import.meta.url), 'utf8')
const manifest = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'))

test('le bundle est un patch de preset, pas un plugin hôte', () => {
  assert.equal(manifest.dsh.bundle.patch, './cordis.patch.yml')
  assert.equal(patch.includes("id: preset-scalpel"), true)
  assert.equal(patch.includes("id: scalpel"), true)
  assert.match(patch, /^# /)
  assert.match(patch, /^- insert:/m)
})

test('les outils fichiers plats sont absents et Scalpel est dans le preset', () => {
  for (const forbidden of ['@deepseek-ai/dsh-tool-fs', '@deepseek-ai/dsh-tool-fs-search', '@deepseek-ai/dsh-skill-filesystem']) {
    assert.equal(patch.includes(forbidden), false, forbidden)
  }
  const preset = patch.indexOf('id: preset-scalpel')
  const mcp = patch.indexOf("name: '@deepseek-ai/dsh-mcp-client'")
  assert.ok(preset !== -1 && mcp > preset)
  assert.match(patch, /serverName: scalpel/)
  assert.match(patch, /transport: stdio/)
  assert.match(patch, /--workspace/)
  assert.match(patch, /C:\\CodeSource\\dsh-boost/)
  assert.match(patch, /failOnStartupError: true/)
})
