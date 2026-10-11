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

function spawnRunner(workspace, cmd, args, timeoutMs = BRIDGE_TIMEOUT_MS) {
  return new Promise((resolvePromise, rejectPromise) => {
    let settled = false
    const finish = (fn, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      fn(value)
    }

    let proc
    try {
      proc = spawn(PYTHON_PATH, [RUNNER_PY, workspace, cmd], {
        cwd: SCALPEL_ROOT,
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
        env: {
          ...process.env,
          PYTHONPATH: resolve(SCALPEL_ROOT, 'src'),
          PYTHONIOENCODING: 'utf-8'
        }
      })
    } catch (err) {
      rejectPromise(new Error('Scalpel bridge: impossible de lancer Python (' + PYTHON_PATH + '): ' + err.message))
      return
    }

    const timer = setTimeout(() => {
      try { proc.kill() } catch {}
      finish(rejectPromise, new Error('Scalpel bridge: aucun resultat de "' + cmd + '" apres ' + Math.round(timeoutMs / 1000) + ' s ; le processus Python a ete arrete. Rien ne garantit que l\'operation n\'a pas ete appliquee : relisez le bloc avant de recommencer.'))
    }, timeoutMs)

    let stdout = ''
    let stderr = ''
    proc.stdout.on('data', (d) => { stdout += d.toString() })
    proc.stderr.on('data', (d) => { stderr += d.toString() })

    // Un echec de lancement (interpreteur introuvable) emet 'error' : sans ce gestionnaire,
    // la promesse ne se reglait jamais.
    proc.on('error', (err) => {
      finish(rejectPromise, new Error('Scalpel bridge: echec du processus Python (' + PYTHON_PATH + '): ' + err.message))
    })

    proc.on('close', (code) => {
      if (code !== 0) {
        return finish(rejectPromise, new Error('Scalpel bridge error (code ' + code + '): ' + (stderr || stdout)))
      }
      finish(resolvePromise, stdout.trim())
    })

    // Un runner qui meurt avant d'avoir lu ses arguments rend EPIPE sur stdin : on le laisse au
    // gestionnaire 'close', qui porte le vrai message.
    proc.stdin.on('error', () => {})
    proc.stdin.end(JSON.stringify(args || {}))
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
