# Backlog Technique & Feuille de Route : Moteur Scalpel (`scalpel-mcp`)

Dernière mise à jour : 13/10/2026  
Statut : Document de référence unifié — Orientations stratégiques, chantiers moteur et roadmap produit.

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

### A. Migration du moteur documentaire vers SQLite FTS5 + BM25 natif (Gain : x20 à x50) — S1 / Critique
* **Symptôme & Mesure** : `DatabaseManager.search_documents` (`scalpel-mcp/src/ast_graph_server/database.py`) effectue un table scan linéaire complet via `WHERE (s.heading LIKE ? OR s.content LIKE ?)` avec boucles Python pour compter manuellement les occurrences de chaque terme.
  * Sur 1 000 sections : **56 ms à 376 ms** par requête.
  * Sur 50 000+ sections : latence de **plusieurs secondes par appel**, bloquant l'agent.
* **Solution technique** :
  1. Créer une table virtuelle SQLite FTS5 (`document_sections_fts`) synchronisée via des triggers sur `document_sections` (`INSERT`, `UPDATE`, `DELETE`).
  2. Configurer le tokeniseur `unicode61` (insensible à la casse et aux accents).
  3. Utiliser la fonction native SQLite `bm25(document_sections_fts, 10.0, 1.0)` pour le ranking direct en base de données.
* **Gain attendu** : Latence divisée par **20 à 50** (< 2 ms sur 1 000 sections, < 15 ms sur 50 000+ sections), gestion native du stemming et calcul de pertinence TF-IDF formel.

### B. Suppression du motif SQL N+1 sur l'inlining des sections (Gain : x5 à x10) — S1
* **Symptôme & Mesure** : La première requête de ranking n'extrait qu'un snippet de 240 caractères (`substr(s.content, 1, 240)`). Dès que des sections doivent être inlinées (`full_top` ou `include_content=True`), le code exécute un `SELECT` individuel via `get_document_section(item["section_id"])` pour chaque match retourné. Pour `limit=100`, cela génère **100 requêtes SQL séquentielles**.
* **Solution technique** : Charger directement le corps complet dès la première requête pour les rangs `<= full_top`, ou rapatrier l'ensemble des contenus en une seule requête groupée via `WHERE s.id IN (...)`.
* **Gain attendu** : Élimination de 99 % des allers-retours SQL lors de l'inlining.

### C. Mémoïsation du parsing Markdown (`markdown_layout`) dans le Patcher (Gain : x5) — S2
* **Symptôme & Mesure** : Pour calculer les métriques de bloc (`content_hash`, `line_count`, `numbered_text`) des sections inlinées, `server.py` appelle `self.patcher.block_metrics(displayed)`. Le Patcher relit le fichier physique sur disque et réexécute l'intégralité du découpage syntaxique Markdown (`markdown_layout`). Si 20 sections proviennent du même document (ex. `LINQ-to-Spark.md`), le document est relu et re-parsé 20 fois de suite dans le même tour d'agent.
* **Solution technique** : Mettre en cache (mémoïsation courte par `(file_path, content_hash)`) la structure Markdown dans le Patcher durant le cycle de vie de la requête.
* **Gain attendu** : Division par 5 du temps de calcul sur les documents volumineux et suppression des I/O disque redondantes.

### D. Interdiction absolue du cache de build figé après un échec (`build_fingerprint` toxique) — S1 / Critique
* **Symptôme & Mesure (Run 1 de `campaign_v3`)** : Lorsqu'un build a échoué (14 erreurs C#), l'agent a corrigé son code mais le harnais a répondu `build FAILED (no compilation) -- answered from the previous build`. L'agent a brûlé 14 tours à contourner ce faux cache en insérant des lignes blanches dans les directives `using` pour modifier artificiellement le fingerprint du build.
* **Règle impérative** : Si le build précédent a échoué (`success == False` ou `errors > 0`), **toute nouvelle demande de build doit impérativement forcer la recompilation réelle**.

