import assert from 'node:assert/strict'
import { test } from 'node:test'
import zlib from 'node:zlib'
import { analyzeJournal } from '../scripts/journal_stats.mjs'

test('J5 - journal_stats sur journal synthétique : comptes exacts, modèle, preset, histogramme', () => {
  const frame1Events = [
    JSON.stringify({ type: 'session', version: 4, agentPreset: 'scalpel', id: 's-test' }),
    JSON.stringify({ type: 'request/header', data: { header: { config: { model: 'deepseek-chat' } } } })
  ].join('\n')

  const frame2Events = [
    JSON.stringify({ type: 'step/start', data: { turn: 1, step: 1 } }),
    JSON.stringify({ type: 'tool/call', data: { turn: 1, step: 1, callId: 'c1', name: 'read_block', arguments: '{"path":"a.js"}' } }),
    JSON.stringify({ type: 'tool/result', data: { turn: 1, step: 1, callId: 'c1', message: { toolCallId: 'c1', content: [{ type: 'text', text: 'export const x = 1' }], isError: false } } })
  ].join('\n')

  const frame3Events = [
    JSON.stringify({ type: 'step/start', data: { turn: 1, step: 2 } }),
    JSON.stringify({ type: 'tool/call', data: { turn: 1, step: 2, callId: 'c2', name: 'edit_block', arguments: '{"path":"a.js","ops":[]}' } }),
    JSON.stringify({ type: 'tool/call', data: { turn: 1, step: 2, callId: 'c3', name: 'read_block', arguments: '{"path":"b.js"}' } }),
    JSON.stringify({ type: 'tool/result', data: { turn: 1, step: 2, callId: 'c2', message: { toolCallId: 'c2', content: [{ type: 'text', text: '[ok] edited' }], isError: false } } }),
    JSON.stringify({ type: 'tool/result', data: { turn: 1, step: 2, callId: 'c3', message: { toolCallId: 'c3', content: [{ type: 'text', text: '[error] not found' }], isError: true } } })
  ].join('\n')

  const c1 = zlib.zstdCompressSync(Buffer.from(frame1Events, 'utf-8'))
  const c2 = zlib.zstdCompressSync(Buffer.from(frame2Events, 'utf-8'))
  const c3 = zlib.zstdCompressSync(Buffer.from(frame3Events, 'utf-8'))

  const syntheticJournal = Buffer.concat([c1, c2, c3])
  const stats = analyzeJournal(syntheticJournal)

  assert.equal(stats.model, 'deepseek-chat')
  assert.equal(stats.preset, 'scalpel')
  assert.equal(stats.steps, 2)
  assert.equal(stats.tool_calls, 3)
  assert.equal(stats.calls_per_step, 1.5)
  assert.deepEqual(stats.histogram, { 1: 1, 2: 1 })
  assert.equal(stats.frames_failed, 0)

  assert.equal(stats.tools.read_block.calls, 2)
  assert.equal(stats.tools.read_block.errors, 1)
  assert.equal(stats.tools.edit_block.calls, 1)
  assert.equal(stats.tools.edit_block.errors, 0)
})

test('J5 - journal_stats : un cadre final tronqué est compté dans frames_failed', () => {
  const validEvents = [
    JSON.stringify({ type: 'session', agentPreset: 'standard' }),
    JSON.stringify({ type: 'step/start', data: { turn: 1, step: 1 } }),
    JSON.stringify({ type: 'tool/call', data: { turn: 1, step: 1, callId: 'c1', name: 'pwsh', arguments: '{"command":"ls"}' } }),
    JSON.stringify({ type: 'tool/result', data: { turn: 1, step: 1, callId: 'c1', message: { content: [{ type: 'text', text: 'file.txt' }] } } })
  ].join('\n')

  const validFrame = zlib.zstdCompressSync(Buffer.from(validEvents, 'utf-8'))
  // Simuler un cadre zstd tronqué ou corrompu commençant par le magic header 28 B5 2F FD
  const truncatedFrame = Buffer.concat([
    Buffer.from([0x28, 0xb5, 0x2f, 0xfd]),
    Buffer.from('corrupted_truncated_data')
  ])

  const journalWithTruncation = Buffer.concat([validFrame, truncatedFrame])
  const stats = analyzeJournal(journalWithTruncation)

  assert.equal(stats.steps, 1)
  assert.equal(stats.tool_calls, 1)
  assert.equal(stats.calls_per_step, 1)
  assert.equal(stats.tools.pwsh.calls, 1)
  assert.equal(stats.frames_failed, 1, 'le cadre tronqué doit être compté dans frames_failed')
})