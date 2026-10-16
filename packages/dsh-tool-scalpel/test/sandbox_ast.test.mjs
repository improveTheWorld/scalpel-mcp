import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { apply } from '../lib/index.js'

function createTestHarness({ policy, approver, onCall } = {}) {
  const tools = new Map()
  const ctx = {
    tools: {
      register(tool) {
        tools.set(tool.name, tool)
      }
    },
    inject: (_deps, fn) => fn(ctx),
    on: () => {},
    get: () => undefined,
    systemPrompt: {
      section: () => {},
      getSectionOrder: () => 1100
    },
    commands: { register: () => {} },
    logger: { warn: () => {} }
  }

  const calls = []
  const mockCallTool = async (ws, name, args) => {
    calls.push({ ws, name, args })
    if (onCall) return onCall(ws, name, args)
    return { status: 'success', block_id: args?.path || args?.parent, content_hash: 'mockhash' }
  }

  apply(ctx, { unindexedFallback: 'ask' }, {
    executeScalpel: mockCallTool,
    callTool: mockCallTool,
    getSandboxPolicy: () => policy,
    getApproval: () => approver
  })

  return {
    tools,
    calls,
    execFor(ws) {
      return {
        agent: { session: { id: 'test-sess', header: { cwd: ws } } },
        callId: 'call-1',
        signal: undefined
      }
    }
  }
}

test('1. edit_block sous read-only -> refus immédiat avec sandboxDenial, callTool non appelé', async () => {
  const ws = path.join(os.tmpdir(), 'test-ws-1')
  const { tools, calls, execFor } = createTestHarness({
    policy: { mode: 'read-only', workspaceRoot: ws }
  })
  await assert.rejects(
    async () => {
      await tools.get('edit_block').execute({
        path: 'src/app.js::run',
        ops: [{ replace: [1, 1], content: 'console.log(1)' }],
        expected_hash: 'abc'
      }, execFor(ws))
    },
    (err) => {
      assert.ok(err.message.includes('[sandbox: file access denied under read-only mode]'))
      return true
    }
  )
  assert.equal(calls.length, 0, 'callTool ne doit pas être appelé')
})

test('2a. insert_block (symbole) sous read-only -> refus immédiat avec sandboxDenial', async () => {
  const ws = path.join(os.tmpdir(), 'test-ws-2a')
  const { tools, calls, execFor } = createTestHarness({
    policy: { mode: 'read-only', workspaceRoot: ws }
  })
  await assert.rejects(
    async () => {
      await tools.get('insert_block').execute({
        parent: 'src/app.js::run',
        text: 'const a = 1'
      }, execFor(ws))
    },
    (err) => {
      assert.ok(err.message.includes('[sandbox: file access denied under read-only mode]'))
      return true
    }
  )
  assert.equal(calls.length, 0, 'callTool ne doit pas être appelé')
})

test('2b. insert_block (création de fichier avec content) sous read-only -> refus immédiat avec sandboxDenial', async () => {
  const ws = path.join(os.tmpdir(), 'test-ws-2b')
  const { tools, calls, execFor } = createTestHarness({
    policy: { mode: 'read-only', workspaceRoot: ws }
  })
  await assert.rejects(
    async () => {
      await tools.get('insert_block').execute({
        parent: '.',
        text: 'created.js',
        content: 'export const x = 1\n'
      }, execFor(ws))
    },
    (err) => {
      assert.ok(err.message.includes('[sandbox: file access denied under read-only mode]'))
      return true
    }
  )
  assert.equal(calls.length, 0, 'callTool ne doit pas être appelé')
})

test('3. delete_block sous read-only -> refus immédiat avec sandboxDenial', async () => {
  const ws = path.join(os.tmpdir(), 'test-ws-3')
  const { tools, calls, execFor } = createTestHarness({
    policy: { mode: 'read-only', workspaceRoot: ws }
  })
  await assert.rejects(
    async () => {
      await tools.get('delete_block').execute({
        path: 'src/app.js::run'
      }, execFor(ws))
    },
    (err) => {
      assert.ok(err.message.includes('[sandbox: file access denied under read-only mode]'))
      return true
    }
  )
  assert.equal(calls.length, 0, 'callTool ne doit pas être appelé')
})

