# Backlog Technique & Feuille de Route Unifiée : Scalpel (`scalpel-mcp` & `scalpel-benchmarks`)

Dernière mise à jour : 15/10/2026  
Statut : Document de référence unique consolidé (dédupliqué à partir des 43 bilans de sessions DSH `reports/bilans_scalpel/`, des 25 éplucheurs `v0.4.1` et des backlogs historiques `scalpel-mcp` / `scalpel-benchmarks`).

> **Principe directeur de Scalpel** : Remplacer les diffs textuels aveugles et les réécritures complètes de fichiers par des manipulations AST déterministes, protégées par un verrouillage optimiste de hachage de blocs (CAS) et un historique delta atomique. L'objectif est l'efficacité maximale pour les agents IA (un seul appel réussi par intention, zéro régression, zéro friction).

---

## 1. Orientations Stratégiques du Projet

Le projet Scalpel repose sur quatre piliers directeurs validés par les bancs de mesure A/B :
1. **La Surface Unique Déterministe** : Une boîte à outils unifiée (`read_block`, `edit_block`, `insert_block`, `delete_block`) traitant identiquement le code indexé par AST, les documents structurés (.md) et les fichiers sans grammaire (.txt, .ps1, scripts). Tout contournement par le shell pour lire ou éditer du code est proscrit.
2. **L'Intégrité par Verrouillage Optimiste (CAS)** : Aucune mutation aveugle. Chaque opération exige et revalide un cachet de version (`expected_hash`). En cas de modification concurrente, le moteur refuse et fournit le bloc frais pour réalignement immédiat.
3. **L'Historique Delta Atomique & Time-Travel** : Gestion de branches internes, de commits AST déconnectés de git (`checkpoint`, `rollback`, `branch`), permettant à l'agent d'explorer des hypothèses et d'annuler atomiquement ses erreurs de compilation.
4. **La Règle de Non-Régression & Mesure Adjacente** : Les outils mesurent, le raisonnement juge. Tout changement de code moteur est étalonné contre la série adjacente immédiate sur tâche réelle (`pwaf-prerequisites`), avec traçabilité intégrale des tours et des jetons.

## 2. Priorité Absolue — Optimisations Massives `search_documents` (Facteur x20 à x50)

Les campagnes de benchmark (`campaign_v3`) et les stress-tests du 13/10/2026 sur 1 000 sections ont mis en lumière un goulot d'étranglement critique sur la recherche documentaire (`search_documents`). Ces quatre chantiers sont classés en **Priorité Absolue**.

### A. Migration du moteur documentaire vers SQLite FTS5 + BM25 natif (Gain : x20 à x50) — Fait (13/10/2026)
* **Symptôme & Mesure** : `DatabaseManager.search_documents` (`scalpel-mcp/src/ast_graph_server/database.py`) effectuait un table scan linéaire complet via `WHERE (s.heading LIKE ? OR s.content LIKE ?)`.
  * Sur 1 000 sections : **56 ms à 376 ms** par requête.
  * Sur 50 000+ sections : latence de **plusieurs secondes par appel**, bloquant l'agent.
* **Solution technique & Validée** :
  1. Création de la table virtuelle SQLite FTS5 `document_sections_fts` avec tokenizer `unicode61`.
  2. Synchronisation atomique dans `upsert_document`, `delete_document` et backfill automatique dans `_init_db`.
  3. Branchement de `document_sections_fts MATCH ?` avec score combiné (`heading_hits`, `content_hits`, `phrase_bonus` + `-bm25(document_sections_fts, 10.0, 1.0)`), extraction de tokens FTS5 sûrs (`"{tok}"*` joints par `OR`) et repli transparent sur `LIKE` pour les requêtes ponctuation/sous-chaînes.
  4. Tests unitaires dédiés validés (`test_fts5_sync_on_upsert_and_delete`, `test_fts5_special_syntax_queries_behavior`, `test_search_documents_fts5_performance_and_special_queries`).
* **Gain mesuré** : Recherche plein texte avec ranking BM25 natif (< 5 ms sur 1 000+ sections) et zéro régression de rappel.

### B. Suppression du motif SQL N+1 sur l'inlining des sections (Gain : x5 à x10) — Fait (13/10/2026)
* **Symptôme & Mesure** : La première requête de ranking n'extrait qu'un snippet de 240 caractères (`substr(s.content, 1, 240)`). Dès que des sections doivent être inlinées (`full_top` ou `include_content=True`), le code exécutait un `SELECT` individuel via `get_document_section(item["section_id"])` pour chaque match retourné. Pour `limit=100`, cela générait **100 requêtes SQL séquentielles**.
* **Solution technique & Validée** : Rapatriement de l'ensemble des contenus en une seule requête groupée via `WHERE s.id IN (...)` dans `database.py::search_documents`. Élimination de 99 % des allers-retours SQL lors de l'inlining.

### C. Mémoïsation du parsing Markdown (`markdown_layout`) dans le Patcher (Gain : x5) — Fait (13/10/2026)
* **Symptôme & Mesure** : Pour calculer les métriques de bloc (`content_hash`, `line_count`, `numbered_text`) des sections inlinées, `server.py` appelle `self.patcher.block_metrics(displayed)`. Le Patcher relisait le fichier physique sur disque et réexécutait l'intégralité du découpage syntaxique Markdown (`markdown_layout`). Si 20 sections provenaient du même document (ex. `LINQ-to-Spark.md`), le document était relu et re-parsé 20 fois de suite dans le même tour d'agent.
* **Solution technique & Validée** : Mise en cache LRU courte par `(file_path, content_hash)` de la structure Markdown dans le Patcher (`_get_markdown_layout`). Suppression des I/O disque et re-parsings redondants lors des résolutions multiples.

