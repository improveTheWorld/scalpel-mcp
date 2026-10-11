import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

export const MODES = ['read-only', 'workspace-write', 'danger-full-access']
export const ESCALATION_TARGETS = ['workspace-write', 'danger-full-access']
const WIDER_MODES = {
  'read-only': ['workspace-write', 'danger-full-access'],
  'workspace-write': ['danger-full-access']
}

// Champs de schéma à ajouter aux 3 outils qui mutent (textes EXACTS du jeu plat).
export const ESCALATION_FIELDS = {
  sandbox_permissions: {
    type: 'string',
    enum: [...ESCALATION_TARGETS],
    description: 'The narrowest wider sandbox mode for a one-shot retry of the exact operation the sandbox just denied; the retry asks the user for approval.'
  },
  justification: {
    type: 'string',
    description: 'Required with sandbox_permissions: one sentence for the user explaining why this exact file operation needs the wider access. Use the language of the user\\u2019s current request.'
  }
}

export function validateEscalationArgs(sandboxPermissions, justification) {
  if (sandboxPermissions !== undefined && justification === undefined) throw new Error('invalid escalation: sandbox_permissions requires a justification')
  if (justification !== undefined && sandboxPermissions === undefined) throw new Error('invalid escalation: justification is only valid together with sandbox_permissions')
  if (justification !== undefined && String(justification).trim().length === 0) throw new Error('invalid justification: expected a non-empty sentence')
}

export async function approveEscalation({ requestedMode, effectiveMode, justification, subject }, { approver, agent, callId, toolName, signal }) {
  if (requestedMode === effectiveMode) return effectiveMode
  if (!(WIDER_MODES[effectiveMode] ?? []).includes(requestedMode)) throw new Error(`sandbox escalation to "${requestedMode}" is not strictly wider than this call's current "${effectiveMode}" mode`)
  if (approver === undefined) throw new Error(`sandbox escalation to "${requestedMode}" requires approval, but no approval service is composed`)
  if (agent === undefined) throw new Error(`sandbox escalation to "${requestedMode}" requires approval, but the call has no agent to route it through`)
  const outcome = await approver.request({
    agent, toolName, callId,
    reason: `escalate sandbox to ${requestedMode}: ${justification}`,
    ...(signal ? { signal } : {})
  })
  switch (outcome) {
    case 'allowed-once': return requestedMode
    case 'rejected': throw new Error(`the user rejected escalating this ${subject} to "${requestedMode}"; it stays denied, so stop and explain instead of working around it`)
    case 'cancelled': throw new Error(`approval for escalating to "${requestedMode}" was cancelled`)
    case 'unavailable': throw new Error(`sandbox escalation to "${requestedMode}" requires approval, but no approval channel is available`)
    default: throw new Error(`unexpected approval outcome: ${String(outcome)}`)
  }
}

export function canonicalPath(p) {
  try { return fs.realpathSync.native(p) } catch { return p }
}

export function writableRoots(policy) {
  if (policy.mode !== 'workspace-write') return []
  return [...new Set([policy.workspaceRoot, '/tmp', os.tmpdir()].map(canonicalPath))]
}

export function sandboxDenial(mode) {
  return new Error(`[sandbox: file access denied under ${mode} mode]\n[sandbox: escalation available \\u2014 retry this exact operation once with sandbox_permissions (the narrowest wider mode that suffices) + justification; the approval prompt asks the user]`)
}

export function isPathInside(parentCanon, childCanon) {
  let p = path.resolve(parentCanon)
  let c = path.resolve(childCanon)
  if (process.platform === 'win32') {
    p = p.toLowerCase()
    c = c.toLowerCase()
  }
  const rel = path.relative(p, c)
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel))
}


export function policyFor(exec, getSandboxPolicy) {
  const svc = typeof getSandboxPolicy === 'function' ? getSandboxPolicy() : getSandboxPolicy
  if (svc === undefined) {
    return { mode: 'danger-full-access', workspaceRoot: process.cwd() }
  }
  if (!svc) {
    throw new Error('Politique de bac à sable indisponible : opération refusée.')
  }
  let res
  if (typeof svc.resolve === 'function') {
    try {
      res = svc.resolve(exec?.agent?.session ? { session: exec.agent.session } : {})
    } catch {
      throw new Error('Politique de bac à sable indisponible : opération refusée.')
    }
  } else {
    res = svc
  }
  if (!res || !MODES.includes(res.mode) || typeof res.workspaceRoot !== 'string') {
    throw new Error('Politique de bac à sable indisponible : opération refusée.')
  }
  return res
}

export function checkSandbox(policy, mode, canonTarget) {
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

export async function enforceMutationSandbox({ ws, targets, args, exec, getSandboxPolicy, getApproval, toolName }) {
  validateEscalationArgs(args?.sandbox_permissions, args?.justification)
  const policy = policyFor(exec, getSandboxPolicy)
  let mode = policy.mode
  if (args?.sandbox_permissions !== undefined) {
    const approver = typeof getApproval === 'function' ? getApproval() : getApproval
    mode = await approveEscalation(
      { requestedMode: args.sandbox_permissions, justification: args.justification, effectiveMode: policy.mode, subject: 'operation' },
      { approver, agent: exec?.agent, callId: exec?.callId, toolName, signal: exec?.signal }
    )
  }
  if (mode === 'read-only') {
    throw sandboxDenial(mode)
  }
  const targetList = Array.isArray(targets) ? targets : (targets !== undefined && targets !== null ? [targets] : [])
  for (const t of targetList) {
    if (!t) continue
    const resolved = path.isAbsolute(t) ? t : path.resolve(ws, t)
    const canon = canonicalPath(resolved)
    checkSandbox(policy, mode, canon)
  }
  return { policy, mode }
}