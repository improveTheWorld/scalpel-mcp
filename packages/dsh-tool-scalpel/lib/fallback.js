import fs from 'node:fs'
import * as nodeFsPromises from 'node:fs/promises'
import readline from 'node:readline'
import path from 'node:path'
import { createHash, randomUUID } from 'node:crypto'

export const UNINDEXED_SIGNATURES = [
  'resolves outside the workspace root',
  'matches the workspace ignore pattern',
  'exists on disk but is not indexed',
  'and no parser claims it: the raw text is returned below',
  'is not UTF-8 text',
  'the parser recognised no container'
]

// Refus du repli pour un fichier que Scalpel indexe : le texte nomme la voie qui marche et la limite
// connue (le contenu racine hors symbole n'est pas adressable), au lieu de renvoyer l'agent vers un
// outil qui refuse a son tour.
const INDEXED_REFUSAL =
  "Ce fichier est indexé par Scalpel : traitez ses symboles avec read_block (paths=[...] pour en lire plusieurs) et edit_block. " +
  "Pour ajouter une instruction racine (test(...), export) : edit_block avec insert_after sur la dernière ligne du symbole voisin. " +
  "Le contenu racine hors symbole n'est pas adressable aujourd'hui : signalez cette limite dans votre rapport, sans contourner par le shell."
export function hintForText(text, options = {}) {
  const mode = typeof options.getMode === 'function' ? options.getMode() : (options.mode || 'on')
  const isOff = mode === 'off'
  if (typeof text !== 'string') return text
  let parsed = null
  try {
    parsed = JSON.parse(text)
  } catch {
    return text
  }
  if (!parsed || typeof parsed !== 'object' || parsed.status !== 'error' || typeof parsed.error !== 'string') {
    return text
  }
  let extraLines = []
  if (parsed.error.includes('is a FILE, not a block that can take a child')) {
    const hint = isOff
      ? "insert_block ne crée que l'arbre (fichiers/dossiers) et des paragraphes .md ; pour ajouter du code -> edit_block(path='fichier::symbole', ops=[{insert_after:<dernière ligne>, content}]) (écrit hors du bloc, signalé)."
      : "insert_block ne crée que l'arbre (fichiers/dossiers) et des paragraphes .md ; pour ajouter du code -> edit_block(path='fichier::symbole', ops=[{insert_after:<dernière ligne>, content}]) (écrit hors du bloc, signalé) ; fichier sans bloc adressable -> write_unindexed/edit_unindexed (la lecture est libre ; les mutations suivent le bac à sable de la session)."
    extraLines.push('actionable_hint (DSH): ' + hint)
  }
  if (!isOff && UNINDEXED_SIGNATURES.some(sig => parsed.error.includes(sig))) {
    extraLines.push('repli_hint: Fichier hors couverture Scalpel : read_unindexed / edit_unindexed / write_unindexed / delete_unindexed (la lecture est libre ; les mutations suivent le bac à sable de la session).')
  }
  if (extraLines.length > 0) {
    return text + '\n' + extraLines.join('\n')
  }
  return text
}
import os from 'node:os'
import {
  MODES,
  ESCALATION_FIELDS,
  validateEscalationArgs,
  approveEscalation,
  canonicalPath,
  writableRoots,
  sandboxDenial,
  isPathInside
} from './sandbox.js'



