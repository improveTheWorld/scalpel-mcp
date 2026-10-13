# Spécification Technique : Optimisation et Refactorisation de `build_context`

## 1. Contexte et Objectifs
L'outil `build_context` est le constructeur de contexte budgété et classé par pertinence de Scalpel. Il permet à un agent d'obtenir un dossier de travail immédiatement exploitable pour une tâche complexe, en équilibrant survol large (breadth) et approfondissement vertical (depth jusqu'au niveau 3 avec code source et hashes `[h:hash]`).

L'audit approfondi réalisé en conditions réelles (sur `Scalpel-clean` et `DataLinq`) a mis en évidence 4 dysfonctionnements majeurs :
1. **Dérive budgétaire critique** :
   - Le format `json` consomme entre **+150% et +192%** de tokens réels en plus que le texte brut, car le packing est calculé sur le texte et la sérialisation JSON duplique inutilement les champs (`entries`, `text`, `context`).
   - Le paramètre `map='structure'` alloue ses 32 000 tokens en **surplus** du budget, générant jusqu'à 37 332 tokens pour un budget demandé de 8 000 tokens (+366%).
2. **Latence excessive (12 secondes sur gros dépôts)** :
   - Présence d'une quadruple boucle séquentielle de deepening exécutant 600 appels à `render()`.
   - Problème de requêtes N+1 dans `_to_text` via `patcher.displayed_id` (2.8 s de latence pure).
3. **Pollution sémantique du ranking BM25** :
   - Les fichiers JSON de bilans (`reports/`) et de transcripts saturent les résultats au détriment du code source.
4. **Troncature brute au niveau 3** :
   - Coupure de texte en plein mot à 2 000 caractères et suppression intégrale du hash sans indication de bornes.

Ce document structure la refactorisation en **4 Lots indépendants et incrémentaux**.

---

## 2. Découpage en Lots

### Lot 1 : Maîtrise Budgétaire Absolue & Enveloppe Globale (Priorité Haute)
- **Objectif** : Garantir le respect strict du budget (`budget_tokens`) sur le fil d'entrée du LLM, en format `text` comme en format `json`, et en présence de cartes de projet (`map`).
- **Périmètre technique** :
  - `scalpel-mcp/src/ast_graph_server/context_builder.py` (`ContextBuilder.build`, constantes de budget).
  - `scalpel-mcp/src/ast_graph_server/server.py` (`ASTGraphServer._register_tools::build_context`).
  - `scalpel-mcp/tests/test_context_builder.py`.

#### Spécifications détaillées Lot 1 :
1. **Enveloppe globale pour `map`** :
   - `budget_tokens` est désormais le **plafond absolu** de la réponse, incluant la carte.
   - Si `map != 'none'`, le budget de la carte est alloué comme une quote-part (ex: max 25% à 30% du `budget_tokens`, ou min(map_tokens, budget_tokens * 0.3)).
   - Les sections conservent au minimum 70% du budget demandé.
   - En aucun cas `text_chars + map_chars` ne doit dépasser `budget_chars = budget_tokens * CHARS_PER_TOKEN`.
2. **Calibration du budget pour le format `json`** :
   - Si `format == 'json'`, le budget de caractères alloué à la sélection doit tenir compte de la surcharge de sérialisation JSON (`CHARS_PER_TOKEN_JSON = 1.3` au lieu de 3.3, soit un facteur d'overhead de 2.5x).
   - Dans `server.py`, supprimer l'affectation `payload["context"] = payload["text"]` qui doublait inutilement la taille du payload JSON.
   - Dans `build()`, calculer `used_tokens_estimate` en fonction du format réel demandé.
3. **Sécurisation de `breadth_ratio`** :
   - Si `breadth_ratio == 0.0`, l'outil ne doit pas retourner un résultat vide : il doit appliquer un plancher minimal (au moins 1 entrée ou désactiver la phase breadth pour approfondir directement les meilleurs candidats en depth-first).
   - La phase breadth doit limiter le nombre de candidats par un quota explicite plutôt que par un seuil de caractères saturé immédiatement.

---

### Lot 2 : Performance & Latence (De 12s à < 400ms)
- **Objectif** : Éliminer les goulots d'étranglement CPU et I/O pour ramener le temps d'exécution sous les 400 ms.
- **Périmètre technique** :
  - `scalpel-mcp/src/ast_graph_server/context_builder.py`.
  - `scalpel-mcp/src/ast_graph_server/patcher.py`.

#### Spécifications détaillées Lot 2 :
1. **Single-Pass Deepening (Suppression de la boucle multi-passes)** :
   - Remplacer `for level in range(1, max_level + 1): for key in order:` par une seule passe sur les candidats triés par score décroissant.
   - Pour chaque candidat, calculer et injecter directement son niveau maximal admissible (3, 2, ou 1) selon le budget résiduel.
2. **Batching de la résolution des identifiants d'affichage** :
   - Regrouper les `ref_id` par fichier source avant formatage.
   - Appeler `symbol_addresses(file_path)` et `document_addresses(file_path)` une seule fois par fichier, puis mapper en mémoire locale. Supprimer les appels unitaires N+1 à `displayed_id`.
3. **Cache de session `block_metrics`** :
   - Mémoriser dans un dictionnaire éphémère `_metrics_cache` les résultats de `block_metrics` pour éviter les lectures disque répétées du même fichier au cours d'un même appel.

---

### Lot 3 : Hygiène de l'Index & Pondération Sémantique (Anti-Bruit)
- **Objectif** : Garantir la primauté du code source et des spécifications fonctionnelles sur les fichiers de log, de rapports ou de traces.
- **Périmètre technique** :
  - `scalpel-mcp/src/ast_graph_server/relevance.py`.
  - `scalpel-mcp/src/ast_graph_server/context_builder.py`.
  - `scalpel-mcp/src/ast_graph_server/indexer.py`.

#### Spécifications détaillées Lot 3 :
1. **Exclusion par défaut des dossiers de données de run** :
   - Exclure de l'indexation par défaut les dossiers de métadonnées et de résultats : `reports/`, `benchmarks/results/`, `transcripts/`, `.work/`, `.pytest_cache/`.
2. **Pondération AST du scoring BM25** :
   - Appliquer un multiplicateur de score selon le type d'élément :
     - Code source (`class`, `method`, `function`, `interface`) : multiplicateur `x 1.5`.
     - Documentation (`section`, `document`) : multiplicateur `x 1.0`.
     - Propriétés JSON génériques (`property`, `array`) : malus `x 0.3`.
3. **Activation par défaut d'un `max_entries_per_file` raisonnable** (ex: 5 par défaut) pour empêcher un document de 100 sections de cannibaliser les autres sources.

---

### Lot 4 : Qualité de Restitution au Niveau 3 (Graceful Truncation)
- **Objectif** : Restituer des blocs propres et des métadonnées exploitables même lorsque la taille dépasse la limite autorisée.
- **Périmètre technique** :
  - `scalpel-mcp/src/ast_graph_server/context_builder.py` (`_stamped_entry`, `_bounded_content`).

#### Spécifications détaillées Lot 4 :
1. **Découpage propre aux frontières de lignes** :
   - Remplacer `numbered[:cap]` par une coupure au dernier caractère de saut de ligne `\n` avant `cap`.
   - Ne jamais couper un mot ou une ligne de code au milieu.
2. **Conservation des métadonnées de localisation** :
   - En cas de dépassement de `MAX_CONTENT_CHARS`, conserver l'indication du nombre de lignes total (`Lignes 1-45 sur 120`) et le conseil d'utilisation de `read_block` avec offset.

---

## 3. Guide de Lecture pour l'Agent Exécuteur
Avant d'écrire la moindre ligne de code, l'agent exécuteur **doit impérativement lire et comprendre** :
1. `scalpel-mcp/src/ast_graph_server/context_builder.py` :
   - Constantes de classe : `MAX_CONTENT_CHARS`, `CHARS_PER_TOKEN`, `DETAIL_LEVEL_BUDGETS`, `MAP_LEVEL_TOKENS`.
   - Méthode `build()` : lignes 318 à 511 (logique de budget, Phase 1 breadth, Phase 2 deepening, assemblage final).
   - Méthode `_to_text()` : lignes 514 à 551 (groupement par fichier et formatage textuel).
2. `scalpel-mcp/src/ast_graph_server/server.py` :
   - Outil `build_context` : lignes 1219 à 1346 (résolution des paramètres, délégation au builder, gestion de `format="json"`).
3. `scalpel-mcp/tests/test_context_builder.py` :
   - Suite de tests unitaires existante servant de filet de sécurité anti-régression.