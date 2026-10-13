# Backlog Technique Moteur Scalpel (`scalpel-mcp`)

Dernière mise à jour : 13/10/2026  
Statut : Actif / Chantiers Prioritaires

---

## 1. Priorité Absolue — Optimisations Massives `search_documents` (Facteur x20 à x50)

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

## 2. Historique des Correctifs Récents Validés (13/10/2026)

* **Déballage défensif des `FieldInfo` (`search_documents`)** : Correction du crash `TypeError: int() argument must be a string... not 'FieldInfo'` dans `database.py` et `server.py`.
* **Correction du contrat `search_documents`** : Suppression de `"required": ["query"]` dans `toolset_schemas.py` et levée de la troncature silencieuse à 8 documents sur requête vide.
* **Suppression du message d'erreur circulaire** : Dans `toolsets.py` (`_plain_tool_error`), suppression de la consigne toxique *"find it with search_documents"* en cas de panne de l'outil.
* **Validation unitaire** : 625/625 tests passés avec succès (`test_search_documents_robustness.py`).