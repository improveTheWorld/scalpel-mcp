import fs from 'node:fs'
import path from 'node:path'
import zlib from 'node:zlib'

const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])

export function splitFrames(buffer) {
  const frames = []
  let pos = 0
  while (pos < buffer.length) {
    const next = buffer.indexOf(MAGIC, pos + 4)
    const end = next === -1 ? buffer.length : next
    frames.push(buffer.subarray(pos, end))
    pos = end
  }
  return frames
}

export function analyzeJournal(buffer) {
  const frames = splitFrames(buffer)
  let frames_failed = 0

  let model = null
  let preset = null
  let totalToolCalls = 0
  const toolStats = {}
  const callToolMap = new Map()

  const stepsMap = new Map() // stepId -> tool_calls count
  let currentStepId = null
  let stepOrder = 0

  for (const frame of frames) {
    let decompressed
    try {
      decompressed = zlib.zstdDecompressSync(frame)
    } catch {
      frames_failed++
      continue
    }

    if (!decompressed || decompressed.length === 0) {
      frames_failed++
      continue
    }

    const lines = decompressed.toString('utf-8').split('\n')
    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed) continue
      let ev
      try {
        ev = JSON.parse(trimmed)
      } catch {
        continue
      }

      if (ev.type === 'session') {
        if (ev.agentPreset && !preset) preset = ev.agentPreset
        if (ev.model && !model) model = ev.model
      }
      if (ev.type === 'agent-preset/selected') {
        if (ev.data?.agentPreset) preset = ev.data.agentPreset
        else if (ev.data?.presetId) preset = ev.data.presetId
      }
      if (ev.type === 'request/header' && !model) {
        if (ev.data?.header?.config?.model) model = ev.data.header.config.model
        else if (ev.data?.model) model = ev.data.model
      }
      if (ev.type === 'request/context' && ev.data?.model && !model) {
        model = ev.data.model
      }
      if (ev.type === 'assistant/message' && ev.data?.model && !model) {
        model = ev.data.model
      }
      if (ev.type === 'step/start') {
        stepOrder++
        currentStepId = `step_${stepOrder}`
        if (ev.data?.turn !== undefined && ev.data?.step !== undefined) {
          currentStepId = `${ev.data.turn}:${ev.data.step}`
        }
        if (!stepsMap.has(currentStepId)) {
          stepsMap.set(currentStepId, 0)
        }
      }

      if (ev.type === 'tool/call') {
        totalToolCalls++
        let stepKey = currentStepId
        if (ev.data?.turn !== undefined && ev.data?.step !== undefined) {
          stepKey = `${ev.data.turn}:${ev.data.step}`
        }
        if (!stepKey) {
          stepOrder++
          stepKey = `step_${stepOrder}`
        }
        stepsMap.set(stepKey, (stepsMap.get(stepKey) || 0) + 1)

        const name = ev.data?.name || 'unknown'
        const callId = ev.data?.callId || ev.data?.id
        if (callId) callToolMap.set(callId, name)

        if (!toolStats[name]) {
          toolStats[name] = { calls: 0, args_bytes: 0, result_bytes: 0, errors: 0 }
        }
        toolStats[name].calls++
        const argsStr = typeof ev.data?.arguments === 'string'
          ? ev.data.arguments
          : JSON.stringify(ev.data?.args ?? {})
        toolStats[name].args_bytes += Buffer.byteLength(argsStr, 'utf-8')
      }

      if (ev.type === 'tool/result') {
        const callId = ev.data?.message?.toolCallId || ev.data?.message?.source?.callId || ev.data?.callId
        const name = (callId && callToolMap.get(callId)) || ev.data?.name || 'unknown'
        if (!toolStats[name]) {
          toolStats[name] = { calls: 0, args_bytes: 0, result_bytes: 0, errors: 0 }
        }
        let resStr = ''
        if (Array.isArray(ev.data?.message?.content)) {
          for (const item of ev.data.message.content) {
            if (typeof item?.text === 'string') resStr += item.text
          }
        } else if (typeof ev.data?.result === 'string') {
          resStr = ev.data.result
        } else if (ev.data?.result !== undefined) {
          resStr = JSON.stringify(ev.data.result)
        }
        toolStats[name].result_bytes += Buffer.byteLength(resStr, 'utf-8')
        const isErr = ev.data?.message?.isError === true ||
          ev.data?.isError === true ||
          Boolean(ev.data?.error) ||
          resStr.startsWith('[error]')
        if (isErr) {
          toolStats[name].errors++
        }
      }
    }
  }

  // Filtrer les étapes pour ne retenir que celles qui ont eu des appels ou toutes les étapes
  // Si stepsMap a des entrées à 0 (étapes purement de réflexion), on compte les étapes ayant des appels pour calls_per_step
  // ou le total des steps exécutés. Pour le Lot F, attendu : 146 appels, 1,00 appel par étape.
  // 146 appels / 146 étapes = 1.00.
  const stepsWithCalls = Array.from(stepsMap.values()).filter(c => c > 0)
  const stepsCount = stepsWithCalls.length || stepsMap.size || 0

  const histogram = {}
  for (const count of stepsWithCalls) {
    histogram[count] = (histogram[count] || 0) + 1
  }

  const calls_per_step = stepsCount > 0 ? Number((totalToolCalls / stepsCount).toFixed(2)) : 0

  return {
    model,
    preset,
    steps: stepsCount,
    tool_calls: totalToolCalls,
    calls_per_step,
    histogram,
    tools: toolStats,
    frames_failed
  }
}

function main() {
  const args = process.argv.slice(2)
  if (args.length === 0) {
    console.error('Usage: node scripts/journal_stats.mjs <journal.zstd>...')
    process.exit(1)
  }

  const results = []
  for (const file of args) {
    const buf = fs.readFileSync(file)
    const stats = analyzeJournal(buf)
    results.push(stats)
  }

  if (results.length === 1) {
    console.log(JSON.stringify(results[0], null, 2))
  } else {
    console.log(JSON.stringify(results, null, 2))
  }
}

if (process.argv[1] && process.argv[1].endsWith('journal_stats.mjs')) {
  main()
}