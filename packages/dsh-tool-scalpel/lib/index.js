import { executeScalpel, callScalpelTool } from './bridge.js'
import { randomUUID } from 'node:crypto'
import { createFallback, safeGet } from './fallback.js'

import fs from 'node:fs'
import path from 'node:path'
import {
  CONTRACT_SECTION_NAME,
  TOOLSET_KEY,
  buildContractSection,
  loadContract,
  resolveMode,
  toolsetProjectionDefinition
} from './toolset.js'

function getSessionWorkspace(exec) {
  return exec.agent?.session?.header?.cwd || process.cwd()
}
import { enforceMutationSandbox } from './sandbox.js'
import { ESCALATION_FIELDS } from './sandbox.js'

function textResult(text) {
  const content = typeof text === 'string' ? text : JSON.stringify(text, null, 2)
  return [{ type: 'text', text: content }]
}

export const name = 'tool-scalpel'
export const inject = ['tools']

const FLAT_TOOLS = ['read', 'edit', 'write', 'grep', 'glob']

export const DSH_EXTENSIONS = {
  insert_block: {
    properties: {
      content: {
        type: 'string',
        description: 'Contenu initial du fichier à créer (uniquement lorsque parent est un dossier)'
      },
      ...ESCALATION_FIELDS
    }
  },
  edit_block: {
    properties: {
      ...ESCALATION_FIELDS
    }
  },
  delete_block: {
    properties: {
      ...ESCALATION_FIELDS
    }
  }
}

const agentSchemasRaw = JSON.parse(
  fs.readFileSync(new URL('./agent_schemas.json', import.meta.url), 'utf-8')
)
const AGENT_SCHEMAS_MAP = new Map(agentSchemasRaw.tools.map(t => [t.name, t]))

export const SCALPEL_TOOLS = [
  'read_block', 'edit_block', 'insert_block', 'delete_block',
  'search_symbols', 'search_documents', 'get_project_map', 'build_context'
]

const OUTPUT_SCHEMA = {
  oneOf: [
    { type: 'object' },
    { type: 'string' },
    { type: 'array' },
    { type: 'boolean' },
    { type: 'number' },
    { type: 'null' }
  ]
}

// Maintient les disposeurs de restriction par agent
const activeRestrictions = new Map()

function buildScalpelNotice(workspace) {
  return [
    `[RUNTIME SYSTEM EVENT: MODE SCALPEL ACTIVE POUR CE WORKSPACE]`,
    `Workspace actif : ${workspace}`,
    `Les outils de fichiers plats (read, edit, write, grep, glob) sont DESACTIVES.`,
    `Outils Scalpel actifs : ${SCALPEL_TOOLS.join(', ')}`,
    `Le contrat d'ingenierie complet figure dans tes instructions systeme (section "Mode Scalpel") : il s'applique des ce tour. Le shell ne sert ni a lire ni a editer du code source.`
  ].join('\n')
}

/**
 * Aligne la restriction d'outils plats (en memoire, par agent) sur le mode voulu. Idempotent.
 * @throws si la restriction est refusee par le service d'outils
 */
function syncRestriction(agent, wantScalpel) {
  const key = agent?.session?.id || 'default'
  const has = activeRestrictions.has(key)
  if (wantScalpel === has) return
  if (has) {
    activeRestrictions.get(key)()
    activeRestrictions.delete(key)
    return
  }
  const tools = agent?.ctx?.tools
  if (!tools?.restrict) return
  // tools.restrict() REFUSE un nom qu'il ne connait pas (dsh-tools : "names unknown global
  // tools"). Un preset qui ne monte pas les outils plats n'a rien a masquer : on ne masque que
  // ceux que l'agent VOIT. Sans tools.get (ancienne API), on garde la liste complete.
  const present = typeof tools.get === 'function'
    ? FLAT_TOOLS.filter(name => tools.get(name, agent) !== undefined)
    : FLAT_TOOLS
  // Aucun outil plat visible : le mode Scalpel est actif, il n'y a simplement rien a retirer.
  activeRestrictions.set(key, present.length > 0 ? tools.restrict({ deny: present }) : () => {})
}