function getCanonicalPath(ws, p) {
  let abs = path.resolve(ws, p)
  let cur = abs
  const trailing = []
  while (true) {
    try {
      const real = fs.realpathSync.native(cur)
      return trailing.length > 0 ? path.join(real, ...trailing) : real
    } catch (e) {
      if (e && e.code !== 'ENOENT') throw e
      const parent = path.dirname(cur)
      if (parent === cur) {
        return abs
      }
      trailing.unshift(path.basename(cur))
      cur = parent
    }
  }
}
function escapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function canonicalizeTarget(ws, p, op) {
  if (!p || typeof p !== 'string' || p.trim().length === 0) {
    throw new Error("L'argument 'path' est obligatoire.")
  }
  let target = p
  const hostPattern = `localhost|127\\.0\\.0\\.1|\\[?::1\\]?|${escapeRegex(os.hostname())}`
  const adminShareRe = new RegExp(`^\\\\\\\\(${hostPattern})\\\\([A-Za-z])\\$(\\\\.*)?$`, 'i')
  const m = target.match(adminShareRe)
  if (m) {
    const driveLetter = m[2]
    const rest = m[3] || '\\'
    const mapped = `${driveLetter}:${rest}`
    try {
      const s1 = fs.statSync(target, { bigint: true })
      const s2 = fs.statSync(mapped, { bigint: true })
      if (s1.dev === s2.dev && s1.ino === s2.ino) {
        target = mapped
      }
    } catch {}
  }
  let canonTarget
  try {
    canonTarget = getCanonicalPath(ws, target)
  } catch (err) {
    if (err && err.code !== 'ENOENT') {
      throw new Error(`cannot ${op} "${p}": not a regular file`)
    }
    canonTarget = path.resolve(ws, target)
  }
  try {
    const st = fs.statSync(canonTarget)
    if (!st.isFile()) {
      throw new Error(`cannot ${op} "${p}": not a regular file`)
    }
  } catch (err) {
    if (err && err.code !== 'ENOENT') {
      throw new Error(`cannot ${op} "${p}": not a regular file`)
    }
  }
  return canonTarget
}



function normalizePath(p) {
  let resolved = path.resolve(p)
  resolved = path.normalize(resolved)
  if (process.platform === 'win32') {
    resolved = resolved.toLowerCase()
  }
  return resolved
}

function resolveMode(getMode) {
  const m = typeof getMode === 'function' ? getMode() : 'on'
  return m === 'off' ? 'off' : 'on'
}

export function safeGet(target, serviceName) {
  if (!target || typeof target.get !== 'function') return undefined
  try {
    return target.get(serviceName)
  } catch {
    return undefined
  }
}



async function streamReadFile(absPath, offset, limit, maxChars = 200000, fsApi = nodeFsPromises) {
  return new Promise((resolve, reject) => {
    const hash = createHash('sha1')
    let isBinary = false
    let isInvalidUtf8 = false
    let totalLines = 0
    const formattedLines = []
    let charCount = 0
    let truncatedByChars = false

    const createStream = fs.createReadStream
    const stream = createStream(absPath)
    const decoder = new TextDecoder('utf-8', { fatal: true })

    let sampleChecked = false
    stream.on('data', (chunk) => {
      hash.update(chunk)
      if (!sampleChecked) {
        const checkLen = Math.min(chunk.length, 8000)
        for (let i = 0; i < checkLen; i++) {
          if (chunk[i] === 0) {
            isBinary = true
            break
          }
        }
        sampleChecked = true
      }
      if (!isInvalidUtf8) {
        try {
          decoder.decode(chunk, { stream: true })
        } catch {
          isInvalidUtf8 = true
        }
      }
    })

    const rl = readline.createInterface({
      input: stream,
      crlfDelay: Infinity
    })

    const startIdx = offset - 1

    rl.on('line', (line) => {
      totalLines++
      if (line.includes('\uFFFD')) {
        isInvalidUtf8 = true
      }
      if (isBinary || isInvalidUtf8) return

      const currentLineNum = totalLines
      if (currentLineNum >= offset && currentLineNum < offset + limit) {
        if (!truncatedByChars) {
          const lineStr = `${currentLineNum}: ${line}\n`
          if (charCount + lineStr.length > maxChars) {
            truncatedByChars = true
          } else {
            formattedLines.push(lineStr)
            charCount += lineStr.length
          }
        }
      }
    })

    rl.on('close', () => {
      if (isBinary || isInvalidUtf8) {
        return reject(new Error(`Le fichier "${absPath}" est binaire ou n'est pas encodé en UTF-8 valide ; impossible de le lire comme texte.`))
      }
      const sha1 = hash.digest('hex')
      const isTruncated = truncatedByChars || (startIdx + formattedLines.length < totalLines)
      let resultText = formattedLines.join('')
      if (isTruncated) {
        resultText += `\n[Note de troncature : affichage de ${formattedLines.length} lignes sur ${totalLines} (${charCount} caractères). Spécifiez offset=${startIdx + formattedLines.length + 1} pour lire la suite.]`
      }

      resolve({
        path: absPath,
        content_hash: sha1,
        total_lines: totalLines,
        lines_returned: formattedLines.length,
        offset,
        limit,
        truncated: isTruncated,
        text: resultText
      })
    })

    stream.on('error', (err) => {
      reject(err)
    })
  })
}

