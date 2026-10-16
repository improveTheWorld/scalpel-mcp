# Audit Détaillé du Pont et Plugin DSH Scalpel (`packages/dsh-tool-scalpel`)

**Agent Auditeur** : `67795dfa-f718-4408-96e8-cd566be2c1c8`  
**Capsule Enrichie** : `scalpel-mcp.dsh-integration` (`v5`)  
**Règle d'Or** : *« Un outil qui sait exécuter la demande d'un agent d'une façon déterministe ne doit JAMAIS la refuser, ni causer des tours supplémentaires. »*

---

## 1. Anomalies Critiques S1 — Faux `Stale read`, Destruction de Fichiers & Hints Fantômes

### 1.1. [CRITIQUE — S1] Intercepteur `isWholeFileOrUnresolvedError` dans `index.js` : écrasement des diagnostics AST et suppression/corruption du fichier parent sur `::`
* **Localisation** : `packages/dsh-tool-scalpel/lib/index.js:211-225` (`apply::isWholeFileOrUnresolvedError`), handler `edit_block` (`index.js:322-388`) et handler `delete_block` (`index.js:390-426`).
* **Mécanisme constaté** :
  1. `isWholeFileOrUnresolvedError(resOrErr)` retourne `true` dès que la réponse du moteur Python contient `'whole code file'`, `'whole file'`, `'nothing inside it is a block'`, `'recognised no container'`, `'unresolved'`, `'no symbol or markdown section has this identifier'`, ou `'outside the workspace root'`.
  2. Dans `edit_block` (`index.js:342`) et `delete_block` (`index.js:409`), dès que `callToolFailed` est `true`, le plugin tronque le suffixe `::...` :
     ```js
     const rawTarget = scalpelArgs.path.split('::')[0].split('#')[0]
     ```
     puis vérifie seulement `fs.statSync(absPath).isFile()` avant d'exécuter `fb.editFileBlock` ou `fb.deleteFileBlock` sur le **fichier entier** `absPath`.