test('4. workspace-write : mutation permise dans le workspace et sous os.tmpdir()', async () => {
  const ws = path.join(os.tmpdir(), 'test-ws-4')
  const { tools, calls, execFor } = createTestHarness({
    policy: { mode: 'workspace-write', workspaceRoot: ws }
  })
  // Dans le workspace
  await tools.get('edit_block').execute({
    path: 'src/app.js::run',
    ops: [{ replace: [1, 1], content: 'ok' }]
  }, execFor(ws))
  assert.equal(calls.length, 1)

  // Sous os.tmpdir()
  const tmpTarget = path.join(os.tmpdir(), 'scratch.js') + '::temp'
  await tools.get('edit_block').execute({
    path: tmpTarget,
    ops: [{ replace: [1, 1], content: 'ok' }]
  }, execFor(ws))
  assert.equal(calls.length, 2)

  // insert_block dans le workspace
  await tools.get('insert_block').execute({
    parent: 'src/app.js::run',
    text: 'const b = 2'
  }, execFor(ws))
  assert.equal(calls.length, 3)

  // delete_block dans le workspace
  await tools.get('delete_block').execute({
    path: 'src/app.js::unused'
  }, execFor(ws))
  assert.equal(calls.length, 4)
})

test('5. workspace-write : mutation refusée si cible hors racines inscriptibles', async () => {
  const ws = path.join(os.tmpdir(), 'test-ws-5')
  const outsideFile = path.resolve('C:\\outside_sandbox_zone', 'external.js')
  const { tools, calls, execFor } = createTestHarness({
    policy: { mode: 'workspace-write', workspaceRoot: ws }
  })

  // edit_block hors racine
  await assert.rejects(
    async () => {
      await tools.get('edit_block').execute({
        path: `${outsideFile}::extSymbol`,
        ops: [{ replace: [1, 1], content: 'bad' }]
      }, execFor(ws))
    },
    (err) => {
      assert.ok(err.message.includes('[sandbox: file access denied under workspace-write mode]'))
      return true
    }
  )

  // insert_block hors racine (symbole)
  await assert.rejects(
    async () => {
      await tools.get('insert_block').execute({
        parent: `${outsideFile}::extSymbol`,
        text: 'const bad = true'
      }, execFor(ws))
    },
    (err) => {
      assert.ok(err.message.includes('[sandbox: file access denied under workspace-write mode]'))
      return true
    }
  )

  // insert_block hors racine (création de fichier)
  await assert.rejects(
    async () => {
      await tools.get('insert_block').execute({
        parent: path.resolve('C:\\outside_sandbox_zone'),
        text: 'created_outside.js',
        content: 'bad'
      }, execFor(ws))
    },
    (err) => {
      assert.ok(err.message.includes('[sandbox: file access denied under workspace-write mode]'))
      return true
    }
  )

  // delete_block hors racine
  await assert.rejects(
    async () => {
      await tools.get('delete_block').execute({
        path: `${outsideFile}::extSymbol`
      }, execFor(ws))
    },
    (err) => {
      assert.ok(err.message.includes('[sandbox: file access denied under workspace-write mode]'))
      return true
    }
  )

  assert.equal(calls.length, 0, 'callTool ne doit jamais être appelé')
})

test('6. edit_block en lot (edits=[...]) : une seule cible hors racine refuse tout le lot', async () => {
  const ws = path.join(os.tmpdir(), 'test-ws-6')
  const outsideFile = path.resolve('C:\\outside_sandbox_zone', 'external.js')
  const { tools, calls, execFor } = createTestHarness({
    policy: { mode: 'workspace-write', workspaceRoot: ws }
  })

  await assert.rejects(
    async () => {
      await tools.get('edit_block').execute({
        edits: [
          { path: 'src/good.js::okSymbol', ops: [{ replace: [1, 1], content: 'good' }] },
          { path: `${outsideFile}::badSymbol`, ops: [{ replace: [1, 1], content: 'bad' }] }
        ]
      }, execFor(ws))
    },
    (err) => {
      assert.ok(err.message.includes('[sandbox: file access denied under workspace-write mode]'))
      return true
    }
  )

  assert.equal(calls.length, 0, 'callTool ne doit jamais être appelé en cas de refus partiel')
})

test('7. Escalade unitaire : approver.request en allowed-once autorise la mutation et nettoie les arguments', async () => {
  const ws = path.join(os.tmpdir(), 'test-ws-7')
  const outsideFile = path.resolve('C:\\outside_sandbox_zone', 'external.js')
  const approverCalls = []
  const approver = {
    async request(req) {
      approverCalls.push(req)
      return 'allowed-once'
    }
  }

  const { tools, calls, execFor } = createTestHarness({
    policy: { mode: 'workspace-write', workspaceRoot: ws },
    approver
  })

  await tools.get('edit_block').execute({
    path: `${outsideFile}::extSymbol`,
    ops: [{ replace: [1, 1], content: 'escalated' }],
    sandbox_permissions: 'danger-full-access',
    justification: 'Accès nécessaire pour patch critique externe'
  }, execFor(ws))

  assert.equal(approverCalls.length, 1)
  assert.equal(approverCalls[0].toolName, 'edit_block')
  assert.equal(approverCalls[0].reason, 'escalate sandbox to danger-full-access: Accès nécessaire pour patch critique externe')

  assert.equal(calls.length, 1)
  assert.equal(calls[0].name, 'edit_block')
  assert.equal(calls[0].args.sandbox_permissions, undefined)
  assert.equal(calls[0].args.justification, undefined)
  assert.equal(calls[0].args.path, `${outsideFile}::extSymbol`)
})