export function apply(ctx, config, options = {}) {
  const scalpelExec = options.executeScalpel || executeScalpel
  const callTool = options.callTool || options.executeScalpel || callScalpelTool
  const fallback = createFallback({
    getMode: () => (config?.unindexedFallback === 'off' ? 'off' : 'on'),
    executeScalpel: scalpelExec,
    getApproval: options.getApproval || (() => safeGet(ctx, 'approval')),
    getSandboxPolicy: options.getSandboxPolicy || (() => safeGet(ctx, 'sandboxPolicy')),
    fsApi: options.fsApi,
    now: options.now
  })

  // 0. Etat du jeu d'outils : projection de session repliee depuis le journal
  //    (source unique pour l'UI, le prompt systeme et la commande /status).
  ctx.inject(['sessionProjections'], (projCtx) => {
    // Enregistrement optionnel : sans registre de projections, le mode retombe sur le defaut du preset.
    projCtx.sessionProjections?.register?.(toolsetProjectionDefinition)

    // A la reprise d'une session (process neuf), la restriction en memoire est perdue :
    // on la reconstruit depuis le journal avant chaque etape.
    projCtx.on?.('agent/pre-step', async ({ agent }, next) => {
      try {
        const { explicit } = readModes(agent?.session)
        if (explicit === 'scalpel') syncRestriction(agent, true)
        else if (explicit === 'standard') syncRestriction(agent, false)
      } catch (err) {
        ctx.logger?.warn?.('[toolset] resynchronisation des outils echouee: %o', err)
      }
      return next()
    })
  })

  function readModes(session) {
    const projections = safeGet(ctx, 'sessionProjections')
    let explicit = null
    let preset = null
    try { explicit = projections?.stateOf(session, TOOLSET_KEY)?.mode ?? null } catch (err) {
      ctx.logger?.warn?.('[toolset] projection toolset illisible: %o', err)
    }
    try { preset = projections?.stateOf(session, 'agentPreset') ?? null } catch (err) {
      ctx.logger?.warn?.('[toolset] projection agentPreset illisible: %o', err)
    }
    return { explicit, preset, mode: resolveMode(explicit, preset) }
  }

  // Contrat complet dans le prompt systeme, au niveau des contrats des outils plats (TOOL_READ),
  // tant que la session est en mode Scalpel (choix explicite ou preset Scalpel).
  ctx.inject(['systemPrompt'], (promptCtx) => {
    const systemPrompt = promptCtx.systemPrompt
    if (typeof systemPrompt?.section !== 'function') return
    systemPrompt.section({
      name: CONTRACT_SECTION_NAME,
      order: systemPrompt.getSectionOrder('TOOL_READ'),
      interpolate: false,
      text: (assembleCtx) => {
        const session = assembleCtx?.agent?.session
        if (!session) return ''
        if (readModes(session).mode !== 'scalpel') return ''
        const contract = loadContract()
        if (contract.error) ctx.logger?.warn?.('[toolset] contrat illisible: %s', contract.error)
        return buildContractSection(contract)
      }
    })
  })

    function isWholeFileOrUnresolvedError(resOrErr) {
      if (!resOrErr) return false
      const msg = typeof resOrErr === 'string'
        ? resOrErr
        : (resOrErr.error || resOrErr.message || JSON.stringify(resOrErr))
      if (typeof msg !== 'string') return false
      const lower = msg.toLowerCase()
      return (
        lower.includes('whole code file') ||
        lower.includes('whole file') ||
        lower.includes('nothing inside it is a block') ||
        lower.includes('recognised no container') ||
        lower.includes('unresolved') ||
        lower.includes('no symbol or markdown section has this identifier') ||
        lower.includes('outside the workspace root')
      )
    }
  // 1. Enregistrement des 8 outils Scalpel universels depuis agent_schemas.json
  for (const name of SCALPEL_TOOLS) {
    const baseSchema = AGENT_SCHEMAS_MAP.get(name)
    if (!baseSchema) continue

    const parameters = JSON.parse(JSON.stringify(baseSchema.parameters || {}))
    const ext = DSH_EXTENSIONS[name]
    if (ext?.properties) {
      parameters.properties = {
        ...parameters.properties,
        ...ext.properties
      }
    }

    ctx.tools.register({
      name: baseSchema.name,
      description: baseSchema.description,
      parameters,
      output: {
        schema: OUTPUT_SCHEMA,
        render: (_args, value) => textResult(value)
      },
      async execute(args, exec) {
        const ws = getSessionWorkspace(exec)

        if (name === 'edit_block' || name === 'insert_block' || name === 'delete_block') {
          const targets = []
          if (name === 'edit_block') {
            if (Array.isArray(args?.edits)) {
              for (const entry of args.edits) {
                const p = entry?.path || args?.path
                if (typeof p === 'string' && p.trim()) {
                  targets.push(p.split('::')[0].split('#')[0])
                }
              }
              if (targets.length === 0 && typeof args?.path === 'string' && args.path.trim()) {
                targets.push(args.path.split('::')[0].split('#')[0])
              }
            } else if (typeof args?.path === 'string' && args.path.trim()) {
              targets.push(args.path.split('::')[0].split('#')[0])
            }
          } else if (name === 'insert_block') {
            if (args?.content !== undefined && args?.content !== null) {
              targets.push(path.resolve(ws, args.parent || '.', args.text || ''))
            } else if (typeof args?.parent === 'string' && args.parent.trim()) {
              targets.push(args.parent.split('::')[0].split('#')[0])
            }
          } else if (name === 'delete_block') {
            if (typeof args?.path === 'string' && args.path.trim()) {
              targets.push(args.path.split('::')[0].split('#')[0])
            }
          }

          await enforceMutationSandbox({
            ws,
            targets,
            args,
            exec,
            getSandboxPolicy: options.getSandboxPolicy || (() => safeGet(ctx, 'sandboxPolicy')),
            getApproval: options.getApproval || (() => safeGet(ctx, 'approval')),
            toolName: name
          })
        }

        const { sandbox_permissions, justification, ...scalpelArgs } = args || {}

        if (name === 'insert_block') {
          const { parent, anchor, text, content } = scalpelArgs
          if (content !== undefined && content !== null) {
            if (typeof content !== 'string') {
              throw new Error("L'argument 'content' doit être une chaîne de caractères.")
            }
            if (typeof text !== 'string' || text.includes('\n') || text.includes('\r')) {
              throw new Error("Lorsque 'content' est fourni, 'text' doit être le nom du fichier seul (sans saut de ligne).")
            }
            const resolvedParent = path.resolve(ws, parent || '.')
            let isDir = false
            try {
              const st = fs.statSync(resolvedParent)
              isDir = st.isDirectory()
            } catch {
              isDir = false
            }
            if (!isDir) {
              throw new Error(`Lorsque 'content' est fourni, 'parent' doit être un dossier existant ("${parent}" n'est pas un dossier).`)
            }
            const payloadText = `${text}\n${content}`
            const res = await callTool(ws, 'insert_block', { parent, anchor, text: payloadText })
            return fallback.hintForText(res)
          }
          const res = await callTool(ws, 'insert_block', scalpelArgs)
          return fallback.hintForText(res)
        }

        if (name === 'edit_block') {
          let res
          let callToolFailed = false
          let caughtError = null
          try {
            res = await callTool(ws, 'edit_block', scalpelArgs)
            if (isWholeFileOrUnresolvedError(res)) {
              callToolFailed = true
            }
          } catch (err) {
            caughtError = err
            if (isWholeFileOrUnresolvedError(err)) {
              callToolFailed = true
            } else {
              throw err
            }
          }

          if (callToolFailed && typeof scalpelArgs?.path === 'string') {
            const rawTarget = scalpelArgs.path.split('::')[0].split('#')[0]
            const absPath = path.isAbsolute(rawTarget) ? rawTarget : path.resolve(ws, rawTarget)
            let isFile = false
            try {
              isFile = fs.statSync(absPath).isFile()
            } catch {}
            if (isFile) {
              const fb = options.fileBlock || (await import('./file_block.js'))
              return await fb.editFileBlock({
                absPath,
                ops: scalpelArgs.ops,
                expectedHash: scalpelArgs.expected_hash
              })
            }
          }

          if (caughtError) throw caughtError
          return fallback.hintForText(res)
        }

        if (name === 'delete_block') {
          let res
          let callToolFailed = false
          let caughtError = null
          try {
            res = await callTool(ws, 'delete_block', scalpelArgs)
            if (isWholeFileOrUnresolvedError(res)) {
              callToolFailed = true
            }
          } catch (err) {
            caughtError = err
            if (isWholeFileOrUnresolvedError(err)) {
              callToolFailed = true
            } else {
              throw err
            }
          }

          if (callToolFailed && typeof scalpelArgs?.path === 'string') {
            const rawTarget = scalpelArgs.path.split('::')[0].split('#')[0]
            const absPath = path.isAbsolute(rawTarget) ? rawTarget : path.resolve(ws, rawTarget)
            let isFile = false
            try {
              isFile = fs.statSync(absPath).isFile()
            } catch {}
            if (isFile) {
              const fb = options.fileBlock || (await import('./file_block.js'))
              return await fb.deleteFileBlock({
                absPath,
                expectedHeading: scalpelArgs.expected_heading
              })
            }
          }

          if (caughtError) throw caughtError
          return fallback.hintForText(res)
        }

        if (name === 'read_block') {
          let res
          let outsideRoot = false
          let caughtError = null
          try {
            res = await callTool(ws, 'read_block', scalpelArgs)
            const msg = typeof res === 'string' ? res : (res?.error || '')
            if (msg.includes('outside the workspace root') || msg.includes('resolves outside the workspace root')) {
              outsideRoot = true
            }
          } catch (err) {
            caughtError = err
            const msg = err.message || String(err)
            if (msg.includes('outside the workspace root') || msg.includes('resolves outside the workspace root')) {
              outsideRoot = true
            } else {
              throw err
            }
          }

          if (outsideRoot && typeof scalpelArgs?.path === 'string') {
            const absPath = path.resolve(ws, scalpelArgs.path)
            let isFile = false
            try {
              isFile = fs.statSync(absPath).isFile()
            } catch {}
            if (isFile) {
              const cryptoMod = await import('node:crypto')
              const buf = fs.readFileSync(absPath)
              const hash = cryptoMod.createHash('sha256').update(buf).digest('hex').slice(0, 8)
              const hasBOM = buf.length >= 3 && buf[0] === 0xEF && buf[1] === 0xBB && buf[2] === 0xBF
              const text = hasBOM ? buf.subarray(3).toString('utf8') : buf.toString('utf8')
              let raw = text
              if (raw.endsWith('\r\n')) raw = raw.slice(0, -2)
              else if (raw.endsWith('\n') || raw.endsWith('\r')) raw = raw.slice(0, -1)
              const lines = raw === '' ? [] : raw.split(/\r?\n/)
              const width = String(Math.max(1, lines.length)).length
              const numbered = lines.map((l, i) => `${String(i + 1).padStart(width, ' ')} | ${l}`).join('\n')
              const header = `${absPath} [file_text] [h:${hash}] (${lines.length} lines)`
              if (scalpelArgs.format === 'json') {
                return {
                  status: 'success',
                  id: absPath,
                  kind: 'file_text',
                  content_hash: hash,
                  line_count: lines.length,
                  numbered_text: `${header}\n${numbered}`
                }
              }
              return `${header}\n${numbered}`
            }
          }

          if (caughtError) throw caughtError
          return fallback.hintForText(res)
        }

        return await callTool(ws, name, scalpelArgs)
      }
    })
  }

  // 1b. Enregistrement de l'outil d'évaluation unifié scalpel_report
  ctx.tools.register({
    name: 'scalpel_report',
    description: "Enregistre un bilan d'évaluation consolidé de l'outil Scalpel pour cette session. Capture automatiquement l'empreinte technique (version du preset, commit Git, modèle, plateforme, ID de session) et consigne les blocages, refus inattendus, réussites et données nécessaires pour reproduire les anomalies dans le dossier unifié reports/bilans_scalpel/.",
    parameters: {
      type: 'object',
      properties: {
        task_objective: {
          type: 'string',
          description: "Description de l'objectif concret de la tâche réalisée."
        },
        verdict: {
          type: 'string',
          enum: ['success', 'partial', 'blocked', 'failed'],
          description: "Verdict global sur le fonctionnement de Scalpel pour cette tâche (success, partial, blocked, failed)."
        },
        summary: {
          type: 'string',
          description: "Résumé concis de l'évaluation, des points clés et du comportement observé."
        },
        tool_call_counts: {
          type: 'object',
          description: "Comptes d'appels par outil Scalpel (ex: { read_block: 5, edit_block: 2 })."
        },
        blockages_and_refusals: {
          type: 'array',
          description: "Liste des blocages, refus inattendus ou erreurs rencontrés avec les outils Scalpel.",
          items: {
            type: 'object',
            properties: {
              tool: { type: 'string', description: "Nom de l'outil (read_block, edit_block, etc.)." },
              target: { type: 'string', description: "Cible (chemin de fichier ou symbole)." },
              nature: { type: 'string', description: "Nature du problème (refus_inattendu, conflit_hash, erreur_parse, hors_racine, etc.)." },
              error_message: { type: 'string', description: "Message d'erreur ou de refus exact renvoyé par l'outil." },
              context_call: { description: "Arguments passés lors de l'appel." },
              workaround: { type: 'string', description: "Contournement utilisé ou indication du blocage." }
            },
            required: ['tool', 'nature', 'error_message']
          }
        },
        reproduction_data: {
          type: 'array',
          description: "Données nécessaires pour reproduire les anomalies ou comportements inattendus.",
          items: {
            type: 'object',
            properties: {
              title: { type: 'string', description: "Titre court du cas de reproduction." },
              target_file: { type: 'string', description: "Chemin du fichier concerné." },
              initial_state: { type: 'string', description: "État initial du bloc/fichier ou hash." },
              tool_call: { description: "Nom de l'outil et arguments exacts (JSON)." },
              expected: { type: 'string', description: "Comportement attendu." },
              actual: { type: 'string', description: "Comportement obtenu." },
              steps: { type: 'string', description: "Étapes textuelles de reproduction." }
            },
            required: ['title']
          }
        },
        successes: {
          type: 'array',
          description: "Points forts et réussites constatés lors de la session.",
          items: { type: 'string' }
        },
        recommendations: {
          type: 'string',
          description: "Recommandations et axes d'amélioration pour Scalpel."
        }
      },
      required: ['task_objective', 'verdict', 'summary']
    },
    output: {
      schema: OUTPUT_SCHEMA,
      render: (_args, value) => textResult(value)
    },
    async execute(args, exec) {
      const { saveScalpelReport } = await import('./report.js')
      const ws = getSessionWorkspace(exec)
      const res = saveScalpelReport(args, { exec, workspace: ws })
      return {
        status: 'success',
        message: `Bilan Scalpel consolidé enregistré avec succès dans ${res.mdPath}`,
        reportsDir: res.reportsDir,
        jsonPath: res.jsonPath,
        mdPath: res.mdPath,
        verdict: res.report.verdict,
        environment: res.report.environment
      }
    }
  })

  // Repli hors index unifié dans read_block / edit_block / delete_block (Lot U)
  // 2. Commande slash /toolset [scalpel|standard|status] avec source v4 valide.
  //    Le mode affiche vient de la projection (journal), jamais d'un etat local.
  function notice(agent, text, summary) {
    if (typeof agent?.inject !== 'function') return
    try {
      agent.inject({
        id: randomUUID(),
        role: 'user',
        content: [{ type: 'text', text }],
        source: { kind: 'toolset', form: 'notice', summary }
      })
    } catch (err) {
      console.warn('[toolset] inject notice failed', err)
    }
  }

  ctx.inject(['commands'], (commandCtx) => {
    commandCtx.commands.register({
      name: 'toolset',
      description: "Bascule dynamiquement le jeu d'outils de code pour cette session (scalpel, standard ou status)",
      input: { hint: '[scalpel|standard|status]' },
      handler: async ({ agent, rawInput }) => {
        const choice = String(rawInput ?? '').trim().toLowerCase()
        const ws = agent?.session?.header?.cwd || process.cwd()

        if (choice === 'scalpel') {
          try {
            syncRestriction(agent, true)
          } catch (err) {
            return { kind: 'error', text: `Echec de la restriction d'outils: ${err.message}` }
          }
          notice(agent, buildScalpelNotice(ws), 'Mode Scalpel active pour cette session')
          return {
            kind: 'success',
            text: [
              '[Toolset] Mode Scalpel active pour cette session.',
              `Workspace actif : ${ws}`,
              `Outils fichiers plats masques : ${FLAT_TOOLS.join(', ')}`,
              `Outils Scalpel actifs : ${SCALPEL_TOOLS.join(', ')}`,
              'Le contrat complet est ajoute aux instructions systeme.'
            ].join('\n')
          }
        }

        if (choice === 'standard') {
          try {
            syncRestriction(agent, false)
          } catch (err) {
            return { kind: 'error', text: `Echec du retablissement des outils: ${err.message}` }
          }
          notice(
            agent,
            `[RUNTIME SYSTEM EVENT: RETOUR AU MODE STANDARD]\nLes outils de fichiers plats (read, edit, write, grep, glob) sont de nouveau ACTIFS. Le contrat Scalpel ne s'applique plus.\nWorkspace actif : ${ws}`,
            'Mode Standard retabli pour cette session'
          )
          return {
            kind: 'success',
            text: [
              '[Toolset] Mode Standard retabli pour cette session.',
              `Workspace actif : ${ws}`,
              `Outils fichiers plats reactives : ${FLAT_TOOLS.join(', ')}`
            ].join('\n')
          }
        }

        const { mode, explicit } = readModes(agent?.session)
        return {
          kind: 'success',
          text: [
            `[Toolset] Etat actuel : ${mode === 'scalpel' ? 'Scalpel (outils plats masques)' : 'Standard (tous les outils)'}${explicit === null ? ' [defaut du preset]' : ''}`,
            `Workspace actif : ${ws}`,
            'Commandes disponibles :',
            '  /toolset scalpel   -> Active Scalpel et injecte le contrat complet',
            '  /toolset standard  -> Retablit tous les outils standard'
          ].join('\n')
        }
      }
    })
  })
}
