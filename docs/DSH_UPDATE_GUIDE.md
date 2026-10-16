# Guide de Mise à Jour de Scalpel dans DeepSeek Harness (DSH)

Ce document décrit l'architecture de liaison entre le moteur Python Scalpel (`ast-graph-server`) et DeepSeek Harness (`@local/dsh-tool-scalpel`), ainsi que la **procédure complète et reproductible** pour mettre à jour Scalpel vers une nouvelle version (ex. `v0.4.3` $\to$ `v0.4.4`) sans décalage de démon ni désynchronisation de contrat.

---

## 1. Architecture de Liaison DSH $\leftrightarrow$ Scalpel

Le profil Web de DSH (`~/.dsh/profiles/web`) consomme Scalpel via un lien symbolique (`link:`) pointant directement vers `scalpel-mcp/packages/dsh-tool-scalpel`. Quatre composants déterminent la version réellement active en session :

1. **Moteur Python (`scalpel-mcp/src/ast_graph_server`)** :
   * Version déclarée dans `scalpel-mcp/pyproject.toml` et `scalpel-mcp/src/ast_graph_server/__init__.py` (`__version__`).
   * Installé en mode éditable (`pip install -e scalpel-mcp`) et chargé via `PYTHONPATH=scalpel-mcp/src` par le pont Node.js.
2. **Démon Python Persistant (`packages/dsh-tool-scalpel/lib/runner.py --daemon` & `lib/bridge.js`)** :
   * `bridge.js` maintient un sous-processus Python `runner.py --daemon` persistant en mémoire (multiplexage NDJSON) pour garantir une latence chaude $< 10\text{ ms}$.
   * **Rechargement automatique (depuis `v0.4.3`)** : `computePythonCodeSignature()` dans `bridge.js` calcule l'empreinte `(__version__, nombre_de_fichiers_py, mtime_runner_py, max_mtime_ast_graph_server)`. Dès qu'un fichier `.py` du moteur ou `runner.py` est modifié, `ensureDaemon()` ferme automatiquement l'ancien démon au prochain appel et en démarre un neuf.
3. **Instantanés de Surface et de Workflow (`packages/dsh-tool-scalpel/lib/`)** :
   * `lib/agent_schemas.json` : généré depuis `src/ast_graph_server/toolset_schemas.py::SCALPEL_SCHEMAS` via `scripts/export_agent_schemas.py`.
   * `lib/agent_workflow.json` : généré depuis `scalpel-benchmarks/benchmarks/runner.py::SCALPEL_SYSTEM_PROMPT` via `scripts/export_agent_workflow.py`. Lu à chaud (avec cache `mtimeMs`) par `lib/toolset.js` pour injecter la section `# Mode Scalpel` dans le prompt système de chaque tour.
4. **Manifestes NPM du Plugin et du Preset DSH** :
   * `scalpel-mcp/packages/dsh-tool-scalpel/package.json` (`@local/dsh-tool-scalpel`)
   * `scalpel-mcp/dsh/preset-scalpel/package.json` (`@local/dsh-preset-scalpel`)

---

## 2. Checklist de Mise à Jour Pas à Pas (ex. passage à `vX.Y.Z`)

### Étape 1 — Aligner les numéros de version dans les 5 fichiers sources

Mettre à jour la chaîne de version `X.Y.Z` dans :
1. `scalpel-mcp/pyproject.toml` (`version = "X.Y.Z"`)
2. `scalpel-mcp/src/ast_graph_server/__init__.py` (`__version__ = "X.Y.Z"`)
3. `scalpel-mcp/packages/dsh-tool-scalpel/package.json` (`"version": "X.Y.Z"`)
4. `scalpel-mcp/dsh/preset-scalpel/package.json` (`"version": "X.Y.Z"`)
5. `scalpel-benchmarks/benchmarks/runner.py` : dans `SCALPEL_SYSTEM_PROMPT` et `SCALPEL_SYSTEM_PROMPT_WITH_DOSSIER` (`This project is pre-indexed by Scalpel (vX.Y.Z).`)

### Étape 2 — Régénérer les instantanés de schémas et de workflow DSH

Si les schémas d'outils (`toolset_schemas.py`) ou le prompt de workflow (`benchmarks/runner.py`) ont changé, exporter les nouveaux JSON dans `packages/dsh-tool-scalpel/lib/` :

```powershell
python scalpel-mcp/packages/dsh-tool-scalpel/scripts/export_agent_schemas.py > scalpel-mcp/packages/dsh-tool-scalpel/lib/agent_schemas.json
python scalpel-mcp/packages/dsh-tool-scalpel/scripts/export_agent_workflow.py > scalpel-mcp/packages/dsh-tool-scalpel/lib/agent_workflow.json
```

> **Note** : `lib/toolset.js` invalide son cache dès que `mtimeMs` de `lib/agent_workflow.json` ou `SCALPEL_CONTRACT.md` change ; le nouveau texte de workflow est donc injecté immédiatement au tour suivant sans redémarrer DSH.

### Étape 3 — Vérifier ou recycler le démon Python actif

- **Si seul le code Python (`src/ast_graph_server/*.py` ou `runner.py`) a changé** :
  * Si le Host DSH exécute déjà `bridge.js` ($\ge$ `v0.4.3`), le redémarrage du démon Python est **automatique** au prochain appel d'outil Scalpel grâce à `computePythonCodeSignature()`.
  * Pour vérifier ou forcer manuellement le renouvellement du démon Python en cours de session :
    ```powershell
    Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
      Where-Object { $_.CommandLine -match 'runner\.py --daemon' } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    ```
    Le démon est recréé automatiquement dès l'appel `read_block` suivant.
- **Si le code JavaScript du plugin (`lib/index.js`, `lib/bridge.js`, `lib/sandbox.js`) ou `agent_schemas.json` a changé** :
  * Le cache ESM de Node.js dans le processus Host DSH conserve l'instance de module déjà importée (`lib/index.js`). Pour recharger le code JS ou les schémas d'outils en mémoire dans le Host, redémarrer le processus DSH (ou incrémenter l'identité du bundle).

### Étape 4 — Exécuter la suite de validation complète (Parité + Pont + Moteur)

Toujours valider les deux suites avant de commiter ou taguer la version :

```powershell
# 1. Tests de contrat, de parité de surface, de workflow et de démon DSH (120 tests Node.js)
node --test scalpel-mcp/packages/dsh-tool-scalpel/test/*.test.mjs scalpel-mcp/dsh/preset-scalpel/manifest.test.mjs

# 2. Tests unitaires du moteur Python et parité serveur/agent (710+ tests pytest)
python -m pytest scalpel-mcp/tests -q --tb=short
```