test('8. Escalade refusée : approver renvoie rejected -> échec et callTool non appelé', async () => {
  const ws = path.join(os.tmpdir(), 'test-ws-8')
  const outsideFile = path.resolve('C:\\outside_sandbox_zone', 'external.js')
  const approver = {
    async request() {
      return 'rejected'
    }
  }

  const { tools, calls, execFor } = createTestHarness({
    policy: { mode: 'workspace-write', workspaceRoot: ws },
    approver
  })

  await assert.rejects(
    async () => {
      await tools.get('edit_block').execute({
        path: `${outsideFile}::extSymbol`,
        ops: [{ replace: [1, 1], content: 'escalated' }],
        sandbox_permissions: 'danger-full-access',
        justification: 'Demande qui sera rejetée'
      }, execFor(ws))
    },
    (err) => {
      assert.ok(err.message.includes('the user rejected escalating this operation to "danger-full-access"'))
      return true
    }
  )

  assert.equal(calls.length, 0, 'callTool ne doit pas être appelé après rejet utilisateur')
})

test('9. danger-full-access : mutations autorisées partout sans approbation', async () => {
  const ws = path.join(os.tmpdir(), 'test-ws-9')
  const outsideFile = path.resolve('C:\\outside_sandbox_zone', 'external.js')
  const approver = {
    async request() {
      assert.fail('approver ne doit jamais être sollicité en danger-full-access')
    }
  }

  const { tools, calls, execFor } = createTestHarness({
    policy: { mode: 'danger-full-access', workspaceRoot: ws },
    approver
  })

  await tools.get('edit_block').execute({
    path: `${outsideFile}::extSymbol`,
    ops: [{ replace: [1, 1], content: 'allowed' }]
  }, execFor(ws))

  await tools.get('insert_block').execute({
    parent: `${outsideFile}::extSymbol`,
    text: 'const x = 1'
  }, execFor(ws))

  await tools.get('delete_block').execute({
    path: `${outsideFile}::extSymbol`
  }, execFor(ws))

  assert.equal(calls.length, 3, 'les 3 mutations doivent avoir appelé callTool sans passer par approver')
})

test('10. Validation syntaxique des arguments d\'escalade', async () => {
  const ws = path.join(os.tmpdir(), 'test-ws-10')
  const { tools, execFor } = createTestHarness({
    policy: { mode: 'read-only', workspaceRoot: ws }
  })

  await assert.rejects(
    async () => {
      await tools.get('edit_block').execute({
        path: 'src/app.js::run',
        sandbox_permissions: 'workspace-write'
      }, execFor(ws))
    },
    (err) => {
      assert.ok(err.message.includes('sandbox_permissions requires a justification'))
      return true
    }
  )

  await assert.rejects(
    async () => {
      await tools.get('edit_block').execute({
        path: 'src/app.js::run',
        justification: 'justification seule'
      }, execFor(ws))
    },
    (err) => {
      assert.ok(err.message.includes('justification is only valid together with sandbox_permissions'))
      return true
    }
  )
})
test('11. insert_block en lot (inserts=[...]) : enforceMutationSandbox vérifie toutes les cibles du lot', async () => {
  const ws = path.join(os.tmpdir(), 'test-ws-11')
  const outsideFile = 'C:\\outside_forbidden_dir\\secret.js'
  const { tools, calls, execFor } = createTestHarness({
    policy: { mode: 'workspace-write', workspaceRoot: ws }
  })

  await assert.rejects(
    async () => {
      await tools.get('insert_block').execute({
        inserts: [
          { parent: path.join(ws, 'src/ok.js::App'), text: 'methodA() {}' },
          { parent: `${outsideFile}::Secret`, text: 'methodB() {}' }
        ]
      }, execFor(ws))
    },
    (err) => {
      assert.ok(err.message.includes('[sandbox: file access denied under workspace-write mode]'))
      return true
    }
  )
  assert.equal(calls.length, 0, 'callTool ne doit pas être invoqué si une entrée de inserts viole le sandbox')
})