* **Conséquences directes** :
  1. **Suppression accidentelle du fichier source entier sur `delete_block("src/Foo.cs::TypoMethod")`** :
     - Si l'agent appelle `delete_block` sur un symbole mal orthographié (`src/Foo.cs::TypoMethod`), `patcher.py:8282` renvoie légitimement `"No symbol or Markdown section has this identifier"` avec la liste des candidats proches.
     - `isWholeFileOrUnresolvedError` matche `'no symbol or markdown section has this identifier'`, `index.js:409` coupe `::TypoMethod` en `src/Foo.cs` et appelle `fb.deleteFileBlock({ absPath: ".../src/Foo.cs" })` qui exécute `fsApi.unlink(absPath)` (`file_block.js:448`) ! **Le fichier de code entier est supprimé du disque** (et reste fantôme dans l'index SQLite).
  2. **Remplacement du diagnostic Python par un faux `Stale read` (ou corruption du fichier) sur `edit_block`** :
     - Si l'agent appelle `edit_block(path="src/Foo.cs::TypoMethod", ops=[...], expected_hash="a1b2c3d4")` (ou `edit_block(path="src/Foo.cs", ...)` lorsque `expected_hash` ne désigne pas un enfant unique), Python retourne un refus structuré listant les candidats ou l'outline du fichier (`is a whole code file`).
     - `index.js:342` intercepte ce refus, coupe `::TypoMethod`, et passe `src/Foo.cs` à `fb.editFileBlock`.
     - `editFileBlock` compare `expected_hash` (hash du symbole) au SHA-256 du **fichier entier** `src/Foo.cs` et lève `Error: Stale read: the block changed since you read it (expected a1b2c3d4, current <fileHash>)` (`file_block.js:383`) ! L'agent perd la liste des candidats AST et reçoit un faux `Stale read` portant le hash du fichier entier (+2 à +4 tours).
     - Pire : si `expected_hash` est omis (optionnel dans le schéma), `editFileBlock` **applique les opérations de lignes du symbole directement sur les lignes `1..k` du fichier entier**, corrompant l'en-tête du fichier source sans validation Tree-sitter ni mise à jour SQLite.
* **Optimisation concrète** :
  - Depuis `v0.4.1` (`patcher.py:2750-2801`, `_file_text_block`), le moteur Python gère déjà nativement en lecture et en écriture (unitaire et batch) tous les fichiers plats (`file_text`) et fichiers de code sans conteneur situés dans le workspace.
  - Dans `index.js` :
    1. **Interdire formellement** tout repli vers `file_block.js` dès que `path` contient `'::'` ou `'#'` (l'agent cible un sous-bloc AST/Markdown, jamais un fichier plat).
    2. Restreindre le repli `editFileBlock` aux seuls cas où Python refuse parce que le fichier est **hors du workspace** (`resolves outside the workspace root`) ou exclu par `ignore_patterns` (`matches the workspace ignore pattern`), jamais sur `'whole code file'` ni `'no symbol or markdown section has this identifier'`.

---

### 1.2. [CRITIQUE — S1/S2] `hintForText` dans `fallback.js` injecte des noms d'outils fantômes (`read_unindexed`, `edit_unindexed`, `write_unindexed`, `delete_unindexed`) et casse le JSON
* **Localisation** : `packages/dsh-tool-scalpel/lib/fallback.js:23-50` (`hintForText`) & `700-718` (`hintFor`), appelés dans `index.js:316, 319, 387, 425, 482`.
* **Mécanisme constaté** :
  - Les outils `read_unindexed`, `edit_unindexed`, `write_unindexed`, `delete_unindexed` ont été supprimés de `ctx.tools.register` (Lot U, vérifié par `surface_unique.test.mjs:190-208`).
  - Pourtant, `index.js:124` instancie toujours `createFallback({ getMode: () => (config?.unindexedFallback === 'off' ? 'off' : 'on'), ... })` (donc `'on'` par défaut) et passe toutes les réponses de `insert_block`, `edit_block`, `delete_block`, `read_block` par `fallback.hintForText(res)`.
  - Dès qu'un refus JSON contient `is a FILE, not a block that can take a child` ou l'une des `UNINDEXED_SIGNATURES` (`fallback.js:7-14`), `hintForText` concatène après le JSON :
    `repli_hint: Fichier hors couverture Scalpel : read_unindexed / edit_unindexed / write_unindexed / delete_unindexed ...`
* **Conséquences** :
  1. **Incubation d'appels vers des outils inexistants (+1 à +2 tours)** : L'agent tente d'appeler `write_unindexed` ou `read_unindexed` prescrits par le hint et reçoit un refus du harnais DSH.
  2. **Contradiction avec le moteur sur `insert_block`** : Le hint affirme *"insert_block ne crée que l'arbre (fichiers/dossiers) et des paragraphes .md"*, alors que `insert_block` crée aussi des symboles de code sous une classe ou un namespace (`insert_block(parent='file.cs::ClassName', text=...)`, corrigé en `v0.4.4` `E3/F2`).
  3. **Corruption du format JSON** : Ajouter `\nrepli_hint: ...` à la fin d'une chaîne JSON valide (`{"status": "error", ...}`) produit une chaîne JSON invalide.
* **Optimisation concrète** :
  - Supprimer toute mention de `*_unindexed` dans `fallback.js:37-46` et `703-716` (ou retirer l'appel `fallback.hintForText` dans `index.js` puisque `patcher.py` fournit déjà des `actionable_hint` exacts en `v0.4.4`).

---

## 2. Anomalies Majeures S2 — Refus Évitables, Perte du `\n` Final et Désynchronisations `runner.py`

### 2.1. [MAJEUR — S2] `insert_block` avec `content` dans `index.js` : refus évitable hors dossier, perte du `\n` terminal (régression D5/F3) et oubli du mode batch `inserts=[...]`
* **Localisation** : `packages/dsh-tool-scalpel/lib/index.js:269-274, 294-320` et `src/ast_graph_server/server.py:1220-1345`.
* **Trois défauts mesurés** :
  1. **Refus artificiel quand `content` est utilisé sur un document Markdown ou un symbole de code (`index.js:303-313`)** :
     - Si un agent appelle `insert_block(parent="docs/guide.md", text="## Nouvelle Section", content="Corps du paragraphe")`, `index.js:311` lève une exception :
       `Error: Lorsque 'content' est fourni, 'parent' doit être un dossier existant ("docs/guide.md" n'est pas un dossier).`
     - Or `patcher.py:7650-7653` supporte nativement `content` sur les documents Markdown (`if content is not None: heading_text = text.strip(); body_text = content`) et la concaténation `${text}\n${content}` fonctionne aussi de manière déterministe pour un symbole de code. Ce refus viole directement la Règle d'Or (+1 tour).
  2. **Réintroduction du bug D5/F3 (perte du `\n` final sur création de fichier) par `index.js:314-315`** :
     - Au lieu de transmettre `content` au moteur Python, `index.js:314-315` fait :
       ```js
       const payloadText = `${text}\n${content}`
       const res = await callTool(ws, 'insert_block', { parent, anchor, text: payloadText })
       ```
     - Côté Python, `server.py:1220-1258` ne déclare pas `content: Optional[str] = None` dans sa signature `@self.mcp.tool` (alors qu'il le gère déjà dans `inserts` à la ligne `server.py:1309` via `item.get("content")` !).
     - Résultat : `patcher.py:7650` reçoit `content=None` et passe `payloadText` dans `_split_block_text(text)` (`patcher.py:7999`) qui exécute `.strip("\n")`, **supprimant systématiquement le saut de ligne final `\n` de tout fichier créé avec `insert_block(content=...)` depuis DSH** !
  3. **Oubli de `inserts=[...]` dans `enforceMutationSandbox` (`index.js:269-274`)** :
     - `targets` n'inspecte que `args.parent` et ignore `args.inserts` (`[{parent, anchor, text}, ...]`). En mode batch `insert_block(inserts=[...])` sans `parent` top-level, `targets` est vide `[]` et le contrôle de chemin sandbox est contourné.
* **Optimisation concrète** :
  - Ajouter `content: Optional[str] = Field(default=None, ...)` dans `server.py:1250` et passer `content` à `_insert_one(parent, anchor, text, content)` (`server.py:1345`).
  - Dans `index.js:294-320`, ne plus refuser `content` lorsque `parent` est un document ou un symbole, et transmettre directement `content` (ainsi que dans `inserts=[...]`).
  - Dans `index.js:269-274`, collecter tous les `entry.parent || args.parent` de `args.inserts` dans `targets`.

---

### 2.2. [MAJEUR — S2] `dispatch_command` dans `runner.py` : désynchronisations d'index et de `mtime_cache`
* **Localisation** : `packages/dsh-tool-scalpel/lib/runner.py:59-148` (`dispatch_command`).
* **Quatre angles morts identifiés** :
  1. **Oubli de `args["inserts"]` dans la collecte des `candidates` (`runner.py:64-82`)** :
     - `dispatch_command` inspecte `path`, `parent`, `parent_id`, `block_id`, `paths` et `edits`, mais **ignore totalement `args.get("inserts")`** !
     - Lors d'un `insert_block(inserts=[{parent: "doc.md", ...}])`, `doc.md` n'est ni pré-réindexé s'il a changé sur disque avant l'appel, ni purgé de `mtime_cache` après l'appel (`runner.py:144-146`).
  2. **Entrées `edits` sans `path` (résolues par `expected_hash`) non purgées de `mtime_cache` (`runner.py:75-81 & 144-146`)** :
     - Lorsqu'un agent envoie `edit_block(edits=[{"expected_hash": "a1b2c3d4", "ops": [...]}])`, `mutated_abs_paths` ne contient pas le chemin résolu par `expected_hash`.
  3. **Outils de recherche et de carte (`search_symbols`, `search_documents`, `get_project_map`, `build_context`) et `read_block` sur dossier aveugles aux modifications disque externes (`runner.py:64, 93`)** :
     - La synchronisation `mtime_cache` ne s'exécute que pour `cmd in ("read_block", "edit_block", "insert_block", "delete_block")` et seulement si `abs_p.is_file()`.
     - Si `read_block(path="src/dossier")` est appelé sur un **dossier** (`abs_p.is_dir()`), `abs_p.is_file()` est `False` : les fichiers modifiés hors Scalpel dans ce dossier ne sont pas réindexés avant de construire le listing du dossier.
  4. **Incohérence `discovery_exclude_patterns` entre `runner.py:93` et `patcher.py:7526`** :
     - Dans `runner.py:93`, `server.indexer.ignore_pattern_for(rel_p) is None` (`include_discovery=False`) indexe dans SQLite tout fichier de `reports/` ou `benchmarks/results/` dès qu'il est lu avec `read_block`, polluant ensuite `search_documents` et `build_context`.
     - En sens inverse, dans `_create_child_of` (`patcher.py:7526`), `if indexer.should_ignore(target):` utilise `include_discovery=True`, ce qui **refuse** `insert_block(parent="reports/bilans_scalpel", text="bilan.md\n...")` (`matches an ignore pattern of this workspace`) alors que `read_block` et `edit_block` sur `reports/` sont autorisés !
* **Optimisation concrète** :
  - Dans `runner.py:64-148` : collecter `args.get("inserts")` (`entry.get("parent")` et `entry.get("anchor")`), rafraîchir les fichiers enfants directs lorsque `abs_p.is_dir()` sur `read_block`, et invalider `mtime_cache` sur toute mutation.
  - Dans `patcher.py:7526`, utiliser `indexer.ignore_pattern_for(relative, include_discovery=False) is not None` au lieu de `indexer.should_ignore(target)`.

---

### 2.3. [MAJEUR — S2] `_normalize_edit_ops` & `_plain_tool_error` dans `runner.py` : normalisation JSON incomplète et résidu du message circulaire (#15)
* **Localisation** : `packages/dsh-tool-scalpel/lib/runner.py:22-56, 117-123`.
* **Mécanisme constaté** :
  1. **Normalisation JSON incomplète (`runner.py:44-56, 117-123`)** :
     - `_normalize_edit_ops` ne traite que le cas où `args["ops"]` est **déjà une `list`** contenant des chaînes JSON (`isinstance(args.get("ops"), list)`).
     - Si le modèle envoie `ops` lui-même sous forme de chaîne JSON (`ops: '[{"replace": [1, 2], "content": "..."}]'`), ou `edits: '[...]'`, ou `inserts: '[...]'`, ou `paths: '["a", "b"]'`, ou un élément `entry` de `edits`/`inserts` sous forme de chaîne JSON, `runner.py` ne le désérialise pas et Pydantic rejette l'appel (`the value of ops has the wrong type or shape`), coûtant 1 tour.
  2. **Résidu du message circulaire toxique de l'anomalie `#15` dans `_plain_tool_error` (`runner.py:24-29`)** :
     - Alors que `BACKLOG.md:163` (`#15`) indique que la consigne *"find it with search_documents"* a été supprimée de `toolsets.py`, elle est **toujours présente dans `runner.py:24-29`** :
       `"Retry the same call once; if it fails again, reach the same block another way (read the whole file, or find it with search_documents)."`
     - En outre, ce bloc masque complètement le message réel de l'exception `text` (`Error executing tool ...`).
* **Optimisation concrète** :
  - Étendre `_normalize_edit_ops` en un normaliseur déterministe dans `runner.py` (et dans `index.js`) qui désérialise tout argument `ops`, `edits`, `inserts`, `paths` (ainsi que leurs éléments) lorsqu'ils sont passés sous forme de chaîne JSON valide.
  - Dans `runner.py:24-29`, supprimer la consigne *"find it with search_documents"* et inclure l'extrait nettoyé de l'exception sous-jacente.

---

## 3. Divergences entre `file_block.js` (JS) et `patcher.py` / `lineops.py` (Python)

Pour les fichiers hors workspace qui passent par `file_block.js` (ainsi que le repli `outsideRoot` de `read_block` dans `index.js:449-479`), **5 divergences de comportement** avec le moteur Python causent des refus ou des écarts de hash :

1. **Rejet des `insert_after` multiples à la même position (`file_block.js:108-114` vs `lineops.py:110`)** :
   - `lineops.py:110` (`apply_ops`, Lot 1) autorise plusieurs `insert_after` à la même ligne `position` et les applique en ordre FIFO :
     `ordered = sorted(actions, key=lambda item: (item[0], 0 if item[0] == item[1] else 1, item[3]), reverse=True)`
   - `file_block.js:108-114` (`applyLineOps`) contient encore l'ancien garde :
     ```js
     if (insertPoints.has(position)) {
       return { text: null, error: `op ${index}: another insert already targets position ${position}.` };
     }
     ```
     et son `.sort()` (`file_block.js:149-152`) ne préserve pas l'ordre FIFO (`b.index - a.index` en bottom-up).
2. **Divergence de calcul de `content_hash` sur les fichiers avec BOM UTF-8 / UTF-16 (`file_block.js:371, 397` & `index.js:458` vs `patcher.py:2353, 2846`)** :
   - Dans `patcher.py:2353` (`_raw_text_payload`) et `patcher.py:2846` (`_edit_block_locked`), lorsqu'un fichier commence par un BOM (`EF BB BF`, `FF FE`, `FE FF`), `content_hash` est le SHA-256 du texte UTF-8 **sans le BOM**.
   - Dans `index.js:458` et `file_block.js:371`, le hash est calculé sur `buf` / `finalText` **avec le BOM** (`createHash('sha256').update(buf)`), et les BOM UTF-16LE/BE (`#34`) ne sont pas supportés.
3. **Refus `Stale read` dans `file_block.js:382-386` sans `numbered_text` ni `line_count`** :
   - Contrairement à `patcher.py:2928-2950` qui retourne le `numbered_text` frais dans le message d'erreur pour permettre la correction en 1 tour sans `read_block`, `file_block.js:383` lève une simple erreur `Stale read: the block changed since you read it (expected ..., current ...) [h:...]` sans le contenu numéroté du fichier, forçant un tour supplémentaire de `read_block`.
4. **Absence d'héritage du `path` top-level et d'atomicité/rebasage sur `edits=[...]` dans `index.js:357-384`** :
   - À la ligne `index.js:359`, `if (!entry || typeof entry.path !== 'string')` échoue si une entrée unique de `edits` omet `entry.path` en comptant sur `scalpelArgs.path` au top-level (pourtant supporté par `server.py:1027` et `index.js:258`).
   - De plus, si plusieurs entrées de `edits` ciblent le même fichier hors-racine, `index.js:379` appelle `fb.editFileBlock` en boucle séquentielle sur le fichier disque sans rebasage des numéros de lignes ni des `expected_hash`.
5. **Oubli de `paths=[...]` dans le repli `outsideRoot` de `read_block` (`index.js:449`)** :
   - Le handler `read_block` (`index.js:449`) teste `if (outsideRoot && typeof scalpelArgs?.path === 'string')`.
   - Si l'agent utilise la lecture groupée `read_block(paths=["../other/file.txt"])` (prescrite par le contrat Scalpel), `scalpelArgs.path` est `undefined` et la lecture hors-racine échoue au lieu d'être servie.

---

## 4. Anomalies du Pont Démon (`bridge.js`) et des Schémas (`agent_schemas.json`)

### 4.1. [MOYEN — S2/S3] `computePythonCodeSignature` dans `bridge.js` : ~25 appels `fs.statSync` / `readdirSync` synchrones à CHAQUE appel d'outil
* **Localisation** : `packages/dsh-tool-scalpel/lib/bridge.js:25-58` (`computePythonCodeSignature`) et `156-164` (`ensureDaemon`).
* **Mécanisme constaté** :
  - `spawnRunner` (`bridge.js:209`) appelle `ensureDaemon()` à chaque invocation d'outil.
  - `ensureDaemon()` (`bridge.js:157`) exécute `computePythonCodeSignature()` **avant même de vérifier `pendingRequests.size === 0`** (`bridge.js:159`).
  - `computePythonCodeSignature()` parcourt récursivement tout `src/ast_graph_server` en synchrone (`readdirSync`, `statSync` sur chaque `.py`, `readFileSync('__init__.py')`). Lors de rafales d'appels parallèles, le calcul est même jeté si `pendingRequests.size > 0`.
* **Optimisation concrète** :
  - Ne calculer `computePythonCodeSignature()` dans `ensureDaemon()` que si `!daemonProc` ou si `pendingRequests.size === 0` ET que la dernière vérification remonte à plus de `1000 ms` (mémoïsation courte par timestamp).

### 4.2. [MOYEN — S2/S3] `handleDaemonLine` dans `bridge.js:148-150` : conversion forcée `\n` $\to$ `\r\n` sous Windows
* **Localisation** : `packages/dsh-tool-scalpel/lib/bridge.js:148-150` (`handleDaemonLine`).
* **Mécanisme constaté** :
  ```js
  const winOut = process.platform === 'win32' ? rawOut.replace(/\r?\n/g, '\r\n') : rawOut
  req.resolve(winOut.trim())
  ```
  - Convertir toutes les fins de ligne de la réponse texte/JSON du démon en `\r\n` sous Windows alourdit inutilement toutes les réponses d'outils d'un octet `\r` par ligne et crée une asymétrie Windows/Linux dans les chaînes retournées au LLM.

### 4.3. [MAJEUR — S2] Contradiction dans `agent_schemas.json` & `toolset_schemas.py` : fichiers `.ps1` / `.txt` déclarés `READ-ONLY`
* **Localisation** : `packages/dsh-tool-scalpel/lib/agent_schemas.json:144, 165`, `src/ast_graph_server/toolset_schemas.py:55`, `src/ast_graph_server/server.py:60` (`READ_BLOCK_DESCRIPTION`).
* **Mécanisme constaté** :
  - Le schéma de `read_block` indique encore au modèle :
    > *"A file NO parser and NO document reader claims (a .ps1, a .txt, a shell script) returns its WHOLE text, numbered from 1 with its hash, and is **READ-ONLY**."*
  - Or depuis `v0.4.1` (Anomalie `#13`), ces fichiers sont **éditables via `edit_block`** (`read_only: False` dans `patcher.py:2363`). Déclarer dans le schéma de l'outil qu'ils sont `READ-ONLY` pousse directement l'agent à utiliser `pwsh` pour éditer les fichiers `.ps1`, `.txt` ou `.gitignore`.
* **Optimisation concrète** :
  - Remplacer `and is READ-ONLY` / `read-only` dans `server.py:60`, `toolset_schemas.py:55` et `agent_schemas.json:144` par `and is editable as a single file_text block with edit_block`.

---

## 5. Synthèse Priorisée des Optimisations Recommandées

| Priorité | Fichier / Symbole | Optimisation concrète | Gain |
| :---: | :--- | :--- | :--- |
| **P0 (S1)** | `index.js:211-426` | Interdire tout repli vers `file_block.js` si `path` contient `::` ou `#`, ou si l'erreur est `'whole code file'` / `'no symbol or markdown section has this identifier'`. Limiter le repli JS aux fichiers hors workspace / ignorés. | Élimine la suppression accidentelle de fichiers sources sur `delete_block` et les faux `Stale read` masquant les candidats AST sur `edit_block`. |
| **P0 (S1/S2)** | `fallback.js:23-50` | Purger les hints vers les outils fantômes `*_unindexed` supprimés au Lot U et ne plus corrompre l'enveloppe JSON. | Supprime les appels d'outils inexistants (+1 à +2 tours). |
| **P1 (S2)** | `index.js:269-320` & `server.py:1250` | Exposer `content` dans `server.py::insert_block`, transmettre `content` sans `.strip("\n")`, autoriser `content` sur tout type de parent dans `index.js`, et inclure `inserts` dans `enforceMutationSandbox`. | Corrige la perte du `\n` terminal en DSH, évite le refus sur `insert_block(parent="doc.md", content=...)` et sécurise le sandbox batch. |
| **P1 (S2)** | `runner.py:24-148` & `patcher.py:7526` | Ajouter `inserts` dans `candidates`, désérialiser tout `ops`/`edits`/`inserts`/`paths` stringifié en JSON, purger le message circulaire `#15` dans `_plain_tool_error`, et autoriser `_create_child_of` sur `discovery_exclude_patterns` (`reports/`). | Zéro refus de validation sur JSON stringifié, index toujours frais sur `inserts=[...]`, création de fichiers débloquée dans `reports/`. |
| **P2 (S2)** | `file_block.js:108-397` & `index.js:449` | Aligner `applyLineOps` sur `lineops.py:110` (`insert_after` multiples FIFO), hacher sans BOM, inclure `numbered_text` dans le refus `Stale read`, et supporter `paths=[...]` hors workspace dans `read_block`. | Parité 100 % entre le repli hors-racine JS et `patcher.py`. |
| **P2 (S2/S3)** | `bridge.js:25-164` & `agent_schemas.json:144` | Mémoïser `computePythonCodeSignature()` (TTL 1s) quand `pendingRequests.size === 0` et remplacer `READ-ONLY` par `editable with edit_block` dans `agent_schemas.json`. | Réduit l'overhead I/O synchrone par appel et élimine l'incitation au contournement shell sur `.ps1`/`.txt`. |