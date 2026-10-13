import { spawn } from 'node:child_process'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
export const PYTHON_PATH = process.env.PYTHON_PATH || 'python'
export const SCALPEL_ROOT = process.env.SCALPEL_ROOT || resolve(__dirname, '../../..')
export const RUNNER_PY = resolve(__dirname, 'runner.py')

// Borne dure d'un appel au pont. Sans elle, un runner bloque (lecture de stdin jamais fermee,
// base verrouillee, interpreteur absent) gelait l'outil ET la session : mesure le 11/10/2026,
// pont ancien (arguments en argv, stdin ouvert) contre runner neuf (lit stdin) = blocage infini.
export const BRIDGE_TIMEOUT_MS = 180000

let daemonProc = null
let daemonBuffer = ''
let daemonStderr = ''
let nextReqId = 1
const pendingRequests = new Map()

let daemonListening = false

function onDaemonStdoutData(chunk) {
  daemonBuffer += chunk.toString('utf8')
  let idx
  while ((idx = daemonBuffer.indexOf('\n')) !== -1) {
    const line = daemonBuffer.slice(0, idx)
    daemonBuffer = daemonBuffer.slice(idx + 1)
    handleDaemonLine(line)
  }
}

function onDaemonStderrData(chunk) {
  daemonStderr += chunk.toString('utf8')
  if (daemonStderr.length > 8192) {
    daemonStderr = daemonStderr.slice(-8192)
  }
}

function setDaemonRefState(proc, shouldRef) {
  if (!proc) return
  try {
    if (shouldRef) {
      if (!daemonListening) {
        daemonListening = true
        proc.stdout?.on('data', onDaemonStdoutData)
        proc.stderr?.on('data', onDaemonStderrData)
        proc.stdout?.resume?.()
        proc.stderr?.resume?.()
      }
      proc.ref?.()
      proc.stdout?.ref?.()
      proc.stderr?.ref?.()
      proc.stdin?.ref?.()
    } else {
      if (daemonListening) {
        daemonListening = false
        proc.stdout?.removeListener('data', onDaemonStdoutData)
        proc.stderr?.removeListener('data', onDaemonStderrData)
        proc.stdout?.pause?.()
        proc.stderr?.pause?.()
      }
      proc.unref?.()
      proc.stdout?.unref?.()
      proc.stderr?.unref?.()
      proc.stdin?.unref?.()
    }
  } catch {}
}

function rejectAllPending(errFactory) {
  const entries = Array.from(pendingRequests.values())
  pendingRequests.clear()
  for (const req of entries) {
    clearTimeout(req.timer)
    try {
      req.reject(errFactory(req))
    } catch {}
  }
}

export function closeDaemon() {
  const proc = daemonProc
  daemonProc = null
  daemonBuffer = ''
  daemonStderr = ''
  if (proc) {
    setDaemonRefState(proc, false)
    try { proc.kill() } catch {}
  }
  rejectAllPending(() => new Error('Scalpel bridge: daemon closed'))
}

function handleDaemonLine(line) {
  if (!line.trim()) return
  let msg
  try {
    msg = JSON.parse(line)
  } catch {
    return
  }
  const req = pendingRequests.get(msg.id)
  if (!req) return
  pendingRequests.delete(msg.id)
  clearTimeout(req.timer)
  if (pendingRequests.size === 0 && daemonProc) {
    setDaemonRefState(daemonProc, false)
  }
  if (msg.ok) {
    const rawOut = typeof msg.result === 'string' ? msg.result : String(msg.result ?? '')
    const winOut = process.platform === 'win32' ? rawOut.replace(/\r?\n/g, '\r\n') : rawOut
    req.resolve(winOut.trim())
  } else {
    req.reject(new Error('Scalpel bridge error: ' + (msg.error || 'Unknown daemon error')))
  }
}

