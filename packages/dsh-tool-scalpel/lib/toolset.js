// Etat du jeu d'outils (Scalpel / Standard) d'une session.
//
// Source de verite unique : le JOURNAL de la session. La commande /toolset est
// deja journalisee par le harnais (`command/run` puis `command/done`) ; une
// projection de session la replie en { mode }. Le client la lit via
// useProjection('toolset') et le prompt systeme la lit cote hote : aucun etat
// local a l'UI, aucun etat en memoire du process pour le mode affiche.
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

export const TOOLSET_KEY = 'toolset'
export const TOOLSET_MODES = ['scalpel', 'standard']
export const CONTRACT_SECTION_NAME = 'scalpel:contract'
export const SCALPEL_CONTRACT_PATH = fileURLToPath(new URL('../SCALPEL_CONTRACT.md', import.meta.url))
const SCALPEL_WORKFLOW_PATH = fileURLToPath(new URL('./agent_workflow.json', import.meta.url))
let workflowCache = null

function loadWorkflow(path = SCALPEL_WORKFLOW_PATH, fsApi = fs) {
  try {
    const mtimeMs = fsApi.statSync(path).mtimeMs
    if (workflowCache && workflowCache.path === path && workflowCache.mtimeMs === mtimeMs) return workflowCache.text
    const raw = fsApi.readFileSync(path, 'utf8')
    const parsed = JSON.parse(raw)
    const text = typeof parsed?.text === 'string' ? parsed.text : ''
    workflowCache = { path, mtimeMs, text }
    return text
  } catch {
    return ''
  }
}

/**
 * Mode par defaut d'un preset. Meme regle que client.js (qui ne peut pas importer ce module).
 * @param {string|null|undefined} presetId
 */
export function defaultModeForPreset(presetId) {
  return typeof presetId === 'string' && presetId.includes('scalpel') ? 'scalpel' : 'standard'
}

/** Mode effectif : choix explicite journalise, sinon defaut du preset. */
export function resolveMode(explicitMode, presetId) {
  return TOOLSET_MODES.includes(explicitMode) ? explicitMode : defaultModeForPreset(presetId)
}

function fail(message) {
  throw new Error(`toolset projection: ${message}`)
}

// Schema minimal compatible avec l'usage du registre (`.parse` uniquement) ;
// ce package n'embarque pas zod.
const stateSchema = {
  parse(value) {
    if (value === null || typeof value !== 'object') fail('state must be an object')
    const { mode, running } = value
    if (mode !== null && !TOOLSET_MODES.includes(mode)) fail(`invalid mode ${JSON.stringify(mode)}`)
    if (running !== null) {
      if (running === undefined || typeof running !== 'object') fail('running must be an object or null')
      if (typeof running.commandId !== 'string') fail('running.commandId must be a string')
      if (!TOOLSET_MODES.includes(running.wanted)) fail('running.wanted invalid')
    }
    return value
  }
}

// Vues pre-allouees : la reference ne change que si le mode change, ce qui
// supprime toute publication sur les evenements sans rapport.
const VIEWS = {
  none: Object.freeze({ mode: null }),
  scalpel: Object.freeze({ mode: 'scalpel' }),
  standard: Object.freeze({ mode: 'standard' })
}

const viewSchema = {
  parse(value) {
    if (value === null || typeof value !== 'object') fail('view must be an object')
    if (value.mode !== null && !TOOLSET_MODES.includes(value.mode)) fail(`invalid view mode ${JSON.stringify(value.mode)}`)
    return value
  }
}

export const toolsetProjectionDefinition = {
  key: TOOLSET_KEY,
  stateVersion: 1,
  stateSchema,
  init: () => ({ mode: null, running: null }),
  apply(state, event) {
    if (event.type === 'command/run' && event.data?.name === 'toolset') {
      const wanted = typeof event.data.args === 'string' ? event.data.args.trim().toLowerCase() : ''
      // `status`, argument vide ou inconnu : la commande ne change rien.
      if (!TOOLSET_MODES.includes(wanted)) return state
      return { ...state, running: { commandId: event.data.commandId, wanted } }
    }
    if (event.type === 'command/done' && state.running !== null && event.data?.commandId === state.running.commandId) {
      // Seul un succes change le mode : une restriction refusee ne doit pas etre affichee comme appliquee.
      return { mode: event.data.kind === 'success' ? state.running.wanted : state.mode, running: null }
    }
    return state
  },
  wire: {
    viewSchema,
    view: (state) => (state.mode === null ? VIEWS.none : VIEWS[state.mode])
  }
}

let contractCache = null

/**
 * Texte integral du contrat. Ne leve jamais : une exception ici ferait echouer
 * l'assemblage du prompt systeme de TOUTES les requetes.
 * @returns {{ text: string, error?: string }}
 */
export function loadContract(path = SCALPEL_CONTRACT_PATH, fsApi = fs) {
  try {
    const mtimeMs = fsApi.statSync(path).mtimeMs
    if (contractCache && contractCache.path === path && contractCache.mtimeMs === mtimeMs) return { text: contractCache.text }
    const text = fsApi.readFileSync(path, 'utf8')
    contractCache = { path, mtimeMs, text }
    return { text }
  } catch (err) {
    return { text: '', error: String(err?.message ?? err) }
  }
}

/** Section de prompt : le contrat complet, au meme niveau que les contrats des outils plats. */
export function buildContractSection(contract, workflow = loadWorkflow()) {
  const body = contract.text.trim() !== ''
    ? contract.text.trim()
    : [
        `(Le fichier de contrat est illisible : ${contract.error ?? 'vide'}.)`,
        'Outils : read_block, search_symbols, search_documents, get_project_map, build_context, edit_block, insert_block, delete_block.',
        'edit_block exige le expected_hash obtenu par un read_block recent. Le shell ne sert ni a lire ni a editer du code source.'
      ].join('\n')
  const lines = [
    '# Mode Scalpel : contrat d\'ingenierie en vigueur pour cette session',
    'Les outils fichiers plats (read, edit, write, grep, glob) sont indisponibles. Scalpel n\'est pas un confort optionnel : c\'est la seule voie de lecture et d\'edition du code source. Contourner Scalpel par le shell est une violation du contrat.'
  ]
  const wf = typeof workflow === 'string' ? workflow.trim() : (workflow?.text?.trim?.() ?? '')
  if (wf !== '') {
    lines.push(
      '',
      'Flux de travail (repris mot pour mot du banc de mesure) :',
      '',
      wf
    )
  }
  lines.push('', body)
  return lines.join('\n')
}