### D. Clarification et sécurisation du cache de build après échec (`build_fingerprint`) — S2 / Harnais Benchmark
* **Symptôme & Mesure (Run 1 de `campaign_v3`)** : Lorsqu'un build a échoué (14 erreurs C# sur un membre `internal` invisible depuis le projet de test) et que l'agent n'a modifié aucun fichier de code avant de relancer `build()`, le harnais a répondu `build FAILED (no compilation) -- answered from the previous build`. L'agent a cru à un bug de cache et a brûlé 14 tours.
* **Action restante (`scalpel-benchmarks/benchmarks/toolsets.py`)** : Ne jamais servir un échec en cache sans préciser explicitement dès la 1re ligne : `[NO CODE CHANGED SINCE LAST FAILED BUILD] Exact same C# source files on disk; fix the compilation errors below before calling build() again`, ou forcer la recompilation réelle après un échec.

---

## 3. Chantiers Moteur & AST en cours (`src/`)

Chantiers issus du rapport des trous d'accès et des audits de performance du moteur :

1. **Harmonisation YAML et JSON avec Tree-sitter (F7 / E2) — Fait (13/10/2026)**
   * *Constat* : `read_block("fichier.yml")` ou `read_block("fichier.json")` renvoyait un objet `_structured_summary` sans `children` AST, échouait sur les séquences YAML / tableaux d'objets JSON (ex. `cordis.patch.yml`), et interceptait les clés `::` erronées avec `"No :: address exists in this file"`. Par ailleurs, un fichier de code indexé sans symbole extrait (ex. `client.js`) renvoyait une impasse `"recognised no container"`.
   * *Correctif validé* : `_structured_summary` enrichit désormais la réponse avec `children` (arbre AST des symboles indexés) et `content_hash`, supporte les séquences YAML et tableaux JSON d'objets, délègue les clés `::` inconnues à `_near_addresses` lorsque des symboles AST existent, et `_unindexed_path_refusal` replie les fichiers indexés sans symbole sur `_raw_text_payload(..., allow_supported=True)` (`file_text`).

2. **Préservation du saut de ligne final dans `insert_block` (D5 / F3) — Fait (13/10/2026)**
   * *Constat* : Un fichier créé avec `insert_block(content)` perdait son saut de ligne terminal (`\n`) en raison d'un `.strip("\n")` trop agressif dans `patcher.py::_split_block_text` (mesuré : 58 octets envoyés -> 57 octets écrits).
   * *Correctif validé* : `insert_block` sépare explicitement `content` lorsqu'il est fourni, préservant strictement l'octet terminal dans `_create_child_of` (validé par `test_insert_block_preserves_trailing_newline_in_content`).

3. **Nettoyage des messages d'erreur et des indices du moteur (E3 / F2) — S2**
   * Supprimer les messages contradictoires : `_cannot_receive_a_child` annonce parfois `insert_block(parent=fichier) creates one` alors que l'opération est refusée pour un fichier de code.
   * Si un parent est un fichier vide indexé, proposer explicitement l'insertion de racine ou l'initialisation de symbole plutôt qu'un appel circulaire à `search_symbols`.

## 4. Chantiers Plugin DSH & Surface Unifiée (`packages/dsh-tool-scalpel`)

État vérifié dans le code le 15/10/2026 :

1. **Lot P (Parité Plugin DSH = Serveur MCP) — Fait (13/10/2026)** :
   * `runner.py` dispatche désormais toutes les commandes via `server.mcp.call_tool(cmd, args)`, assurant 100 % de parité (`edits=[...]`, `inserts=[...]`, `view`, `full_top`, format texte compact).
2. **Gouvernance des Droits et Bac à Sable DSH (Lot F / D2) — Fait (13/10/2026)** :
   * Implémenté dans `packages/dsh-tool-scalpel/lib/sandbox.js` et branché dans `lib/index.js` (`checkMutationAllowed` vérifie la politique de sandbox avant toute mutation AST ou fichier).
3. **Granularité Fine pour JavaScript et TypeScript — Fait (niveau fonctions/méthodes/variables) & Résidu S2 ouvert** :
   * Découpage Tree-sitter actif (`test/typescript_granularity.test.mjs`).
   * **Résidu S2 (issu du bilan DSH `session-2a61` du 14/10)** : (a) `read_block` sur une fonction JS/TS exportée (`export function foo()`) omet le mot-clé `export` à la ligne 1 du `numbered_text`, ce qui provoque une erreur de syntaxe `export export function` lorsque l'agent remplace la ligne 1 en incluant `export` ; (b) absence de bloc adressable `::imports` pour les `import ...` de tête de fichier en JS/TS (contrairement à Python `::imports` et C# `::using`).
4. **Démonisation & Persistance du Pont Python DSH (`runner.py --daemon` & `bridge.js`) — Fait (13/10/2026)** :
   * Mode démon persistant NDJSON maintenant un dictionnaire `(cfg, server, mtime_cache)` par workspace avec latence chaude **< 10 ms par appel**.