---

## 3. Chantiers Moteur & AST en cours (`src/`)

Chantiers issus du rapport des trous d'accès et des audits de performance du moteur :

1. **Harmonisation YAML et JSON avec Tree-sitter (F7 / E2) — S1**
   * *Constat* : `read_block("fichier.yml")` ou `read_block("fichier.json")` renvoie un objet `_structured_summary` obsolète sans `children` ni `actionable_hint` vers les symboles. L'agent croit le fichier non adressable et tente des voies de repli.
   * *Correctif* : Brancher les fichiers YAML et JSON sur `_file_outline` dans `patcher.py::_unresolved_block` pour exposer la hiérarchie de clés (`services.web`, `presets[0].name`) sous forme d'arbre AST navigable et éditable par bloc.
   * *Spécification* : Traiter les clés comme des symboles adressables, et expliciter dans la documentation que les valeurs scalaires se modifient via leur clé parente.

2. **Préservation du saut de ligne final dans `insert_block` (D5 / F3) — S2**
   * *Constat* : Un fichier créé avec `insert_block(content)` perd son saut de ligne terminal (`\n`) en raison d'un `.strip("\n")` trop agressif dans `patcher.py::_split_block_text` (mesuré : 58 octets envoyés -> 57 octets écrits).
   * *Correctif* : Conserver strictement l'octet de fin de ligne présent dans l'argument `content` lors de la création d'un fichier.

3. **Nettoyage des messages d'erreur et des indices du moteur (E3 / F2) — S2**
   * Supprimer les messages contradictoires : `_cannot_receive_a_child` annonce parfois `insert_block(parent=fichier) creates one` alors que l'opération est refusée pour un fichier de code.
   * Si un parent est un fichier vide indexé, proposer explicitement l'insertion de racine ou l'initialisation de symbole plutôt qu'un appel circulaire à `search_symbols`.

## 4. Chantiers Plugin DSH & Surface Unifiée (`packages/dsh-tool-scalpel`)

Orientations d'architecture actées le 10-11/10/2026 pour le pont Node.js / DSH :

1. **Priorité 1 = Lot P (Parité Plugin DSH = Serveur MCP)** :
   * *Constat mesuré* : `runner.py` appelait le patcheur et la base en direct sans passer par l'API unifiée MCP. En conséquence, le plugin DSH perdait le support du batch `edits=[...]`, la réponse compacte d'`edit_block`, le paramètre `view` de `search_symbols`, `full_top` de `search_documents`, et les descriptions enrichies du serveur.
   * *Objectif* : Aligner strictement le plugin DSH sur la surface canonique du serveur MCP afin d'éviter toute dérive d'interface entre l'environnement de benchmark et l'environnement de production.

