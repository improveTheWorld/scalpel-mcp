# Rapport d'Audit Forensique : Outils de Découverte & Recherche (`build_context`, `get_project_map`, `search_symbols`, `search_documents`)

**Agent Auditeur** : `6a543351-3cfa-4b6c-96cc-603d6833585a`  
**Capsule Créée** : `scalpel-mcp.engine.discovery-search`  
**Règle d'Or** : *« Un outil qui sait exécuter la demande d'un agent d'une façon déterministe ne doit JAMAIS la refuser, ni causer des tours supplémentaires (extra turns). »*

---

## 1. Synthèse Exécutive des Constats Majeurs (Classés par Impact sur les Tours Agent)

| ID | Sévérité | Outil(s) | Fichier & Symbole (Lignes) | Problème identifié (Cause d'extra turns ou de faux négatifs) | Gain déterministe attendu |
|---|:---:|---|---|---|---|
| **F-01** | **CRITIQUE (S1)** | `build_context` | `context_builder.py:117-160` (`_stamped_entry`), `context_builder.py:167-219` (`_symbol_text`, `_section_text`), `context_builder.py:725-762` (`_to_text`) | **Troncature Niveau 3 supprimant `content_hash` + En-tête privé de `[file Lstart-end]`** : (1) Dès que `len(numbered) > 2000` (~35-45 lignes numérotées), `_stamped_entry` supprime `content_hash` et oblige un `read_block` ; (2) l'exemption `line_count <= 120` dans `_symbol_text` ne couvre ni les sections Markdown (`_section_text`) ni les symboles dont le nom complet n'est pas littéralement dans `query` ; (3) `_to_text` n'affiche ni `({line_count} lines)` ni `[file L{start}-{end}]` (cause racine de la confusion fichier vs bloc `#51`). | **-1 à -3 tours / run** (0 `read_block` de rattrapage après `build_context`). |
| **F-02** | **CRITIQUE (S1)** | `search_symbols`, `search_documents`, `build_context` | `database.py:16` (`_TERM_RE`), `database.py:56-73` (`query_terms`), `relevance.py:15-23` (`fts_query`), `database.py:664-735` (`search_symbols`) | **Absence de décomposition CamelCase / snake_case / dot-chains et absence de prefix `*` dans `fts_query`** : `fts_query` émet `"token"` sans `*` sur une table FTS5 `unicode61` où `SparkConnectOptions` est un seul token `sparkconnectoptions` ; `query_terms` garde `a_b_c` ou `A.B` d'un seul tenant ; `search_symbols` ne cherche que dans `name` et `id` (jamais dans `signature`). | **Élimination des faux "0 matches"** sur identifiants composés, signatures et préfixes. |
| **F-03** | **HAUTE (S1/S2)** | `search_symbols`, `search_documents` | `server.py:141-201` (`empty_symbol_search_note`), `server.py:204-249` (`empty_document_search_note`), `server.py:594-635` (`search_symbols`) | **Constat passif au lieu d'auto-repli déterministe** : Quand `search_symbols` ou `search_documents` trouve 0 résultat sous `path` mais que la requête sans `path` (`elsewhere`) trouve les cibles, l'outil les liste dans une phrase et dit *"Retry without file_pattern"* (en imprimant en prime `match["symbol_id"]` brut avec `#sha1` à la ligne 161 !), forçant 1 tour supplémentaire. De plus, `empty_document_search_note` ne vérifie pas `db.search_symbols(query)` quand 0 paragraphe ne matche. | **-1 tour systématique** sur chaque filtre `path`/`kind` trop strict ou confusion code/doc. |
| **F-04** | **HAUTE (S2)** | `search_documents` | `database.py:940-968` (`search_documents`), `server.py:1609-1655` (`search_documents`), `rendering.py:151-180` (`render_document_matches`) | **Troncature codée en dur à `max_content_chars=4000` privant `full_top` de `numbered_text` et `content_hash`** : Dans `server.py:1617`, `max_content_chars=4000` tronque toute section Markdown $> 4000$ chars (`content_truncated=True`), ce qui court-circuite `patcher.block_metrics(displayed)` (`server.py:1635`) et prive l'agent de la numérotation `1..N` et du `content_hash` sur les meilleurs résultats `full_top`. | **0 `read_block` post-`search_documents`** sur les sections `full_top`. |
| **F-05** | **MOYENNE (S2)** | `search_symbols`, `search_documents`, `get_project_map` | `database.py:19-53` (`path_matches`) | **Bugs de filtrage glob dans `path_matches`** : (1) À la ligne 46 (`if prefix and "/" in norm_pattern and "*" not in norm_pattern.split("/")[-1]: return normalized.startswith(prefix + "/")`), un pattern comme `src/*/SparkConnectOptions.cs` retourne `True` pour **tous** les fichiers sous `src/` ! (2) Un préfixe `./` (`./src/...`) ou une accolade `*.{cs,md}` échoue silencieusement. | **Zéro faux positif / faux négatif** sur le paramètre `path`. |
| **F-06** | **MOYENNE (S2)** | `get_project_map`, `indexer.py`, `config.py` | `database.py:1031-1273` (`get_project_map`), `indexer.py:478-520` (`_entries_for_symbols`, `_entries_for_document`), `config.py:64-73` | **Hygiène d'index et couverture de `search_index`** : (1) `_archive/` n'est exclu que dans `database.py::search_symbols` (`database.py:717`), mais pollue encore `search_documents`, `build_context` et `get_project_map` ; (2) `_entries_for_document` (`indexer.py:517`) tronque `section.content[:4000]` dans `search_index` ; (3) `_entries_for_symbols` n'indexe ni les appels ni les littéraux du corps pour `build_context`. | **Meilleur rappel BM25** au Tour 0 et élimination du bruit `_archive/` partout. |
| **F-07** | **MOYENNE (Perf)** | `search_symbols`, `search_documents` | `server.py:599-600`, `patcher.py:8002-8048, 8067-8094` | **Motif SQL N+1 sur `displayed_symbol_id` et bypass du cache LRU Markdown** : (1) `search_symbols` appelle `patcher.displayed_symbol_id` dans une boucle sur 50 résultats (100 requêtes SQL au lieu d'un appel par fichier à `symbol_addresses`) ; (2) `displayed_section_id` (`patcher.py:8018`) et `document_addresses` (`patcher.py:8079`) appellent `markdown_layout(text)` en contournant `_get_markdown_layout`. | **Latence divisée par 5 à 10** sur `search_symbols` et `search_documents`. |

---

## 2. Analyse Détaillée par Fichier et par Symbole

### 2.1 `src/ast_graph_server/context_builder.py`

1. **Troncature au Niveau 3 privant l'agent du `content_hash` (`context_builder.py:117-160, 167-219`)** :
   * Dans `ContextBuilder._stamped_entry` (`context_builder.py:140-155`), `cap = max_chars or self.MAX_CONTENT_CHARS` (`2000` caractères). Comme `numbered` contient les préfixes de numérotation (`"   1 | "`), un bloc de 45 lignes dépasse déjà `2000` caractères, perd son `content_hash` (`extra = {}`) et affiche :
     `[... X chars omitted (shown/total lines shown): the text is NOT complete, so no hash is given; call read_block for complete block and hash ...]`
   * L'exemption introduite en `v0.4.3` dans `_symbol_text` (`context_builder.py:175-181`) exige `symbol.name.lower() in getattr(self, "_current_query_lower", "")`. Si le prompt utilise des mots-clés métier sans citer le nom exact complet de la classe ou de la méthode (`ApplyAuthentication`), ou s'il s'agit d'une section Markdown dans `_section_text` (`context_builder.py:211-214`), l'exemption ne se déclenche pas.
   * **Correction recommandée** : Accorder `effective_max = max(max_chars or self.MAX_CONTENT_CHARS, 8000)` aux **top-K entrées de niveau 3** (ex. les 5 premiers blocs approfondis au niveau 3, symboles **et** sections Markdown $\le 150$ lignes), ainsi qu'à tout symbole dont au moins un sous-token CamelCase figure dans la requête.

2. **Formatage de l'en-tête Niveau 3 dans `_to_text` (`context_builder.py:725-762`)** :
   * Aux lignes `750-753`, `_to_text` remplace la première ligne de `text` par :
     `  [code L3] ::SparkConnectOptions [h:a9ac2fdb]`
     Contrairement à `read_block` et `search_symbols(view='implementation')`, cet en-tête **n'affiche ni `({line_count} lines)` ni `[file L{start_line}-{end_line}]`**.
   * C'est la cause directe de l'**Anomalie #51** : lorsque le compilateur C# signale une erreur `SparkConnectOptions.cs(74,14)`, l'agent qui a reçu `SparkConnectOptions` via `build_context` ne connaît pas le `start_line` du bloc dans le fichier (`L10`) et applique `replace: [74, 74]` au lieu de `[65, 65]`.
   * En outre, si `_stamped_entry` retourne `None` et que `_symbol_text` (`context_builder.py:196`) ou `_document_text` (`context_builder.py:245`) retourne `base + "\n" + ...`, `first_line, _, body = text.partition("\n")` dans `_to_text` (`context_builder.py:751`) **supprime la ligne `base`** contenant la signature ou le titre !
   * **Correction recommandée** : Stocker `start_line` et `end_line` dans `extra` de `_metrics_for_symbol` (`context_builder.py:271-278`) et formater la ligne 752 de `_to_text` en :
     `  [code L3] ::Member [h:xxxxxxxx] (89 lines) [file L10-98]`

3. **Double appel `self.backend.search` lorsque `map="structure"` (`context_builder.py:497 & 595`)** :
   * `_structure_map_text` appelle `self.backend.search(query, limit=limit)` (`context_builder.py:497`), puis `build` rappelle `self.backend.search(query, limit=search_limit)` (`context_builder.py:595`). Calculer `scored` une seule fois en amont élimine la requête redondante.

---

### 2.2 `src/ast_graph_server/database.py` & `src/ast_graph_server/relevance.py`

1. **Absence de décomposition CamelCase/snake_case et de préfixe FTS5 (`database.py:56-73`, `relevance.py:15-23`)** :
   * Dans `relevance.py:15-23`, `fts_query` transforme la requête en `"tok1" OR "tok2"` sans joker `*` et sans découper les identifiants CamelCase. Dans SQLite FTS5 (`tokenize = 'unicode61'`), `SparkConnectOptions` est indexé comme l'unique token `sparkconnectoptions`. Une requête contenant `SparkConnect` ne matche donc pas ce token dans `search_index` !
   * Dans `database.py:56-73`, `query_terms` utilise `_TERM_RE = re.compile(r"[0-9A-Za-z_][0-9A-Za-z_.+]*")` (`database.py:16`). Un identifiant `spark_connect` ou `SparkConnectOptions.ApplyAuthentication` reste un seul terme monolithique : si l'agent cherche `spark_connect` alors que le symbole s'appelle `SparkConnect`, `LIKE '%spark_connect%'` renvoie **0 résultat**.
   * **Correction recommandée** :
     * Dans `fts_query` (`relevance.py:15-23`), utiliser `"{token}"*` (comme le fait déjà `search_documents` à la ligne `database.py:900`) et ajouter les sous-tokens CamelCase/snake_case.
     * Dans `_entries_for_symbols` (`indexer.py:478-495`), ajouter dans `body` les sous-mots CamelCase/snake_case de `symbol.name`.
     * Dans `DatabaseManager.search_symbols` (`database.py:664-735`), si la recherche par termes directs donne 0 résultat, relancer automatiquement en décomposant les termes CamelCase / snake_case / `.` et en cherchant également dans `signature LIKE ?`.

2. **Faille logique dans `path_matches` (`database.py:19-53`)** :
   * Aux lignes `45-47` :
     ```python
     prefix = re.split(r"[\*\?\[]", norm_pattern, maxsplit=1)[0].rstrip("/")
     if prefix and "/" in norm_pattern and "*" not in norm_pattern.split("/")[-1]:
         return normalized.startswith(prefix + "/")
     ```
     Si l'agent passe `path="src/*/SparkConnectOptions.cs"` ou `path="DataLinq.Enterprise/**/SparkConnectOptions.cs"` pour un fichier `DataLinq.Enterprise/spark/src/OtherFile.cs` :
     * `fnmatch` (L16) et `collapsed` (L20) valent `False` (car `OtherFile.cs != SparkConnectOptions.cs`).
     * Puis l'exécution arrive à la ligne 45 : `prefix` vaut `"src"` (ou `"DataLinq.Enterprise"`), `"/" in norm_pattern` est `True`, et `"*" not in norm_pattern.split("/")[-1]` est `True` (car le dernier segment est `"SparkConnectOptions.cs"`, qui ne contient pas `*` !).
     * La ligne 47 retourne alors `normalized.startswith(prefix + "/")` -> **`True` pour TOUS les fichiers du dossier `prefix`**, ignorant complètement le nom de fichier `SparkConnectOptions.cs` à la fin du glob !
   * Par ailleurs, `norm_pattern` ne retire pas un éventuel préfixe `./` (`./src/...`), et ne gère pas les globs à accolades `*.{cs,md}`.
   * **Correction recommandée** : Supprimer la condition défectueuse des lignes 46-47 (la ligne 53 `return normalized.startswith(norm_pattern.rstrip("/") + "/")` ne doit s'appliquer **que** lorsque `norm_pattern` ne contient aucun caractère wildcard `*?[` !), nettoyer `.lstrip("./")` et supporter l'expansion `{a,b}`.

---

### 2.3 `src/ast_graph_server/server.py`

1. **Remplacer les refus/notes passives par un auto-repli déterministe dans `search_symbols` (`server.py:141-201, 594-635`)** :
   * Actuellement, lorsque `self.db.search_symbols(query=query, kind=kind, file_pattern=path, limit=limit)` retourne `[]` :
     * `empty_symbol_search_note` (`server.py:157-167`) exécute `elsewhere = db.search_symbols(query=query, kind=kind, limit=5)` et dit à l'agent : *"No symbol matches '...' under file_pattern '...'. N match the same query WITHOUT it: <sample>. Retry without file_pattern, or widen it."*
     * **Bug de surface dans `empty_symbol_search_note` (`server.py:161`)** : `sample = ", ".join(match["symbol_id"] for match in elsewhere[:3])` imprime l'identifiant interne `symbol_id` (avec `#sha1` de surcharge !) au lieu de `patcher.displayed_symbol_id(match["symbol_id"])`.
     * **Violation de la Règle d'Or** : puisque l'outil a déjà trouvé `elsewhere` de façon déterministe, il ne doit **jamais** demander à l'agent de dépenser un tour pour relancer sans `path` (ou sans `kind`) ! Il doit auto-élargir (`matches = elsewhere`), calculer leurs `views` (`contract` / `implementation` avec `numbered_code` et `content_hash`) et préfixer la sortie par une note d'auto-élargissement (`[note: 0 match under path '...'; showing N match(es) across workspace:]`).
     * De même, si `kind != "all"` donne 0 résultat mais que `kind="all"` trouve le symbole (ex: `record` ou `struct` ou `property` cherché comme `class` ou `method`), l'outil doit auto-élargir à `kind="all"` et livrer les résultats dans le même tour.

2. **Troncature à `max_content_chars=4000` et auto-repli dans `search_documents` (`server.py:204-249, 1609-1655`)** :
   * Dans `server.py:1617`, `max_content_chars=4000` coupe toute section Markdown de plus de 4 000 caractères. Ensuite, à la ligne `1635` :
     `if match.get("content") is not None and not match.get("content_truncated"):`
     Dès qu'une section `full_top` dépasse 4 000 caractères, `patcher.block_metrics(displayed)` n'est **pas** appelé : l'agent reçoit du texte brut non numéroté et sans `content_hash`, ce qui l'oblige à faire un `read_block` supplémentaire avant `edit_block`.
   * **Correction recommandée** : Pour les entrées `full_top` (défaut `2`), ne pas tronquer à 4 000 chars (ou porter le plafond à `16000` chars) et **toujours** attacher `block_metrics` (`numbered_text`, `content_hash`, `line_count`) aux `full_top` premiers résultats.
   * Dans `empty_document_search_note` (`server.py:242-246`), ajouter le pendant symétrique de `empty_symbol_search_note` : interroger `db.search_symbols(query=query, limit=5)` et afficher les adresses canoniques `displayed_symbol_id` des symboles correspondants (et auto-élargir si `file_pattern` bloquait les résultats de `search_documents`).

3. **Élimination de la boucle N+1 dans `search_symbols` et du cache manquant dans `_symbol_view` (`server.py:390, 599-600`)** :
   * Dans `server.py:599-600` :
     ```python
     for match in matches:
         match["displayed_id"] = self.patcher.displayed_symbol_id(match["symbol_id"])
     ```
     `displayed_symbol_id` (`patcher.py:8038-8043`) exécute `self.db.get_symbol(symbol_id)` puis `self._symbol_rows(symbol.file_path)` à chaque itération. Pour 50 résultats dans 3 fichiers, cela génère **100 requêtes SQL** au lieu de 3 appels groupés à `self.patcher.symbol_addresses(file_path)`.
   * De même, dans `patcher.py:8018` (`displayed_section_id`) et `patcher.py:8079` (`document_addresses`), `markdown_layout(text)` est appelé directement au lieu de passer par `self._get_markdown_layout(abs_path, raw_bytes, text)` (`patcher.py:664`).

---

### 2.4 `src/ast_graph_server/rendering.py` & `src/ast_graph_server/indexer.py`

1. **Fuite de `symbol_id` brut dans les erreurs de `render_symbol_matches` (`rendering.py:146-147`)** :
   * `for symbol_id, message in (errors or {}).items(): lines.append(str(symbol_id) + " [error] " + str(message))` affiche la clé interne `symbol_id` au lieu de son `displayed_id`.
2. **Indexation FTS5 incomplète dans `Indexer` (`indexer.py:478-520`)** :
   * Dans `Indexer._entries_for_document` (`indexer.py:517`), `"body": section.content[:4000]` tronque l'indexation FTS5 de `search_index` à 4 000 caractères (alors que `document_sections_fts` indexe toute la section).
   * Dans `Indexer._entries_for_symbols` (`indexer.py:478-495`), `body` ne contient que `kind + signature + docstring`. Ajouter les tokens CamelCase décomposés et les noms de cibles d'appels (`calls`) permet à `build_context` de trouver immédiatement une méthode par ce qu'elle appelle ou configure.
   * Dans `config.py:64-73`, ajouter `"_archive"`, `"_archive/*"`, `".archive"`, `".archive/*"` à `discovery_exclude_patterns` pour aligner `build_context`, `get_project_map` et `search_documents` sur l'exclusion déjà pratiquée par `search_symbols` (`database.py:717`).