export function createFallback({
  getMode,
  executeScalpel,
  getApproval,
  getSandboxPolicy,
  fsApi = nodeFsPromises,
  now = Date.now
}) {
  const observations = new Map()

  function getSessionId(exec) {
    return exec?.agent?.session?.id
  }

  function observe(sessionId, canonPath, value) {
    if (!sessionId) return
    let sessionMap = observations.get(sessionId)
    if (!sessionMap) {
      sessionMap = new Map()
      observations.set(sessionId, sessionMap)
    }
    sessionMap.set(canonPath, value)
  }

  function getObservation(sessionId, canonPath) {
    if (!sessionId) return undefined
    return observations.get(sessionId)?.get(canonPath)
  }

  function policyFor(exec) {
    if (getSandboxPolicy === undefined) {
      return { mode: 'danger-full-access', workspaceRoot: process.cwd() }
    }
    const svc = typeof getSandboxPolicy === 'function' ? getSandboxPolicy() : undefined
    if (!svc || typeof svc.resolve !== 'function') {
      throw new Error('Politique de bac à sable indisponible : opération refusée.')
    }
    let res
    try {
      res = svc.resolve(exec?.agent?.session ? { session: exec.agent.session } : {})
    } catch {
      throw new Error('Politique de bac à sable indisponible : opération refusée.')
    }
    if (!res || !MODES.includes(res.mode) || typeof res.workspaceRoot !== 'string') {
      throw new Error('Politique de bac à sable indisponible : opération refusée.')
    }
    return res
  }

  function checkSandbox(policy, mode, canonTarget) {
    if (mode === 'danger-full-access') return
    if (mode === 'read-only') throw sandboxDenial(mode)
    if (mode === 'workspace-write') {
      const roots = writableRoots({ mode, workspaceRoot: policy.workspaceRoot })
      const insideAny = roots.some(root => isPathInside(root, canonTarget))
      if (!insideAny) throw sandboxDenial(mode)
      return
    }
    throw sandboxDenial(mode)
  }

  async function checkEligibility(ws, canonTarget, op) {
    const wsCanon = getCanonicalPath(ws, '.')
    const isInside = isPathInside(wsCanon, canonTarget)

    if (isInside) {
      const relCanon = path.relative(wsCanon, canonTarget)
      let probeRes
      try {
        probeRes = await executeScalpel(ws, 'read_block', { path: relCanon, format: 'json' })
      } catch (err) {
        probeRes = { status: 'error', error: err.message || String(err) }
      }

      if (typeof probeRes === 'string' || (probeRes && probeRes.status === 'success' && probeRes.kind !== 'file_text')) {
        throw new Error(INDEXED_REFUSAL)
      }

      if (probeRes && probeRes.status === 'success' && probeRes.kind === 'file_text') {
        return { eligible: true, reason: 'Fichier texte brut non parsé (read-only dans Scalpel)', canonPath: canonTarget }
      }

      if (probeRes && probeRes.status === 'error' && typeof probeRes.error === 'string') {
        if (probeRes.error.includes('resolves outside the workspace root')) {
          throw new Error(`Discordance détectée : le chemin canonique ("${canonTarget}") est à l'intérieur du workspace mais le moteur le refuse comme externe ; opération refusée.`)
        }

        if (UNINDEXED_SIGNATURES.some(sig => probeRes.error.includes(sig))) {
          return { eligible: true, reason: probeRes.error, canonPath: canonTarget }
        }
      }

      if (op === 'read' || op === 'edit' || op === 'delete') {
        throw new Error(`Fichier introuvable ou non éligible au repli hors index : ${probeRes?.error || 'erreur inconnue'}`)
      }

      let currentDir = path.dirname(canonTarget)
      let foundIgnored = false
      let ignoredReason = ''
      while (currentDir.length >= wsCanon.length && isPathInside(wsCanon, currentDir)) {
        const ancestorRel = path.relative(wsCanon, currentDir) || '.'
        try {
          const ancestorRes = await executeScalpel(ws, 'read_block', { path: ancestorRel, format: 'json' })
          if (ancestorRes?.status === 'error' && typeof ancestorRes.error === 'string' && ancestorRes.error.includes('matches the workspace ignore pattern')) {
            foundIgnored = true
            ignoredReason = ancestorRes.error
            break
          }
        } catch {}
        if (currentDir === wsCanon) break
        const parent = path.dirname(currentDir)
        if (parent === currentDir) break
        currentDir = parent
      }

      if (foundIgnored) {
        return { eligible: true, reason: ignoredReason, canonPath: canonTarget }
      }

      throw new Error("Fichier nouveau dans le workspace indexé : utilisez insert_block plutôt que write_unindexed.")
    } else {
      let probeRes
      try {
        probeRes = await executeScalpel(ws, 'read_block', { path: canonTarget, format: 'json' })
      } catch (err) {
        probeRes = { status: 'error', error: err.message || String(err) }
      }

      if (probeRes && probeRes.status === 'error' && typeof probeRes.error === 'string') {
        if (UNINDEXED_SIGNATURES.some(sig => probeRes.error.includes(sig))) {
          return { eligible: true, reason: probeRes.error, canonPath: canonTarget }
        }
      }

      if (op === 'write') {
        return { eligible: true, reason: 'Chemin hors du workspace root', canonPath: canonTarget }
      }

      throw new Error(`Fichier introuvable ou non éligible au repli hors index : ${probeRes?.error || 'erreur inconnue'}`)
    }
  }

  async function read(args, exec, ws) {
    const mode = resolveMode(getMode)
    if (mode === 'off') {
      throw new Error("Repli hors index désactivé (mode 'off').")
    }

    const canonTarget = canonicalizeTarget(ws, args?.path, 'read')
    await checkEligibility(ws, canonTarget, 'read')

    const offset = typeof args.offset === 'number' && args.offset > 0 ? Math.floor(args.offset) : 1
    const limit = typeof args.limit === 'number' && args.limit > 0 ? Math.floor(args.limit) : 2000

    const readResult = await streamReadFile(canonTarget, offset, limit, 200000, fsApi)

    const sid = getSessionId(exec)
    observe(sid, canonTarget, readResult.content_hash)

    return readResult
  }

  async function edit(args, exec, ws) {
    const m = resolveMode(getMode)
    if (m === 'off') {
      throw new Error("Repli hors index désactivé (mode 'off').")
    }

    if (!args || !args.path) {
      throw new Error("L'argument 'path' est obligatoire.")
    }
    if (typeof args.old_string !== 'string' || typeof args.new_string !== 'string') {
      throw new Error("Les arguments 'old_string' et 'new_string' sont obligatoires.")
    }
    if (args.old_string === '') {
      throw new Error("L'argument 'old_string' ne peut pas être vide.")
    }
    validateEscalationArgs(args.sandbox_permissions, args.justification)

    const canonTarget = canonicalizeTarget(ws, args.path, 'edit')
    await checkEligibility(ws, canonTarget, 'edit')

    const policy = policyFor(exec)
    let mode = policy.mode
    if (args.sandbox_permissions !== undefined) {
      mode = await approveEscalation(
        { requestedMode: args.sandbox_permissions, justification: args.justification, effectiveMode: policy.mode, subject: 'operation' },
        { approver: typeof getApproval === 'function' ? getApproval() : undefined, agent: exec?.agent, callId: exec?.callId, toolName: 'edit_unindexed', signal: exec?.signal }
      )
    }

    checkSandbox(policy, mode, canonTarget)

    let originalMode = null
    let buf
    try {
      const st = await fsApi.stat(canonTarget)
      originalMode = st.mode
      buf = await fsApi.readFile(canonTarget)
    } catch (err) {
      if (err && err.code === 'ENOENT') {
        throw new Error(`cannot edit "${args.path}": not found`)
      }
      throw err
    }

    const currentHash = createHash('sha1').update(buf).digest('hex')

    const sid = getSessionId(exec)
    if (getSandboxPolicy !== undefined) {
      const prior = getObservation(sid, canonTarget)
      if (!sid || prior === undefined) {
        throw new Error(`edit requires reading "${args.path}" first`)
      }
      if (prior === 'absent') {
        throw new Error(`cannot edit "${args.path}": not found`)
      }
      if (prior !== currentHash) {
        throw new Error(`cannot edit "${args.path}": the file changed since you read it; read it again`)
      }
    }
    if (args.expected_hash && args.expected_hash !== currentHash) {
      throw new Error(`Conflit de version sur "${canonTarget}" : expected_hash (${args.expected_hash}) diffère du hash actuel (${currentHash}). Aucune écriture effectuée.`)
    }

    const hasBOM = buf.length >= 3 && buf[0] === 0xEF && buf[1] === 0xBB && buf[2] === 0xBF
    let cleanText
    try {
      const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true })
      const text = decoder.decode(buf)
      cleanText = hasBOM ? text.slice(1) : text
    } catch {
      throw new Error(`Le fichier "${canonTarget}" n'est pas un fichier texte UTF-8 valide ; impossible de l'éditer.`)
    }

    let oldStr = args.old_string
    let newStr = args.new_string

    function matchCount(haystack, needle) {
      let count = 0
      let pos = 0
      while (pos < haystack.length) {
        const idx = haystack.indexOf(needle, pos)
        if (idx === -1) break
        count++
        pos = idx + needle.length
      }
      return count
    }

    let targetOld = oldStr
    let targetNew = newStr

    if (matchCount(cleanText, targetOld) === 0) {
      const asCrlf = oldStr.replace(/\r?\n/g, "\r\n")
      const asLf = oldStr.replace(/\r\n/g, "\n")
      if (matchCount(cleanText, asCrlf) > 0) {
        targetOld = asCrlf
        targetNew = newStr.replace(/\r?\n/g, "\r\n")
      } else if (matchCount(cleanText, asLf) > 0) {
        targetOld = asLf
        targetNew = newStr.replace(/\r\n/g, "\n")
      }
    }

    const count = matchCount(cleanText, targetOld)
    if (count === 0) {
      throw new Error("old_string introuvable dans \"" + canonTarget + "\". Aucune écriture effectuée.")
    }
    if (args.expected_count !== undefined) {
      if (typeof args.expected_count !== 'number' || !Number.isInteger(args.expected_count) || args.expected_count < 0) {
        throw new Error("L'argument 'expected_count' doit être un entier positif ou nul.")
      }
      if (count !== args.expected_count) {
        throw new Error(`Nombre d'occurrences inattendu dans "${canonTarget}" : ${count} trouvée(s), ${args.expected_count} attendue(s). Aucune écriture effectuée.`)
      }
    }
    if (!args.replace_all && count > 1) {
      throw new Error("old_string apparaît " + count + " fois dans \"" + canonTarget + "\" alors que replace_all=false. Aucune écriture effectuée.")
    }

    let replacedText
    if (args.replace_all) {
      replacedText = cleanText.split(targetOld).join(targetNew)
    } else {
      replacedText = cleanText.replace(targetOld, targetNew)
    }

    const finalText = hasBOM ? '\uFEFF' + replacedText : replacedText

    const tmpPath = `${canonTarget}.${randomUUID()}.tmp`
    try {
      await fsApi.writeFile(tmpPath, finalText, 'utf-8')
      await fsApi.rename(tmpPath, canonTarget)
    } catch (err) {
      try { await fsApi.unlink(tmpPath) } catch {}
      throw err
    }

    if (originalMode !== null && typeof fsApi.chmod === 'function') {
      try {
        await fsApi.chmod(canonTarget, originalMode)
      } catch {}
    }

    const newHash = createHash('sha1').update(Buffer.from(finalText, 'utf-8')).digest('hex')
    observe(sid, canonTarget, newHash)

    return {
      status: 'success',
      path: canonTarget,
      content_hash: newHash,
      occurrences_replaced: count
    }
  }

  async function write(args, exec, ws) {
    const m = resolveMode(getMode)
    if (m === 'off') {
      throw new Error("Repli hors index désactivé (mode 'off').")
    }

    if (!args || !args.path) {
      throw new Error("L'argument 'path' est obligatoire.")
    }
    if (typeof args.content !== 'string') {
      throw new Error("L'argument 'content' est obligatoire et doit être une chaîne.")
    }
    validateEscalationArgs(args.sandbox_permissions, args.justification)

    const canonTarget = canonicalizeTarget(ws, args.path, 'write')
    await checkEligibility(ws, canonTarget, 'write')

    const policy = policyFor(exec)
    let mode = policy.mode
    if (args.sandbox_permissions !== undefined) {
      mode = await approveEscalation(
        { requestedMode: args.sandbox_permissions, justification: args.justification, effectiveMode: policy.mode, subject: 'operation' },
        { approver: typeof getApproval === 'function' ? getApproval() : undefined, agent: exec?.agent, callId: exec?.callId, toolName: 'write_unindexed', signal: exec?.signal }
      )
    }

    checkSandbox(policy, mode, canonTarget)

    let originalMode = null
    let fileExists = false
    let currentHash = null
    try {
      const st = await fsApi.stat(canonTarget)
      originalMode = st.mode
      const buf = await fsApi.readFile(canonTarget)
      fileExists = true
      currentHash = createHash('sha1').update(buf).digest('hex')
    } catch (err) {}

    const sid = getSessionId(exec)
    if (getSandboxPolicy !== undefined && fileExists) {
      const prior = getObservation(sid, canonTarget)
      if (!sid || prior === undefined || prior === 'absent') {
        throw new Error(`cannot write "${args.path}": it already exists and was not read in this session; read it first`)
      }
      if (prior !== currentHash) {
        throw new Error(`cannot write "${args.path}": the file changed since you read it; read it again`)
      }
    }

    const tmpPath = `${canonTarget}.${randomUUID()}.tmp`
    try {
      await fsApi.mkdir(path.dirname(canonTarget), { recursive: true })
      await fsApi.writeFile(tmpPath, args.content, 'utf-8')
      await fsApi.rename(tmpPath, canonTarget)
    } catch (err) {
      try { await fsApi.unlink(tmpPath) } catch {}
      throw err
    }

    if (originalMode !== null && typeof fsApi.chmod === 'function') {
      try {
        await fsApi.chmod(canonTarget, originalMode)
      } catch {}
    }

    const newHash = createHash('sha1').update(Buffer.from(args.content, 'utf-8')).digest('hex')
    observe(sid, canonTarget, newHash)

    return {
      status: 'success',
      path: canonTarget,
      content_hash: newHash
    }
  }

  async function deleteFile(args, exec, ws) {
    const m = resolveMode(getMode)
    if (m === 'off') {
      throw new Error("Repli hors index désactivé (mode 'off').")
    }

    if (!args || !args.path) {
      throw new Error("L'argument 'path' est obligatoire.")
    }
    validateEscalationArgs(args.sandbox_permissions, args.justification)

    const canonTarget = canonicalizeTarget(ws, args.path, 'delete')
    await checkEligibility(ws, canonTarget, 'delete')

    const policy = policyFor(exec)
    let mode = policy.mode
    if (args.sandbox_permissions !== undefined) {
      mode = await approveEscalation(
        { requestedMode: args.sandbox_permissions, justification: args.justification, effectiveMode: policy.mode, subject: 'operation' },
        { approver: typeof getApproval === 'function' ? getApproval() : undefined, agent: exec?.agent, callId: exec?.callId, toolName: 'delete_unindexed', signal: exec?.signal }
      )
    }

    checkSandbox(policy, mode, canonTarget)

    let st
    try {
      st = await fsApi.stat(canonTarget)
    } catch (err) {
      throw new Error(`Fichier introuvable sur le disque : "${canonTarget}" (${err.message}).`)
    }
    if (st.isDirectory()) {
      throw new Error(`Le chemin "${canonTarget}" est un dossier. Seuls les fichiers individuels peuvent être supprimés avec delete_unindexed.`)
    }

    const buf = await fsApi.readFile(canonTarget)
    const currentHash = createHash('sha1').update(buf).digest('hex')
    const size = buf.length

    if (args.expected_hash && args.expected_hash !== currentHash) {
      throw new Error(`Conflit de version sur "${canonTarget}" : expected_hash (${args.expected_hash}) diffère du hash actuel (${currentHash}). Aucune suppression effectuée.`)
    }

    await fsApi.unlink(canonTarget)

    const sid = getSessionId(exec)
    observe(sid, canonTarget, 'absent')

    return {
      status: 'success',
      path: canonTarget,
      deleted: true,
      bytes: size,
      deleted_hash: currentHash
    }
  }

  function hintFor(res) {
    const isOff = resolveMode(getMode) === 'off'
    if (res && typeof res === 'object' && res.status === 'error' && typeof res.error === 'string') {
      if (res.error.includes('is a FILE, not a block that can take a child')) {
        res.actionable_hint = isOff
          ? "insert_block ne crée que l'arbre (fichiers/dossiers) et des paragraphes .md ; pour ajouter du code -> edit_block(path='fichier::symbole', ops=[{insert_after:<dernière ligne>, content}]) (écrit hors du bloc, signalé)."
          : "insert_block ne crée que l'arbre (fichiers/dossiers) et des paragraphes .md ; pour ajouter du code -> edit_block(path='fichier::symbole', ops=[{insert_after:<dernière ligne>, content}]) (écrit hors du bloc, signalé) ; fichier sans bloc adressable -> write_unindexed/edit_unindexed (la lecture est libre ; les mutations suivent le bac à sable de la session)."
      }
    }
    if (isOff) {
      return res
    }
    if (res && typeof res === 'object' && res.status === 'error' && typeof res.error === 'string') {
      if (UNINDEXED_SIGNATURES.some(sig => res.error.includes(sig))) {
        res.repli_hint = "Fichier hors couverture Scalpel : read_unindexed / edit_unindexed / write_unindexed / delete_unindexed (la lecture est libre ; les mutations suivent le bac à sable de la session)."
      }
    }
    return res
  }

  return {
    read,
    edit,
    write,
    deleteFile,
    hintFor,
    hintForText: (text) => hintForText(text, { getMode })
  }
}