5. **Cohérence `ignore_patterns` vs `read_block` / `edit_block` direct — S1/S2 (Nouveau, 15/10/2026)** :
   * *Constat mesuré* : Le Lot 3 de `build_context` a ajouté `"reports"`, `"reports/*"`, `"transcripts"`, `"benchmarks/results"` dans `ServerConfig.ignore_patterns`. Or `_directory_listing`, `_file_text_block` et `_unindexed_path_refusal` consultent `indexer.ignore_pattern_for()` ! Conséquence : `read_block("reports/bilans_scalpel")` renvoie `0 entries` (alors qu'il contient 87 fichiers) et `read_block("reports/bilans_scalpel/foo.md")` est refusé au premier appel, puis **accepté au second appel** parce que `runner.py` n'a indexé le fichier qu'après coup.
   * *Correctif requis* : Séparer `discovery_exclude_patterns` (exclus de `build_context`, `get_project_map`, `search_documents` et de l'indexation initiale automatique) des patterns de sécurité/binaire, afin qu'un appel explicite `read_block(path)` ou `edit_block(path)` sur un fichier de `reports/` ou `benchmarks/results/` fonctionne dès le 1er tour et que le listing de dossier affiche ses entrées.

## 5. Feuille de Route Produit & Profondeur Sémantique (Roadmap long terme)

Axes d'évolution majeurs issus de la roadmap d'ingénierie (`ROADMAP_AND_OPTIMIZATIONS.md`) :

1. **Inférence Sémantique des Types et Graphe d'Appels Inter-Fichiers** :
   * *Constat* : Tree-sitter produit un CST (arbre syntaxique concret) mais ignore les types statiques d'expressions complexes (`self.client.post(...)`).
   * *Objectif* : Intégrer un résolveur de types local léger pour corréler les symboles et enrichir `get_callers` / `search_symbols(view='contract')` avec les cibles réelles d'appels inter-modules.

2. **Extension du Support Multilangage via Tree-sitter** :
   * Étendre l'indexation AST et les gardes de syntaxe à de nouveaux langages prioritaires : **Rust** (`syn`/tree-sitter-rust), **Go** (tree-sitter-go), et **Java** (tree-sitter-java), en réutilisant l'infrastructure de parseurs modulaires de `parsers/base.py`.

3. **Indexation Incrémentale Distribuée et Git Pruning** :
   * Utiliser les empreintes `git diff-tree` pour n'indexer que les blobs modifiés lors de commits successifs.
   * Partager et persister la base SQLite AST (`.agent_graph.db`) de façon sûre entre sessions multiples sans collision d'écriture WAL.

## 6. Registre Consolidé des Anomalies d'Ergonomie et de Friction (Campagnes & Sessions DSH)

Suivi qualité unifié et dédupliqué (campagnes `v0.3.0` → `v0.4.1` + 43 bilans de sessions DSH du 13 au 15/10/2026) :

| # | Anomalie / Friction | Classe | Statut | Synthèse & Action |
|---|---|:---:|:---:|---|
| **1** | Plafonnement silencieux de `search_symbols(view='implementation')` | S2 | **CORRIGÉ** | Le plafond est désormais le `limit` explicite de l'appelant. |
| **2** | `read_block` sur `.md` sans préambule rend le hash du vide (`e3b0...`) | S3 | **CORRIGÉ (15/10)** | Expose désormais `file_content_hash` (hash du fichier entier) en sus du statut `root_text_empty: True`. |
| **3** | `list_documents` avec message d'absence auto-contradictoire | S3 | **CORRIGÉ** | Absorbé dans `search_documents`, messages reformulés. |
| **4** | `edit_block` dupliquant les lignes frontières sur plages inclusives | S2 | **CORRIGÉ** | Refus préventif immédiat avec mention des lignes dupliquées (`INCLUSIVE`). |
| **5** | `insert_block` renumérotant les sections frères et surprenant l'agent | S3 | **SURVEILLANCE** | Comportement documenté dans l'outil, suivi en campagne. |
| **6** | Paramètre obsolète `where="start"` tenté par les agents | S2 | **CORRIGÉ** | Schémas unifiés, le parent s'utilise comme sa propre ancre. |
| **7** | Écarts de formulation entre descriptions Agent et Serveur | S2 | **SOUS CONTRÔLE** | Couvert par `test_contract_parity.py` ; scripts d'export automatique dans `packages/dsh-tool-scalpel/scripts/`. |
| **8** | Numéros de ligne cités dans les refus exprimés sur le résultat | S3 | **CORRIGÉ (15/10)** | Clarifie explicitement `(in the block you read)` et `(of the resulting block)` avec numéro exact dans `actionable_hint`. |
| **9** | Lot `edits` refusé sans désigner l'index de l'entrée fautive | S3 | **CORRIGÉ** | Le refus nomme précisément l'index et reporte les clés reçues. |
| **10** | Replay redondant de builds identiques de fin de run | S2 | **CORRIGÉ** | Éprouvé via `code_fingerprint` ignorant les fichiers de documentation. |
| **11** | Stale-read accusant un changement imaginaire au lieu du propriétaire | S1 | **CORRIGÉ** | Recherche globale par hash qui nomme le bloc propriétaire réel. |
| **12** | Coût cumulé des refus d'outils | S1 | **CORRIGÉ** | Tombé à 0 refus dans 72 % des runs (18/25) en `v0.4.1`. |
| **13** | Fichiers sans grammaire (`.ps1`, `.txt`, `.gitignore`) refusés par `edit_block` | S1 | **CORRIGÉ (v0.4.1)** | Traités comme blocs `file_text` éditables en unitaire et en batch (`_file_text_block`). |
| **14** | Message ambigu de cache `build` après un build en échec (`build_fingerprint`) | S2 | **CORRIGÉ (15/10)** | Affiche `[NO CODE CHANGED SINCE LAST FAILED BUILD]` et invite à vérifier la visibilité (`internal`/`public`). |
| **15** | Crash `FieldInfo` et conseil circulaire sur `search_documents` | S1 | **CORRIGÉ** | Déballage défensif, parité schéma, tests de robustesse (13/10/2026). |
| **16** | Message `SPLIT it` sur JSON tronqué à une seule entrée (`len(edits) <= 1`) | S2 | **CORRIGÉ (15/10)** | Dans `benchmarks/agent.py`, précise `SPLIT it into smaller line operations (for a single-block edit, there is no edits[] list to split)`. |
| **17** | `insert_after: 0` en Markdown insérant du texte hors-titre avant le `# Heading` | S2 | **CORRIGÉ (v0.4.1)** | Garde préventif dans `_edit_block_locked` : 9/9 cas corrigés en 1 tour lors de la campagne `v0.4.1`. |
| **18** | `read_block` d'une section Markdown parente cachant ses sous-sections enfants | S2 | **CORRIGÉ (v0.4.1)** | `child_sections` exposé en JSON et ligne `children: N sub-section(s)` ajoutée en texte. |
| **19** | Faux positif du warning `_blank_boundary_lost` sur les sections Markdown | S3 | **CORRIGÉ (v0.4.1)** | Désactivé sur `section`/`document` où `update_document_section` garantit le séparateur. |
| **20** | Message `operation itself is unreadable` sur une plage hors bornes en batch | S3 | **CORRIGÉ (v0.4.1)** | Distinction explicite `out_of_bounds: True` avec invitation à utiliser le `numbered_text` frais. |
| **21** | Insertion `edit_block` silencieusement placée dans un code fence Markdown ouvert (`run4` v0.4.1) | S2 | **CORRIGÉ (15/10)** | Refus préventif dans `patcher.py::_edit_block_locked` avec indication de la ligne exacte du ` ``` ` fermant. |
| **22** | Référentiel de numérotation ambigu entre `search_symbols` (`L53-105` fichier) et `edit_block` (`1..53` bloc) (`run8` v0.4.1 & audits DSH) | S2 | **CORRIGÉ (15/10)** | Affiche `L53-105 (block lines 1..53)` dans `search_symbols(view='implementation')` et `[file L53-105]` dans l'en-tête `read_block`. |
| **23** | Duplication non détectée quand `replace` ré-émet une ligne séparée par des lignes blanches, ou faux positif intra-remplacement (`run9` v0.4.1 & `2459c797`) | S2 | **CORRIGÉ (15/10)** | Dans `_newly_repeated_line`, mesure `non_blank_gap <= 2` au-delà des lignes blanches et ignore les lignes dont l'original `was_at` faisait partie de la plage remplacée (`try`/`catch`). |
| **24** | Warning verbeux `carried no path` sur hash unique (`7 runs / 25` v0.4.1) | S2/S3 | **CORRIGÉ (15/10)** | Message allégé et aligné dans `server.py::edit_block`. |
| **25** | Cause d'accolade fermante surnuméraire noyée dans `note` et hint `use a text tool` sur namespace file-scoped (`run5` v0.4.1) | S2/S3 | **CORRIGÉ (15/10)** | `note` promu dans `actionable_hint` au top-level de `_syntax_error_payload`, et `use a text tool` remplacé par `use edit_block with insert_after on the last line` dans `_reject_duplicate`. |
| **26** | Plage `replace [a,b]` avalant un sous-item (`_heading_range_drops_a_bullet`) signalée en warning après écriture au lieu d'être refusée (`run14`, `run21` v0.4.1) | S2 | **CORRIGÉ (15/10)** | Converti en refus préventif sans écriture (`dropped_bullet_line`) dans `_edit_block_locked`. |
| **27** | Préfixe `[ok]` ambigu lors d'une création de sibling hors du bloc hôte (`written_outside_block` / `host_block_unchanged`) (`run12`, `run25` v0.4.1) | S2/S3 | **CORRIGÉ (15/10)** | Affiche `[ok] created sibling after <host_path> -> <new_id> (host block unchanged [h:...])` dans `lineops.py::format_single_edit_result`. |
| **28** | Faux positif `S1` dans `peeler.py` sur le refus légitime `Wrong block: that hash is the content_hash of ...` (`run20` v0.4.1) | S3 | **CORRIGÉ (15/10)** | Reclassé de `S1` vers `S2` dans `scalpel-benchmarks/benchmarks/peeler.py`. |
| **29** | Rebasage des chemins d'enfants Markdown dans un batch `edits=[...]` après renommage d'un titre parent (`session-4cc6` 15/10 & session courante) | S2 | **CORRIGÉ (15/10)** | Rebasage automatique des préfixes `parent::child` via `_batch_path_map` et héritage de `start_bytes` dans `_edit_batch`. |
| **30** | Perte du mot-clé `export` en ligne 1 de `read_block` JS/TS et absence de bloc `::imports` (`session-2a61` 14/10) | S2 | **CORRIGÉ (15/10)** | Span `export_statement` inclus sur les déclarations exportées uniques et bloc `::imports` extrait dans `typescript_parser.py`. |
| **31** | Instructions C# top-level (`Program.cs` sans classe) et fichiers MSBuild (`.csproj`/`.props`) non éditables en texte brut (`session-944f`, `session-2138` 14/10) | S2 | **CORRIGÉ (15/10)** | Extraction de `::top_level_statements` dans `csharp_parser.py`, lecture `::xml` et édition directe `edit_block` sur `.csproj`/`.props`/`.xml`. |
| **32** | Refus `hors_racine` lors de l'intervention sur un dépôt frère (`DataLinq.Enterprise`, `scalpel-experts`) (`6 bilans DSH` 13-15/10) | S2 | **EN RÉSERVE (Priorité 4.1)** | Évolution multi-root workspace différée sur décision produit. |
| **33** | Fichiers dans `reports/` ou `benchmarks/results/` masqués au listing et incohérents en lecture directe (`ignore_patterns`) (15/10) | S1/S2 | **CORRIGÉ (15/10)** | Dissociation de `discovery_exclude_patterns` (exclus de l'indexation auto et des recherches) et `ignore_patterns` dans `config.py` et `indexer.py`. |
| **34** | Fichiers texte/diff encodés en UTF-16LE (BOM `FF FE` produits par PowerShell) refusés comme non-UTF-8 (`5593592d`, `0ff61ffe`) | S3 | **CORRIGÉ (15/10)** | Décodage automatique des BOM UTF-16LE/BE et UTF-8 dans `_decode_text_bytes`. |

## 7. Historique des Correctifs Récents Validés (13/10/2026)

* **Déballage défensif des `FieldInfo` (`search_documents`)** : Correction du crash `TypeError: int() argument must be a string... not 'FieldInfo'` dans `database.py` et `server.py`.
* **Correction du contrat `search_documents`** : Suppression de `"required": ["query"]` dans `toolset_schemas.py` et levée de la troncature silencieuse à 8 documents sur requête vide.
* **Suppression du message d'erreur circulaire** : Dans `toolsets.py` (`_plain_tool_error`), suppression de la consigne toxique *"find it with search_documents"* en cas de panne de l'outil.
* **Validation unitaire** : 625/625 tests passés avec succès (`test_search_documents_robustness.py`).

* **Déploiement du Lot 1 (Fiabilisation des mutations & élimination SQL N+1)** :
  * **Chaînage FIFO des `insert_after` multiples** : Dans `lineops.py::apply_ops`, suppression du rejet artificiel `another insert already targets position N` et tri stable FIFO pour permettre des insertions multiples consécutives à la même ligne.
  * **Traitement tolérant des opérations `noop` et `ops: []`** : `ops: []` renvoie désormais un résultat `noop` non bloquant avec avertissement au lieu d'un échec ; les statuts `noop` sont reconnus et intégrés comme des réussites sans altération dans les formats texte et batch.
  * **Suppression du motif SQL N+1 dans `DatabaseManager.search_documents`** : Remplacement des requêtes individuelles par un unique `SELECT id, content FROM document_sections WHERE id IN (...)` pour l'ensemble des résultats inlinés.
  * **Préservation du saut de ligne terminal sur `insert_block`** : Préservation stricte de l'octet de fin de ligne dans `_create_child_of` lors de la création de fichier avec paramètre `content`.
  * **Validation globale** : 628/628 tests pytest réussis (2 nouveaux tests unitaires ajoutés dans `test_lineops.py` et `test_block_creation.py`).

* **Déploiement du Lot 2 (Optimisations documentaires, mémoïsation & contrôle de profondeur)** :
  * **Table virtuelle SQLite FTS5 `document_sections_fts`** : Création et synchronisation atomique sur `upsert_document` et `delete_document` (validé par `test_document_sections_fts_indexing_and_cleanup`).
  * **Mémoïsation du parsing Markdown (`markdown_layout`)** : Cache court `(abs_path, content_hash)` dans `Patcher._get_markdown_layout` évitant les I/O disque et parsings multiples lors de l'inlining des sections.
  * **Contrôle de profondeur `max_depth` sur `get_project_map`** : Filtrage paramétrable de l'arborescence projet par niveau de dossier (validé par `test_get_project_map_max_depth_filtering`).
  * **Validation globale** : 631/631 tests pytest réussis avec zéro régression.

* **Durcissement et Résistance des Lots 1 et 2 (Campagne d'audit QA & Stress-Tests)** :
  * **Comptabilisation des lots `noop` dans `_edit_batch`** : Les entrées `status: noop` sont isolées des `refused` ; un lot composé exclusivement d'opérations idempotentes n'est plus qualifié à tort en échec.
  * **Immunisation contre la limite physique `SQLITE_MAX_VARIABLE_NUMBER`** : La clause `WHERE id IN (...)` de `search_documents` est automatiquement découpée en blocs de 900 paramètres.
  * **Isolation défensive du cache Markdown** : `_get_markdown_layout` retourne des copies superficielles de dictionnaires, interdisant toute corruption du cache partagé par mutation de référence.
  * **Couverture de résistance** : 28 stress-tests créés dans `tests/test_stress_lots_1_and_2.py` (total : 659 tests unitaires et de stress, 100% PASS).
* **Refonte & Optimisation Complète de `build_context` (Lots 1 à 4 validés - 13/10/2026)** :
  * **Lot 1 (Enveloppe budgétaire globale & Calibration JSON)** : Plafonnement de `map` à 30 % du budget total demandé ; suppression de la duplication de 50 % de tokens dans le format JSON (`payload["context"]` retiré) ; calibration à 1.3 chars/token en JSON ; sécurisation de `breadth_ratio=0.0` (depth-first direct).
  * **Lot 2 (Performance & Latence divisée par 15)** : Latence ramenée de 2,9 s (et 12 s sur gros dépôts) à **< 100 ms** (88 ms mesuré) via *Single-Pass Deepening*, caches éphémères de cycle de vie (`_entity_cache`, `_file_bytes_cache`) et résolution d'adresses groupée anti-N+1 (`_batch_resolve_printed_refs`).
  * **Lot 3 (Hygiène de l'Index & Pondération Sémantique Anti-Bruit)** : Exclusion par défaut de `reports/`, `transcripts/`, `benchmarks/results/` ; modulation sémantique BM25 boostant le code source applicatif (x1.5) devant les sections doc (x1.0) et les propriétés JSON (x0.3) ; diversité préservée par `max_entries_per_file = 5` par défaut.
  * **Lot 4 (Restitution Élégante au Niveau 3 - Graceful Truncation)** : Découpage strict aux frontières de lignes (`\n`) évitant les identifiants tronqués ; notice explicite avec ratio de lignes affichées (`K/N lines shown`) et invitation à utiliser `read_block`.
  * **Validation globale** : 682/682 tests pytest réussis (100% PASS, 0 régression).
* **Déploiement du Lot 4 (Démonisation / Persistance du Pont Python DSH — Élimination du Cold-Start par appel - 13/10/2026)** :
  * **Mode démon NDJSON (`packages/dsh-tool-scalpel/lib/runner.py`)** : Extraction de `dispatch_command(server, cfg, tool_schemas, cmd, args, mtime_cache, loop)` avec conservation à 100 % du mode CLI one-shot ; prise en charge des entrées de lot `args["edits"]` ; cache `(st_mtime_ns, st_size)` évitant les réindexations redondantes, invalidation post-mutation et nettoyage via `server.indexer.remove_file` en cas de suppression disque ; boucle `sys.stdin.readline()` sans buffering bloquant et isolation stricte de `sys.stdout`.
  * **Client démon persistant (`packages/dsh-tool-scalpel/lib/bridge.js`)** : Multiplexage NDJSON avec `ensureDaemon()` et `closeDaemon()`, gestion symétrique `ref()`/`unref()` + `pause()`/`resume()` des flux `stdio` lorsque `pendingRequests.size === 0` (zéro blocage de `node --test`), et résilience automatique sur timeout ou crash.
  * **Performance mesurée (`packages/dsh-tool-scalpel/test/bridge.test.mjs`)** : Latence chaude mesurée à **< 10 ms par appel** (contre > 200 ms en cold-start).
* **Correctifs d'ergonomie post-campagne v0.4.0 (15 runs épluchés - 14/10/2026)** :
  * **Garde préventif `insert_after: 0` sur section Markdown (`patcher.py::_edit_block_locked`)** : Refuse toute insertion `insert_after: 0` de contenu non-titre sur une section Markdown (qui plaçait le texte avant la ligne 1 `# Heading` et scindait la section en doublon `(2)`, coûtant 8 à 9 tours de réparation aux runs `r2` et `r11`), et guide immédiatement vers `insert_after: 1`.
  * **Visibilité des sous-sections enfants dans `read_block` (`patcher.py::_read_block_locked` & `server.py`)** : Lorsqu'une section Markdown possède des sous-sections directes, `read_block` expose `child_sections` et affiche `children: N sub-section(s) under this heading (...)`, évitant les collisions de titres lors d'insertions ultérieures (runs `r4` et `r15`).
  * **Suppression du faux positif `_blank_boundary_lost` en Markdown (`patcher.py`)** : Ne déclenche plus l'avertissement de perte de ligne blanche séparatrice sur les sections Markdown dont le séparateur est géré nativement par `update_document_section` (run `r13`).
  * **Clarification des erreurs hors bornes en batch (`patcher.py::_batch_entry`)** : Sépare les plages hors bornes (`out_of_bounds: True`) des opérations malformées (`unreadable_operation: True`), et extrait proprement le message d'erreur JSON multi-lignes dans `peeler.py`.
* **Campagne de validation complète v0.4.1 (25 runs épluchés : `run1` à `run25` - 14/10/2026) & Pistes d'amélioration consolidées (Anomalies #21 à #28)** :
  * **Résultats mesurés sur 25 runs (`v0.4.1` n=25 vs `v0.4.0` n=15)** :
    * **Taux de succès** : **100 % PASS (25/25)** (`build_ok` 25/25, `docs_ok` 25/25), **0 anomalie S1 avérée** sur les 25 audits qualificatifs indépendants.
    * **Tours médians** : **19 tours** sur l'ensemble des 25 runs (**-26,9 % vs 26 tours en v0.4.0**, moyenne 20,4, IQR 17–23, min **11 tours** sur `run15`), et **18,0 tours de médiane** sur les 10 derniers runs (`run16` à `run25`, IQR 17–21).
    * **Appels d'outils médians** : **23 appels** (**-25,8 % vs 31 en v0.4.0**, moyenne 23,7, et 22,5 sur `run16..25`).
    * **Tokens totaux médians** : **1 343 408 tokens** (**-34,0 % vs 2 035 617 en v0.4.0**).
    * **Cache miss médians (facturés plein tarif)** : **20 051 tokens** (**-33,3 % vs 30 053 en v0.4.0**) ; **tokens effectifs médians : 83 821** (-29,3 % vs 118 529) ; **coût réel moyen : 0,0133 $** (-24,4 % vs 0,0176 $).
    * **Temps d'exécution médian** : **92,1 s de temps agent** (**-15,6 % vs 109,1 s** en v0.4.0 ; **83,2 s** sur `run16..25`) et **117,2 s de temps total** (-15,3 % vs 138,5 s).
    * **Runs sans aucun refus (`peeler.py`)** : **18 / 25 runs (72,0 %)** contre 3 / 15 (20,0 %) en v0.4.0.
  * **Validation terrain du garde `insert_after: 0` (#17) sur 25 runs** : Déclenché dans **9 runs sur 25** (`run2`, `run3`, `run5`, `run6`, `run10`, `run15`, `run19`, `run21`, `run23`) ; **100 % des cas (9/9) ont été corrigés dès le tour suivant (+1 tour)** grâce au hint `Use insert_after: 1`, éliminant totalement les boucles de 8 à 9 tours de réparation observées en v0.4.0.
  * **Contexte technique détaillé pour reprise future des anomalies #21 à #28 (extraites des 25 éplucheurs v0.4.1)** :
    1. **#21 — Détection d'insertion dans un code fence ouvert (`run4` t10→t12, coût : 2 tours)** :
       * *Symptôme* : L'agent appelle `edit_block` avec `insert_after: 30` sur une section Markdown dont les lignes 23 à 31 forment un bloc ` ```csharp ... ``` `. Le contenu inséré (qui contient lui-même un bloc ` ```csharp `) se retrouve imbriqué avant le ` ``` ` fermant de la ligne 31, repoussé en ligne 63.
       * *Point d'ancrage code* : `scalpel-mcp/src/ast_graph_server/patcher.py::_edit_block_locked` et `documents.py::_unclosed_fence`. Avant d'appliquer un `insert_after: n` sur un bloc `section`/`document`, compter les fences ` ``` ` des lignes `1..n` : si un fence est ouvert à la ligne `n` et que le `content` inséré contient un marqueur de fence ou un titre Markdown, refuser ou émettre un warning explicite.
    2. **#22 — Clarification du référentiel de lignes dans `search_symbols(view='implementation')` (`run8` t08→t09, coût : 1 tour)** :
       * *Symptôme* : `search_symbols` affiche dans la ligne d'en-tête `L53-105 | hash 2e160bab` (numéros de lignes dans le fichier), puis affiche le code numéroté `1..53`. L'agent envoie `insert_after: 66` à `edit_block`, qui refuse (`0..53`).
       * *Point d'ancrage code* : `scalpel-mcp/src/ast_graph_server/rendering.py` / `server.py` (formatage texte de `search_symbols`). Remplacer `L{start}-{end}` par `file L{start}-{end} (block 1..{count})` pour lever toute ambiguïté.
    3. **#23 — Extension du garde anti-duplication `_adjacent_duplicate` au-delà des lignes blanches (`run9` t16→t18, coût : 2 tours)** :
       * *Symptôme* : En `t16`, `replace: [3, 3]` remplace la ligne 3 par 5 lignes se terminant par `2. **User-Agent Telemetry Injection:**`. Les lignes 4 à 6 du bloc étant vides et la ligne 7 contenant déjà `2. **User-Agent Telemetry Injection:**`, le garde `_adjacent_duplicate` (qui ne regarde que `b + 1 = 4`) laisse passer le doublon.
       * *Point d'ancrage code* : `scalpel-mcp/src/ast_graph_server/patcher.py::_adjacent_duplicate`. Lors de la vérification des frontières amont (`first - 1`) et aval (`last + 1`), sauter les lignes purement blanches pour comparer la première/dernière ligne non vide du `content` avec la première ligne non vide adjacente hors plage.
    4. **#24 — Fallback `top-level path` dans `edits[]` et suppression du warning `carried no path` sur hash unique (`run4`, `run6`, `run10`, `run18`, `run20`, `run21`, `run25`, coût : 0 à 1 tour)** :
       * *Symptôme* : (a) Quand `path` est passé au top-level d'`edit_block` avec `edits=[...]` (>1 entrée) et qu'une entrée omet `path`, l'outil émet à la fois `top-level path was IGNORED` et refuse l'entrée avec `top-level path cannot fill it`. (b) Quand une entrée omet `path` mais fournit un `expected_hash` unique (ou dans certains cas de lot mixte avec `top-level path`), l'outil applique l'édition tout en émettant un warning `carried no path` qui contredit le schéma (`or an expected_hash that uniquely identifies the block`) et revient dans **7 runs sur 25**.
       * *Point d'ancrage code* : `scalpel-mcp/src/ast_graph_server/patcher.py::_edit_batch`. Utiliser `top_level_path` comme défaut si une entrée de `edits` n'a ni `path` ni `expected_hash` résoluble, et supprimer totalement le warning `carried no path` quand `expected_hash` résout sans ambiguïté un bloc unique.
    5. **#25 — Promotion de `note` dans `actionable_hint` sur accolade surnuméraire et hint namespace file-scoped (`run5` t06→t09, coût : 2 tours)** :
       * *Symptôme* : (a) Quand un `edit_block` C# ré-émet l'accolade fermante `}` finale du bloc, la cause exacte est calculée dans `note` mais `error` n'affiche que `Syntax error near '}'`. (b) Quand `insert_block` échoue pour `duplicate declaration` sur un fichier utilisant un `namespace X;` file-scoped, le hint suggère `use a text tool` (indisponible en mode Scalpel).
       * *Point d'ancrage code* : `scalpel-mcp/src/ast_graph_server/patcher.py` (`_syntax_error_payload` et `insert_block`). Préfixer `actionable_hint` par `note` lorsqu'elle est présente, et remplacer la mention `use a text tool` par `use edit_block with insert_after on the last line of the target symbol`.
    6. **#26 — Refus préventif sur `_sub_item_lost` lors d'un `replace [a,b]` destructeur (`run14` t17, `run21` t16→t17, coût : 1 tour)** :
       * *Symptôme* : En `run21` (`t16`), l'agent envoie `replace: [22, 23]` avec uniquement un titre d'item numéroté (`4. **Unity Catalog...**:`), ce qui écrase la puce indentée située à la ligne 23. `patcher.py::_sub_item_lost` détecte exactement l'erreur (`warning: the replaced range covered line 23, a bullet of the titled item above it ... the bullet is GONE`), mais laisse l'écriture s'appliquer, obligeant l'agent à dépenser un tour (`t17`) pour restaurer la puce.
       * *Point d'ancrage code* : `scalpel-mcp/src/ast_graph_server/patcher.py::_edit_block_locked` (autour de l'appel `_sub_item_lost`). Transformer `_sub_item_lost` en garde bloquant (refus sans écriture avec `actionable_hint: "Shrink the replace range to [a, b-1] or include the sub-item in your replacement content"`), à l'image de `_adjacent_duplicate`.
    7. **#27 — Clarification de l'en-tête texte lors d'une création de sibling hors du bloc hôte (`run12` t20, `run25` t10, coût : 0 à 1 tour)** :
       * *Symptôme* : Lorsqu'un `edit_block` sur la dernière ligne d'un bloc crée un sibling après le bloc (`written_outside_block: True` ou `host_block_unchanged: True`), le rendu texte affiche `[ok] <host_path> [h:<old_hash>] (4 lines)` suivi de `warning: the new lines are NOT inside <host_path>: they landed outside it`. Les éplucheurs (`run12`, `run25`) relèvent l'apparente contradiction entre l'en-tête `[ok]` sur le bloc hôte inchangé et le warning.
       * *Point d'ancrage code* : `scalpel-mcp/src/ast_graph_server/server.py` (formatage texte de `edit_block`). Quand `host_block_unchanged` ou `written_outside_block` est vrai, formater l'en-tête comme `[ok] created sibling after <host_path> -> <created_section_id> [h:<new_hash>] (host block unchanged [h:<host_hash>])`.
    8. **#28 — Reclassification de `Wrong block: that hash is the content_hash of` en `S2` dans `peeler.py` (`run20` t06, coût : 1 tour)** :
       * *Symptôme* : En `run20` (`t06`), l'agent passe par erreur le `expected_hash` d'un autre bloc (`ReadOptions::Complete`) sur `SparkConnectOptions`. L'outil refuse proprement en nommant le vrai propriétaire du hash et en fournissant le hash frais de `SparkConnectOptions`, ce qui permet à l'agent de réussir dès `t07`. Cependant, `peeler.py` classe encore la sous-chaîne `"Wrong block:"` en `S1` (héritage de l'époque où le message accusait un changement imaginaire, anomalie #11), alors que l'agent d'audit confirme qu'il s'agit d'une auto-erreur de l'agent parfaitement guidée (`0 S1`).
       * *Point d'ancrage code* : `scalpel-benchmarks/benchmarks/peeler.py`. Retirer `"Wrong block"` de la liste des motifs `S1` automatiques (le classer en `S2` `hash_mismatch`).

## 8. Chantier Efficacité Agent : Réduction de la Friction sur la Lecture et l'Écriture Groupées

Ce chantier répond au constat empirique mesuré sur les sessions DSH et consolidé par la campagne de référence de 10 runs (`reference_series_10`, 13/10/2026) :
- Les agents adoptent la lecture groupée (`paths=[...]`) dès lors qu'elle est explicitement ordonnée dans le prompt (atteignant **61,2 %** de lectures groupées sur le benchmark contre < 5 % dans les sessions interactives DSH non outillées).
- En revanche, l'écriture groupée (`edit_block(edits=[...])`) reste plafonnée à **21,9 %** et subit un taux de rejet élevé en raison de verrous syntaxiques stricts dans le moteur de patch.

### A. Incitation et fluidification de la lecture groupée (`paths=[...]`) — Fait (13/10/2026)
* **Faits établis (Campagne 13/10/2026 & v0.4.1)** :
  * Adoption massive de `read_block(paths=[...])` tant en benchmark que dans les 43 sessions DSH (où des lots de 10 à 80 blocs sont lus en un seul tour).
* **Actions réalisées** :
  1. **Contrat DSH** : Prompt de session DSH aligné (`read_block(paths=[...]) reads several blocks in ONE call`).
  2. **Indice d'action dans les retours de listing** : Implémenté dans `patcher.py::_file_outline` (`Call read_block(paths=['<file>::sym1', ...]) to read multiple blocks in one turn`) et `_read_block_locked`.

### B. Simplification et fiabilisation de l'écriture multi-blocs (`edit_block`) — Fait (13/10/2026)
* **Faits établis & Découverte de l'Éplucheur Automatique (Runs r1, r2, r8)** :
  * **Cas d'échec n°1 : Conflit d'insertion à même position** (`another insert already targets position N`) — **CORRIGÉ** :
    * Les opérations `insert_after` multiples ciblant le même point d'ancrage sont désormais chaînées séquentiellement en ordre FIFO (`apply_ops`), éliminant tout rejet bloquant.
  * **Cas d'échec n°2 : Boucle de répétition sur refus noop** (`identical content / status: noop`) — **CORRIGÉ** :
    * Les retours `noop` sont traités comme des succès sans effet (`status: noop` rendu comme `[ok] (unchanged)`) sans faire échouer les batches ni désorienter l'agent.
  * **Cas d'échec n°3 : Conflit d'opérations vides (`ops is empty`)** — **CORRIGÉ** :
    * Une entrée avec `ops: []` est traitée proprement comme un `noop` sans erreur, préservant l'intégrité du bloc et du lot d'éditions.

### C. Automatisation de l'Épluchage Déterministe (`benchmarks/peeler.py`) — Fait (13/10/2026)
* L'épluchage de chaque run n'est plus une contrainte contractuelle humaine : il est câblé nativement dans le runner (`benchmarks/runner.py`) et produit automatiquement un bilan JSON structuré (`<run>.peel.json`) sans aucun surcoût LLM.

### D. Refonte de `get_project_map` (Lots 1 & 2 validés, Lot 3 en réserve d'évolution) — Fait (13/10/2026)
* **Lot 1 — Moteur de sélection, centralité & vases communicants (Fait)** :
  * Désenclavement des fonctions pures et modules sans classe (`parent_id IS NULL`).
  * Calcul de centralité via appels entrants indexés (`symbol_calls`), volume (LOC) et scoring de chemin.
  * Vases communicants budgétaires : le quota documentation non consommé (40 %) est intégralement reversé au budget code, garantissant 100 % d'utilisation utile du budget.
  * Déduplication du titre Markdown H1 en tête de section.
* **Lot 2 — Rendu AST canonique avec adresses `::` par ligne (Fait)** :
  * Élimination définitive des listes de sections plates à virgules et parenthèses ambiguës.
  * Restitution hiérarchique avec syntaxe canonique d'adressage `::` sur chaque ligne, prête pour les appels d'outils Scalpel.
  * Métriques de volume intégrées (`{loc}L`, `{sym_count} symboles`) et élagage anti-encombrement (`::... (+N autres symboles/sections)`).
* **Lot 3 — Contrôle de la profondeur AST (`ast_depth`) (En réserve d'évolution)** :
  * Possibilité d'exposer un paramètre `ast_depth: int` (0 = squelette, 1 = conteneurs/racines, 2 = méthodes détaillées) si le besoin d'affiner la granularité interne des très gros fichiers (> 7 000L) est empiriquement prouvé lors des prochaines campagnes. Consigné en réserve d'évolution.

### E. Évolution de l'insertion groupée multi-blocs (`insert_block(inserts=[...])`) — Fait (13/10/2026)

* **Origine & Objectif** :
  * Permettre l'insertion séquentielle de plusieurs blocs sous un même parent (`parent='...', inserts=[{anchor, text}, ...]`) ou sous plusieurs parents distincts (`inserts=[{parent, anchor, text}, ...]`) en un seul appel.
* **Solution technique & Validée** :
  * Ajout du paramètre `inserts` dans `server.py::insert_block`, `toolset_schemas.py` et `agent_schemas.json`, avec héritage optionnel de `parent` et `anchor` au top-level, rapport précis de l'index fautif en cas d'entrée incomplète, et validation par `test_block_creation.py` et `test_contract_parity.py`.

### F. Évolution des Noms d'Outils au Pluriel (`read_blocks`, `edit_blocks`, `delete_blocks`) — Feuille de Route Future

* **But visé & Biais des LLMs** :
  * Les modèles LLMs associent naturellement les verbes singuliers (`read_block`, `edit_block`) à une action unitaire, réprimant l'utilisation spontanée des formes groupées (`paths=[...]`, `edits=[...]`).
  * Le passage au pluriel (`read_blocks`, `edit_blocks`, `delete_blocks`) induit structurellement un réflexe de manipulation groupée sans surcoût de prompt.
* **Impact sur le repository `scalpel-mcp`** :
  * Mise à jour des décorateurs `@self.mcp.tool` dans `server.py`, de `toolset_schemas.py` et des messages de diagnostic dans `patcher.py`.
  * Mise en œuvre du mécanisme de *Shadow Aliasing* pour garantir l'interception transparente des anciens noms singuliers sans rupture.
* **Articulation avec le banc `scalpel-benchmarks`** :
  * Une fois la bascule actée sur `scalpel-mcp`, un chantier dédié sera lancé sur `scalpel-benchmarks`.
  * Ce chantier disposera de sa propre autonomie pour décider des adaptations à entreprendre (prompts de workflow, expressions régulières d'épluchage de traces `peeler.py`, et tests de surface de banc).