function ensureDaemon() {
  if (daemonProc && !daemonProc.killed && daemonProc.exitCode === null) {
    return daemonProc
  }
  daemonBuffer = ''
  daemonStderr = ''
  daemonListening = false
  const proc = spawn(PYTHON_PATH, [RUNNER_PY, '--daemon'], {
    cwd: SCALPEL_ROOT,
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {
      ...process.env,
      PYTHONPATH: resolve(SCALPEL_ROOT, 'src'),
      PYTHONIOENCODING: 'utf-8'
    }
  })
  daemonProc = proc
  setDaemonRefState(proc, pendingRequests.size > 0)

  proc.on('error', (err) => {
    if (daemonProc === proc) {
      daemonProc = null
      daemonListening = false
    }
    rejectAllPending(() => new Error('Scalpel bridge: echec du processus Python (' + PYTHON_PATH + '): ' + err.message))
  })

  proc.on('close', (code) => {
    const errText = daemonStderr
    if (daemonProc === proc) {
      daemonProc = null
      daemonListening = false
      daemonBuffer = ''
      daemonStderr = ''
    }
    if (pendingRequests.size > 0) {
      rejectAllPending(() => new Error('Scalpel bridge error (code ' + code + '): ' + errText))
    }
  })

  proc.stdin.on('error', () => {})
  return proc
}

function spawnRunner(workspace, cmd, args, timeoutMs = BRIDGE_TIMEOUT_MS) {
  return new Promise((resolvePromise, rejectPromise) => {
    let proc
    try {
      proc = ensureDaemon()
    } catch (err) {
      rejectPromise(new Error('Scalpel bridge: impossible de lancer Python (' + PYTHON_PATH + '): ' + err.message))
      return
    }

    const id = nextReqId++
    const timer = setTimeout(() => {
      pendingRequests.delete(id)
      const currentProc = daemonProc
      daemonProc = null
      daemonBuffer = ''
      daemonStderr = ''
      if (currentProc) {
        setDaemonRefState(currentProc, false)
        try { currentProc.kill() } catch {}
      }
      const timeoutErr = new Error(
        'Scalpel bridge: aucun resultat de "' + cmd + '" apres ' + Math.round(timeoutMs / 1000) +
        ' s ; le processus Python a ete arrete. Rien ne garantit que l\'operation n\'a pas ete appliquee : relisez le bloc avant de recommencer.'
      )
      rejectPromise(timeoutErr)
      rejectAllPending((otherReq) => new Error(
        'Scalpel bridge: aucun resultat de "' + otherReq.cmd + '" suite a l\'arret du processus Python.'
      ))
    }, timeoutMs)

    pendingRequests.set(id, {
      id,
      cmd,
      timer,
      resolve: resolvePromise,
      reject: rejectPromise
    })
    setDaemonRefState(proc, true)

    const payload = JSON.stringify({
      id,
      workspace,
      cmd,
      args: args || {}
    }) + '\n'

    try {
      proc.stdin.write(payload, 'utf8', (err) => {
        if (err && pendingRequests.has(id)) {
          pendingRequests.delete(id)
          clearTimeout(timer)
          if (daemonProc === proc) {
            daemonProc = null
            try { proc.kill() } catch {}
          }
          if (pendingRequests.size === 0 && daemonProc) {
            setDaemonRefState(daemonProc, false)
          }
          rejectPromise(new Error('Scalpel bridge: erreur d\'ecriture stdin: ' + err.message))
        }
      })
    } catch (err) {
      pendingRequests.delete(id)
      clearTimeout(timer)
      if (daemonProc === proc) {
        daemonProc = null
        try { proc.kill() } catch {}
      }
      rejectPromise(new Error('Scalpel bridge: erreur d\'ecriture stdin: ' + err.message))
    }
  })
}

export async function callScalpelTool(workspace, name, args) {
  return await spawnRunner(workspace, name, args)
}

export async function executeScalpel(workspace, cmd, args) {
  const raw = await spawnRunner(workspace, cmd, args)
  try {
    return JSON.parse(raw)
  } catch (err) {
    return raw
  }
}