2. **Gouvernance des Droits et Bac à Sable DSH (Lot F / D2)** :
   * *Règle tranchée* : Alignement sur le jeu plat, même permissivité, ni plus ni moins. Les outils AST doivent respecter les politiques de bac à sable (`sandboxPolicy`, racines inscriptibles, escalade par `approval` en `allowed-once`, politique d'observation).
   * Les mutations AST (`edit_block`, `insert_block`, `delete_block`) doivent vérifier les permissions de fichier avant toute écriture physique sur disque.

3. **Granularité Fine pour JavaScript et TypeScript** :
   * Élargir le découpage Tree-sitter au-delà des classes et fonctions de premier niveau pour adresser les fonctions imbriquées, les méthodes d'objets littéraux complexes et les exports déstructurés en tant que symboles distincts avec hash autonome.

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

## 6. Registre Consolidé des 16 Anomalies d'Ergonomie et de Friction

Suivi qualité systématique des anomalies signalées par les agents lors des campagnes :

| # | Anomalie / Friction | Classe | Statut | Synthèse & Action |
|---|---|:---:|:---:|---|
| **1** | Plafonnement silencieux de `search_symbols(view='implementation')` | S2 | **CORRIGÉ** | Le plafond est désormais le `limit` explicite de l'appelant. |
| **2** | `read_block` sur `.md` sans préambule rend le hash du vide (`e3b0...`) | S3 | **OUVERT** | Rendre le hash du fichier entier au lieu du hash vide non discriminant. |
| **3** | `list_documents` avec message d'absence auto-contradictoire | S3 | **CORRIGÉ** | Absorbé dans `search_documents`, messages reformulés. |
| **4** | `edit_block` dupliquant les lignes frontières sur plages inclusives | S2 | **CORRIGÉ** | Refus préventif immédiat avec mention des lignes dupliquées (`INCLUSIVE`). |
| **5** | `insert_block` renumérotant les sections frères et surprenant l'agent | S3 | **SURVEILLANCE** | Comportement documenté dans l'outil, suivi en campagne. |
| **6** | Paramètre obsolète `where="start"` tenté par les agents | S2 | **CORRIGÉ** | Schémas unifiés, le parent s'utilise comme sa propre ancre. |
| **7** | Écarts de formulation entre descriptions Agent et Serveur | S2 | **SOUS CONTRÔLE** | Couvert par `test_contract_parity.py` ; convergence vers source unique. |
| **8** | Numéros de ligne cités dans les refus exprimés sur le résultat | S3 | **OUVERT** | Préférer citer les lignes du texte actuel pour les refus. |
| **9** | Lot `edits` refusé sans désigner l'index de l'entrée fautive | S3 | **CORRIGÉ** | Le refus nomme précisément l'index et reporte les clés reçues. |
| **10** | Replay redondant de builds identiques de fin de run | S2 | **CORRIGÉ** | Éprouvé via `code_fingerprint` ignorant les fichiers de documentation. |
| **11** | Stale-read accusant un changement imaginaire au lieu du propriétaire | S1 | **CORRIGÉ** | Recherche globale par hash qui nomme le bloc propriétaire réel. |
| **12** | Coût cumulé des refus d'outils (9,4 % des tours perdus) | S1 | **EN RÉDUCTION** | Diminution des rejets syntaxiques et fiabilisation des indices d'action. |
| **13** | Limitations structurelles ayant poussé à l'usage du shell | S1 | **INTÉGRÉ** | Fichiers sans grammaire traités comme blocs uniques dans Scalpel. |
| **14** | Faux "no compilation" sur build après échec (`build_fingerprint`) | S1 | **CRITIQUE (À FAIRE)** | Tout build suivant un échec doit forcer la recompilation réelle. |
| **15** | Crash `FieldInfo` et conseil circulaire sur `search_documents` | S1 | **CORRIGÉ** | Déballage défensif, parité schéma, 625/625 tests passés (13/10/2026). |
| **16** | Fausse alerte de limite de taille sur `edit_block` (coupure LLM) | S2 | **À CLARIFIER** | Reformuler l'erreur de troncature JSON sans ordonner de découpage abusif. |

## 7. Historique des Correctifs Récents Validés (13/10/2026)

* **Déballage défensif des `FieldInfo` (`search_documents`)** : Correction du crash `TypeError: int() argument must be a string... not 'FieldInfo'` dans `database.py` et `server.py`.
* **Correction du contrat `search_documents`** : Suppression de `"required": ["query"]` dans `toolset_schemas.py` et levée de la troncature silencieuse à 8 documents sur requête vide.
* **Suppression du message d'erreur circulaire** : Dans `toolsets.py` (`_plain_tool_error`), suppression de la consigne toxique *"find it with search_documents"* en cas de panne de l'outil.
* **Validation unitaire** : 625/625 tests passés avec succès (`test_search_documents_robustness.py`).

## 8. Chantier Efficacité Agent : Réduction de la Friction sur la Lecture et l'Écriture Groupées

Ce chantier répond au constat empirique mesuré sur les sessions DSH et consolidé par la campagne de référence de 10 runs (`reference_series_10`, 13/10/2026) :
- Les agents adoptent la lecture groupée (`paths=[...]`) dès lors qu'elle est explicitement ordonnée dans le prompt (atteignant **61,2 %** de lectures groupées sur le benchmark contre < 5 % dans les sessions interactives DSH non outillées).
- En revanche, l'écriture groupée (`edit_block(edits=[...])`) reste plafonnée à **21,9 %** et subit un taux de rejet élevé en raison de verrous syntaxiques stricts dans le moteur de patch.

### A. Incitation et fluidification de la lecture groupée (`paths=[...]`) — S2 / Fort Impact
* **Faits établis (Campagne 13/10/2026)** :
  * 60 lectures groupées vs 38 unitaires observées quand le prompt système porte la directive directe *« Pass ALL the identifiers you need to read_block(paths=[...]) in ONE call »*.
  * Dans DSH, l'absence de cette directive conduit les agents à un parcours séquentiel coûteux (1 tour par symbole).
* **Actions Moteur & Contrat** :
  1. **Contrat DSH** : Aligner le prompt de session DSH sur celui du benchmark pour ordonner le regroupement dès l'identification d'une liste de symboles ou de sections.
  2. **Indice d'action dans les retours de listing** : Dans `read_block`, lorsqu'un listing de membres de code ou de plan Markdown est renvoyé, enrichir la réponse avec un rappel actionnable : `Call read_block(paths=['<file>::sym1', '<file>::sym2']) to read multiple members in one turn`.

### B. Simplification et fiabilisation de l'écriture multi-blocs (`edit_block`) — S1 / Prioritaire
* **Faits établis & Découverte de l'Éplucheur Automatique (Runs r1, r2, r8)** :
  * **Cas d'échec n°1 : Conflit d'insertion à même position** (`another insert already targets position N`).
    * *Symptôme observé (r1 t12, r1 t13)* : L'agent envoie deux opérations `insert_after` ciblant la même ligne (ex: ajouter deux sections ou blocs consécutifs). Le moteur Scalpel rejette la 2e opération, forçant l'agent à dépenser 2 tours supplémentaires pour re-découper son batch.
    * *Action* : Autoriser le chaînage d'insertions multiples au même point d'ancrage en les ordonnant séquentiellement (FIFO/LIFO prévisible) plutôt que de lever un conflit bloquant.
  * **Cas d'échec n°2 : Boucle de répétition sur refus noop** (`identical content / status: noop`).
    * *Symptôme mesuré (r8, tours 50 à 80)* : L'agent tente d'éditer un bloc déjà modifié ou identique, reçoit une réponse partielle ou un `noop`, et boucle pendant 30 tours consécutifs jusqu'au plafond `max_turns` (80 tours perdus, coût > 11M tokens).
    * *Action* : Si une opération produit un `noop` (contenu déjà identique), renvoyer explicitement `status: success` ou un avertissement non bloquant plutôt qu'un statut d'erreur ambigu qui désoriente le raisonnement de l'agent.
  * **Cas d'échec n°3 : Conflit d'opérations vides (`ops is empty`)**.
    * *Symptôme (r2 t16)* : Un lot d'éditions dont une entrée a `ops: []` fait échouer l'entrée au lieu de l'ignorer proprement.
    * *Action* : Traiter une entrée `ops: []` comme un noop valide avec avertissement.

### C. Automatisation de l'Épluchage Déterministe (`benchmarks/peeler.py`) — Fait (13/10/2026)
* L'épluchage de chaque run n'est plus une contrainte contractuelle humaine : il est câblé nativement dans le runner (`benchmarks/runner.py`) et produit automatiquement un bilan JSON structuré (`<run>.peel.json`) sans aucun surcoût LLM.
