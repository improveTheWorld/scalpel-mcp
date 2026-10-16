# Audit Approfondi de `insert_block` et `delete_block` (`server.py`, `patcher.py`, `documents.py`)

**Agent Auditeur** : `5598f001-f916-4ea1-a23f-3a87736d50af`  
**Capsule Créée** : `scalpel-mcp.engine.insert-delete`  
**Règle d'Or** : *« Un outil qui sait exécuter la demande d'un agent d'une façon déterministe ne doit JAMAIS la refuser, ni causer des tours supplémentaires. »*

---

## 1. `insert_block` sur le Code (`parent="fichier.cs"`, `namespace`, conteneur unique et types imbriqués)

### 1.1. BUG CRITIQUE S1 : Impossibilité totale d'insérer dans un `struct`, `interface`, `record` ou `enum` sans `anchor` (`patcher.py:5525` vs `5658`)
* **Localisation** : `Patcher.insert_symbol` dans `src/ast_graph_server/patcher.py:5525-5674`.
* **Mécanisme prouvé** :
  * À la ligne `5525`, la branche qui calcule l'indentation des membres (`target_col = (self._ast_depth(parent_sym) + 1) * len(indent_step)`) et place l'insertion **avant** l'accolade fermante `}` du conteneur (`L5586-5588`) teste uniquement :
    ```python
    if parent_sym and parent_sym.kind == "class":
    ```
  * Lorsque `parent_sym.kind` vaut `"struct"`, `"interface"`, `"record"` ou `"enum"` (et que `anchor` est omis), l'exécution tombe dans le `else:` (`patcher.py:5590`), fixe `target_col = 0` et `insertion_byte = len(file_bytes)` (`patcher.py:5629`, **après** l'accolade fermante du conteneur !).
  * Ensuite, la post-condition à `patcher.py:5658` teste :
    ```python
    if parent_sym is not None and parent_sym.kind in ("class", "struct", "interface", "record", "enum"):
    ```
    Elle constate que le nouveau membre a été inséré en fin de fichier hors de `parent_sym.id` et **refuse systématiquement l'appel** avec `"Insertion refused: the declaration would land outside '...'"` !
* **Correctif déterministe** : Remplacer `if parent_sym and parent_sym.kind == "class":` (`patcher.py:5525`) par `if parent_sym and parent_sym.kind in ("class", "struct", "interface", "record", "enum"):`.

### 1.2. Auto-routage déterministe vers le conteneur unique quand `parent="fichier.cs"` ou `parent="fichier.cs::Namespace"` (`patcher.py:5682-5708`)
* **Localisation** : `Patcher.insert_symbol` (`patcher.py:5457-5461`, `5682-5708`) et `Patcher._cannot_receive_a_child` (`patcher.py:7445-7455`).
* **Mécanisme actuel** : Quand l'agent appelle `insert_block(parent="SparkConnectOptions.cs", text="public string Foo { get; set; }")` ou `insert_block(parent="SparkConnectOptions.cs::DataLinq.Spark", text="public void Bar() {}")`, `insert_symbol` insère à `len(file_bytes)` (`patcher.py:5629`), puis détecte à `patcher.py:5691-5708` que le symbole créé est un `MEMBER_KINDS` orphelin (`not s.parent_id`) et **refuse** :
  `"Insertion refused: 'Bar' is a MEMBER declaration and 'SparkConnectOptions.cs' is not a container... Name the container instead: SparkConnectOptions.cs::SparkConnectOptions"`.
* **Optimisation zéro-tour** :
  * Lorsque `parent_sym is None or parent_sym.kind == "namespace"` et que le code soumis est une déclaration de membre (ou génère un `orphans` lors du pré-calcul), inspecter les conteneurs du fichier (`s.kind in parser.CONTAINER_KIND_NAMES`).
  * S'il existe **exactement 1 conteneur** dans le fichier/namespace (cas de >90 % des fichiers C#/TS/Java), **router automatiquement** `parent_sym` vers cet unique conteneur, recalculer l'indentation et l'offset de fin de corps, écrire directement et renseigner `routed_to_parent` + un `warning` explicite.

### 1.3. Mauvais placement dans un `namespace` à accolades (`namespace X { ... }`) (`patcher.py:5599-5609` & `5629-5637`)
* **Localisation** : `Patcher.insert_symbol` (`patcher.py:5599-5609` et `5629-5637`).
* **Deux anomalies** :
  1. **Sous `parent="fichier.cs::MonNamespace"` avec `anchor=None` (`position="end"` par défaut)** : À `patcher.py:5603`, `first_child = self.db.get_first_child_symbol(parent_sym.id) if at_start else None` vaut toujours `None` lorsque `at_start` est `False` (et même lorsque `at_start` est `True` en C#, car les types top-level ont `parent_id = None` en base !). Le code exécute donc toujours `insertion_byte = self._namespace_body_start(file_bytes, parent_sym)` (`patcher.py:5608`), insérant **au tout début** du namespace avec `target_col = 0` au lieu d'insérer **à la fin** du namespace (juste avant son `}` fermant s'il est bloc) avec l'indentation du namespace !
  2. **Sous `parent="fichier.cs"` contenant un `namespace X { ... }` à accolades** : `patcher.py:5629` utilise `insertion_byte = len(file_bytes)` et `patcher.py:5635` ne traite que `_file_scoped_namespace_body` (`namespace X;`). Sur un namespace à accolades, la nouvelle classe/enum est écrite **après l'accolade fermante `}` du namespace** ! Si le fichier possède un unique namespace à accolades, l'insertion doit se placer avant le `}` fermant de ce namespace (`ns_sym.end_byte - 1`).

### 1.4. Garde `#44(b)` (`insert_block(parent=Class, text="public enum ...")`) : auto-routage en sibling (`patcher.py:5525-5558`)
* **Localisation** : `Patcher.insert_symbol` (`patcher.py:5525-5558`).
* **Constat & Optimisation** : Lorsqu'un agent soumet un `enum` (sans `anchor`) sous `parent="File.cs::ClassName"`, le code actuel refuse (`patcher.py:5543-5558`) en lui demandant de rappeler `insert_block(parent='File.cs', anchor='File.cs::ClassName', text=...)`. Puisque le refus connaît déjà l'appel exact voulu par l'agent, **router automatiquement** en sibling juste après `ClassName` (`anchor_sym = parent_sym`, `parent_sym = None`, `target_col = self._ast_depth(parent_sym) * len(indent_step)`) avec un `warning` explicite (à l'image de `_title_route` en Markdown) économise 1 tour complet.

### 1.5. Oubli de `content_hash` dans le retour de `insert_symbol` (`patcher.py:5730-5743`)
* **Localisation** : `Patcher.insert_symbol` (`patcher.py:5740-5743`).
* **Constat** : Alors que la description de `insert_block` (`server.py:1199`) promet *"return the state of the block you just made: its id, its numbered text and its hash, so the next call needs no read_block"*, `insert_symbol` renvoie `line_count` et `numbered_text` mais **omet `payload["content_hash"] = state["content_hash"]`** (contrairement à `Patcher.insert_block` en `patcher.py:7881`).

---

## 2. `insert_block` avec un `anchor` décalé, nom court, ou parent/enfant inversé

### 2.1. `anchor` passé comme nom court sans préfixe fichier refusé (`patcher.py:7641` & `5467`)
* **Localisation** : `Patcher.insert_block` (`patcher.py:7641-7644`) et `Patcher.insert_symbol` (`patcher.py:5466-5470`).
* **Mécanisme** :
  * Les deux méthodes conditionnent la résolution de l'ancre à `if "::" in where:` / `if "::" in anchor_id:`.
  * Or `read_block` affiche les enfants sous leur **nom court** (`Connecting with the Context API`, `BuildAuthenticationConfig`). Si l'agent passe `parent="doc.md::Quick Start", anchor="Connecting with the Context API"` (sans `::`), `_resolve_displayed_path` n'est **pas appelé**, `where` ne matche aucun `doc.md#slug` dans `live_ids` (`patcher.py:7775`), et l'outil **refuse** !
  * De même, si l'agent passe `anchor="Quick Start::Connecting"` (avec `::` mais sans `doc.md::`), `_resolve_displayed_path` prend `"Quick Start"` comme chemin de fichier et échoue.
* **Correctif déterministe** : Si `where` / `anchor` ne résout pas tel quel, tenter automatiquement :
  1. `self._resolve_displayed_path(f"{parent_id}::{anchor}")`
  2. `self._resolve_displayed_path(f"{file_or_doc_path}::{anchor}")`

### 2.2. Généralisation de `#47` en Markdown : auto-routage universel vers `holder_of(anchor)` (`patcher.py:7787-7815`)
* **Localisation** : `Patcher.insert_block` (`patcher.py:7787-7815`) et `Patcher._where_not_a_child` (`patcher.py:7916-7976`).
* **Mécanisme actuel** : `#47` (`patcher.py:7791-7811`) ne route automatiquement vers le parent réel de `anchor` que si `parent_index is None and len(children) == 1 and holder_idx == children[0]`.
* **Cas encore refusés alors que l'intention est 100 % déterministe** :
  * `parent="doc.md"` avec `anchor="doc.md::H1::H2::H3"` (niveau $\ge 3$, ou document ayant plusieurs H1).
  * `parent="doc.md::H1"` avec `anchor="doc.md::H1::H2::H3"` (`parent` est le grand-parent de `anchor`).
  * `parent="doc.md::H1::H2_A"` avec `anchor="doc.md::H1::H2_B"` (`parent` et `anchor` sont deux frères sous `H1`).
* **Preuve dans le code** : `_where_not_a_child` (`patcher.py:7947-7960`) calcule déjà `holder_index = self._holder_index(layout, child_index)` et `holder = live_ids[holder_index] if holder_index is not None else document_id`, uniquement pour dire à l'agent dans le refus : *"To place the new block beside it, call insert_block(parent='<holder>', anchor='<where>', text=...)"* !
* **Optimisation zéro-tour** : Dès que `child_index is not None` et `int(layout[child_index]["level"]) > 0` et `where != parent_id`, si `child_index not in children`, **router automatiquement** `parent_index = holder_index` et `parent_id = holder`, recalculer `level = parent_level + 1` et `children`, insérer juste après le sous-arbre de `child_index`, et émettre un `warning` de re-ciblage.

### 2.3. Trois refus sur `anchor` en Code (`patcher.py:5479-5506`)
* **Localisation** : `Patcher.insert_symbol` (`patcher.py:5479-5506`).
  1. **`anchor == parent_id` refusé en code alors qu'il est prescrit en Markdown** : En Markdown (`patcher.py:7758`), passer `anchor=parent_id` insère en **premier enfant** (`at_start=True`). En code (`patcher.py:5480`), `parent="Foo.cs::MyClass", anchor="Foo.cs::MyClass"` a `anchor_sym.id == parent_sym.id`, donc `anchor_sym.parent_id != parent_sym.id` est vrai et l'appel est **refusé** (`"Anchor 'Foo.cs::MyClass' is not a direct child of container 'Foo.cs::MyClass'"`) ! **Correctif** : Si `parent_sym and anchor_sym.id == parent_sym.id`, traiter comme `at_start = True, anchor_sym = None`.
  2. **`parent="Foo.cs"` (ou `Foo.cs::Namespace`) avec `anchor="Foo.cs::MyClass::MyMethod"`** : Refusé à `patcher.py:5497-5506` alors que `anchor_sym.parent_id` identifie `MyClass` de façon unique. Router automatiquement `parent_sym = self.db.get_symbol(anchor_sym.parent_id)`.
  3. **`parent="Foo.cs::MyClass::MyMethod"` (une méthode passée comme `parent` au lieu de `anchor`)** : Si `parent_sym.kind in member_kinds.values()` et `parent_sym.parent_id` existe, router automatiquement vers `anchor_sym = parent_sym` et `parent_sym = self.db.get_symbol(parent_sym.parent_id)`.

---

## 3. `insert_block` : Bloc/Fichier existant, Chemins imbriqués et Insertions Multiples (`text` ou `inserts=[...]`)

### 3.1. BUG CRITIQUE S1/S2 : Inversion de l'ordre (LIFO au lieu de FIFO) dans `insert_block(inserts=[...])` (`server.py:1293-1337`)
* **Localisation** : `insert_block` dans `src/ast_graph_server/server.py:1309-1336`.
* **Mécanisme prouvé** :
  * Lorsqu'un agent appelle `insert_block(parent="doc.md::Sec", inserts=[{"text": "A"}, {"text": "B"}, {"text": "C"}])` (avec `anchor` omis pour insérer en tête, ou avec un `anchor="doc.md::Sec::Existing"` partagé au top-level) :
    * L'entrée 0 insère `A` en premier enfant (ou juste après `Existing`).
    * L'entrée 1 réutilise `item_anchor = None` (ou `Existing`) et insère `B` **avant `A`** !
    * L'entrée 2 réutilise `item_anchor = None` (ou `Existing`) et insère `C` **avant `B`** !
  * **Résultat** : Les blocs insérés arrivent dans **l'ordre inverse (`C, B, A`)** aussi bien en Markdown qu'en code !
* **Correctif déterministe** : Dans la boucle `for idx, item in enumerate(inserts):` (`server.py:1302`), maintenir un suivi `last_created_by_target: Dict[Tuple[str, Optional[str]], str] = {}`. Si l'entrée `idx` cible le même `(item_parent, effective_anchor)` qu'une entrée précédente du même lot ayant réussi, chaîner automatiquement son `anchor` sur le `block_id` / `created_symbol_id` créé par l'entrée précédente (ordre FIFO strict).

### 3.2. Insertion de plusieurs sections/symboles dans un seul `text` (`patcher.py:7654, 7820-7839`)
* **Localisation** : `Patcher.insert_block` (`patcher.py:7654`, `_split_block_text` en `patcher.py:7978-8000`, `patcher.py:7820-7839`).
* **Mécanisme** :
  * Si l'agent passe plusieurs sections frères dans un seul `text` (ex: `text="## Sec A\nBody A\n\n## Sec B\nBody B"`), `_split_block_text` ne découpe que la **1re ligne** (`Sec A`), lui applique `"#" * level` et `_numbered_heading`, et laisse `## Sec B` brut dans `body_text` !
  * Si le niveau `#` écrit par l'agent diffère du `level` dérivé (`parent_level + 1`), la 1re section prend `level` mais la 2e garde ses `#` bruts (devenant enfant de la 1re ou cassant la hiérarchie !). De plus, dans un document numéroté, seule la 1re section est numérotée et `_renumber_following` ne décale les suivants que de `+1` au lieu de `+K`.
* **Correctif déterministe** : Si `content is None` et que `text` contient $K \ge 2$ titres consécutifs du même niveau que la 1re ligne (via `_sibling_chunks` en `patcher.py:6921-6933`), les découper et les insérer séquentiellement en chaîne (comme le fait `_title_routes_in_sequence` en `patcher.py:6792-6919`), et retourner `created_blocks` avec le hash de chaque bloc créé.

### 3.3. Création de fichier avec sous-chemin (`text="sub/file.cs"`), message obsolète dans `_missing_file_refusal` et idempotence sur bloc existant
* **Localisation** : `_create_child_of` (`patcher.py:7505-7543`), `_missing_file_refusal` (`patcher.py:5367-5409`), `_reject_duplicate_heading` (`patcher.py:5874-5887`), `_reject_duplicate` (`patcher.py:332-351`).
* **Trois optimisations zéro-tour** :
  1. **Création de sous-dossiers intermédiaires (`mkdir -p`) dans `_create_child_of` (`patcher.py:7505`)** : Autoriser les séparateurs `/` dans `name` tant que `target` reste confiné dans `workspace_root` (sans segment `..`), en créant les dossiers parents via `target.parent.mkdir(parents=True, exist_ok=True)`.
  2. **Correction du mensonge dans `_missing_file_refusal` (`patcher.py:5388`)** : Le message affirme encore *"File '...' does not exist on disk, and no tool creates a file"*, ce qui est faux depuis `_create_child_of`. Prescrire `insert_block(parent='<folder>', text='<filename>\n<code>')` (ou créer directement le fichier lorsque `parent="dossier/NouveauFichier.cs"` n'existe pas sur disque et que `anchor` est omis !).
  3. **Idempotence (`noop`) et livraison du hash sur doublon** : Lorsqu'un fichier (`_create_child_of` `patcher.py:7532`), une section (`_reject_duplicate_heading` `patcher.py:5874`) ou un symbole (`_reject_duplicate` `patcher.py:332`) existe déjà :
     * Si son contenu est **identique**, retourner `status: "noop"` avec `block_id`, `content_hash`, `line_count` et `numbered_text`.
     * Si son contenu diffère, inclure `existing_block_id`, `content_hash`, `line_count` et `numbered_text` du bloc existant dans le refus pour permettre un `edit_block` immédiat sans `read_block` intermédiaire.

---

## 4. `insert_block` et la Numérotation Markdown (`1.2. Titre`, collisions et décalages)

### 4.1. BUG : `_sibling_clash` compare les titres AVEC leur numéro, laissant passer les doublons de titres frères (`patcher.py:5856-5872` & `7821-7824`)
* **Localisation** : `Patcher._sibling_clash` (`patcher.py:5863-5868`) appelé après `_numbered_heading` (`patcher.py:7821-7822`).
* **Mécanisme prouvé** :
  * `_numbered_heading` préfixe d'abord `heading_text` par son numéro (ex: `"Overview"` inséré en position 3 devient `"3. Overview"`), puis `_sibling_clash` compare `item["heading"].strip().lower() != normalized` (`"1. overview" != "3. overview"`).
  * Comme `"1. overview"` $\ne$ `"3. overview"`, `_sibling_clash` ne détecte **pas** qu'une section sœur `"1. Overview"` existe déjà sous le même parent ! Elle laisse créer `## 3. Overview`, qui entre ensuite en collision dans `stable_section_ids` (`documents.py:88`, qui utilise `section_title()` sans numéro) et crée un doublon `Overview (2)`.
* **Correctif** : Dans `_sibling_clash` (`patcher.py:5863-5868`), comparer `section_title(item["heading"]).strip().lower() == section_title(heading).strip().lower()`.

### 4.2. BUG : Numérotation hiérarchique (`1.1.`, `1.2.`) ignorée par `_numbered_heading`, `_previous_numbered_sibling` et `_renumber_following` (`patcher.py:7058-7193`)
* **Localisation** : `patcher.py:7079, 7103, 7123, 7169`.
* **Mécanisme prouvé** :
  * `section_title` (`documents.py:54-59`) retire les numéros hiérarchiques `^\s*\d+(?:\.\d+)*(?:[.)]|\s)\s*` (ex: `"1.2. Architecture"` $\to$ `"Architecture"`).
  * Mais `_numbered_heading`, `_next_numbered_sibling`, `_previous_numbered_sibling` et `_renumber_following` utilisent tous `r"^(\d+)[.)](?!\d)"`, qui **exclut tout numéro hiérarchique `1.2` ou `1.2.`** à cause du `(?!\d)` !
  * **Conséquences** :
    1. Un titre sans numéro inséré parmi des frères `1.1.`, `1.2.` ne reçoit **aucun numéro**.
    2. Un titre avec numéro explicite `### 1.2. Nouveau` inséré avant `### 1.2. Existant` retourne `number = None` (`patcher.py:7079`), donc `_renumber_following` ne s'exécute pas (`patcher.py:7837`), laissant deux sections `1.2.` en collision !
    3. Lorsqu'un parent `## 2. Parent` est renuméroté en `## 3. Parent`, ses enfants `### 2.1. Sous-section` restent `2.1.` (`patcher.py:7167`).
* **Correctif déterministe** : Supporter les numéros multi-niveaux `^((?:\d+\.)*)(\d+)([.)]?)(?=\s)` dans les 4 méthodes pour dériver et incrémenter automatiquement le dernier segment numérique (et propager le changement de préfixe aux sous-sections enfants lors d'un décalage de parent).

### 4.3. BUG : Séquence `1, 2, 2, 4` quand l'agent fournit un numéro explicite $\le$ au frère précédent (`patcher.py:7078-7080`)
* **Localisation** : `Patcher._numbered_heading` (`patcher.py:7078-7080`).
* **Mécanisme** : Si le document contient `1. A`, `2. B`, `3. C` et que l'agent appelle `insert_block(parent=..., anchor="...::B", text="2. Nouveau\n...")` (inséré *après* `2. B`), `_numbered_heading` conserve `number = 2`, puis `_renumber_following` (`patcher.py:7175`) décale `3. C -> 4. C`, laissant `1. A`, `2. B`, `2. Nouveau`, `4. C` (`1, 2, 2, 4`) !
* **Correctif** : Dans `_numbered_heading`, vérifier `prev = cls._previous_numbered_sibling(layout, position, level)` même si l'agent a écrit un numéro initial `given`. Si `prev is not None and given <= prev`, corriger automatiquement le numéro en `prev + 1` et le mentionner dans `warnings`.

---

## 5. `delete_block` : Suppression en lot (`paths=[...]`), `expected_heading`, Retour du Hash Parent et Verrous

### 5.1. Absence de `paths=[...]` sur la surface MCP de `delete_block` alors que `Patcher.delete_block` l'implémente déjà (`server.py:1348-1375` vs `patcher.py:8205-8231`)
* **Localisation** : `delete_block` dans `server.py:1359-1375` vs `Patcher.delete_block` dans `patcher.py:8205-8231`.
* **Constat** :
  * `Patcher.delete_block` possède déjà le paramètre `paths: Optional[List[str]] = None` (`patcher.py:8206`) et son `actionable_hint` (`patcher.py:8230`) prescrit même `"delete_block(paths=[...]) removes several blocks at once"` !
  * Pourtant, `server.py:1359-1375` ne déclare que `path: str = Field(...)` (requis) et ignore `paths` !
  * En outre, dans `unify_addresses` (`patcher.py:8173-8175`), seule la clé `out["blocks"]` est parcourue récursivement, et **pas `out["results"]`** (utilisée par `delete_block(paths=[...])` et `insert_block(inserts=[...])`).

### 5.2. `expected_heading` trop strict dans `_heading_mismatch` (`patcher.py:5889-5910`)
* **Localisation** : `Patcher._heading_mismatch` (`patcher.py:5897-5910`).
* **Mécanisme prouvé** :
  * `_heading_mismatch` teste uniquement `heading.strip().lower() == expected.lower()`.
  * Or `target["heading"]` dans `markdown_layout` (`documents.py:146`) ne contient **pas** les `###` initiaux, mais **contient** le numéro de section (ex: `"2. Connecting with the Context API"`).
  * Si l'agent passe `expected_heading="### 2. Connecting with the Context API"` (copié de la ligne 1 de `read_block`) **OU** `expected_heading="Connecting with the Context API"` (copié de l'outline `read_block("doc.md")` ou de l'adresse `::`), `_heading_mismatch` **refuse** en accusant à tort un renommage de la section !
* **Correctif déterministe** : Ignorer les `#{1,6}\s+` en tête de `expected_heading` et accepter la correspondance si le titre complet **ou** `section_title(heading)` (sans numéro, replié via `_fold_name`) matche `section_title(expected)`.

### 5.3. Ergonomie Zéro-Tour après `delete_block` : retourner l'état/hash du parent et renuméroter (`patcher.py:5746-5806` & `8284-8355`)
* **Localisation** : `Patcher.delete_symbol` (`patcher.py:5801-5806`) et `Patcher.delete_document_section` (`patcher.py:8346-8355`).
* **Optimisations zéro-tour** :
  1. **Retourner `parent_id`, `parent_content_hash`, `parent_line_count` (et `parent_numbered_text` si taille modérée)** dans `delete_symbol` (quand `sym.parent_id` existe) et dans `delete_document_section` (pour la section parente ou le document), afin que l'agent puisse enchaîner un `edit_block` sur le parent au tour suivant sans `read_block`.
  2. **Retourner `reparented_block_ids` (adresses `::` canoniques après suppression)** dans `delete_document_section` (`patcher.py:8330-8354`) au lieu des seuls titres bruts `survivors`, car la suppression du parent modifie le chemin `::` de tous ses enfants promus.
  3. **Renuméroter les sections frères suivantes après suppression d'une section numérotée** dans `delete_document_section` (si `## 2. Beta` est supprimée entre `## 1. Alpha` et `## 3. Gamma`, décrémenter `3. Gamma -> 2. Gamma` et le rapporter dans `warnings`).
  4. **Lister les chemins des enfants de 1er niveau** lorsqu'un appel `delete_block("fichier.cs")` ou `delete_block("doc.md")` cible un fichier entier (`patcher.py:8243-8254` et `8273-8282`), en prescrivant `delete_block(paths=[...])`.

### 5.4. Verrouillage Concurrent (`_edits_lock`) manquant sur `insert_block`, `insert_symbol` et `delete_block`
* **Localisation** : `Patcher.insert_block` (`patcher.py:7600`), `Patcher.insert_symbol` (`patcher.py:5437`), `Patcher.delete_block` (`patcher.py:8200`).
* **Constat** : Alors que `read_block` (`patcher.py:2542`) et `edit_block` (`patcher.py:2731`) acquièrent `self._edits_lock.acquire(target)`, ni `insert_block`, ni `insert_symbol`, ni `delete_block` ne prennent ce verrou par fichier, exposant les opérations parallèles à des lectures ou réindexations concurrentes non sérialisées.

---

## Tableau Récapitulatif des Chantiers Prioritaires (`insert_block` & `delete_block`)

| Priorité | Cible | Fichier & Lignes | Impact Agent |
| :---: | :--- | :--- | :--- |
| **P0 (Bug S1)** | `insert_symbol` sur `struct`, `interface`, `record`, `enum` sans `anchor` toujours refusé | `patcher.py:5525` vs `5658` | Débloque l'insertion dans tous les types non-`class` |
| **P0 (Bug S1)** | Ordre inversé (LIFO au lieu de FIFO) sur `insert_block(inserts=[...])` | `server.py:1302-1336` | Élimine l'inversion silencieuse de l'ordre des blocs en batch |
| **P0 (Zéro-Tour)** | Ajout de `content_hash` dans la réponse de `insert_symbol` | `patcher.py:5740-5743` | Économise 1 `read_block` après chaque `insert_block` code |
| **P1 (Zéro-Tour)** | Auto-routage `parent="file.cs"` / `namespace` $\to$ conteneur unique pour un membre | `patcher.py:5682-5708` | Élimine le refus `is a MEMBER declaration` (1 à 2 tours gagnés) |
| **P1 (Zéro-Tour)** | Résolution des noms courts sur `anchor` (`parent::anchor` et `file::anchor`) | `patcher.py:5466-5470, 7641-7644` | Élimine les refus `Anchor not found` sur nom court |
| **P1 (Zéro-Tour)** | Généralisation de `#47` : auto-routage de `parent` vers `holder_of(anchor)` en Markdown et Code | `patcher.py:7787-7815, 5479-5506` | Élimine tous les refus `_where_not_a_child` quand `anchor` existe |
| **P1 (Bug S2)** | `_sibling_clash` comparant avec numéro + support numérotation hiérarchique `1.2.` + garde `1, 2, 2, 4` | `patcher.py:5863, 7058-7193` | Supprime les collisions `1.2.` et les doublons `Title (2)` |
| **P1 (Surface)** | Exposition de `paths: Optional[List[str]]` sur `delete_block` MCP + `unify_addresses` sur `results` | `server.py:1359-1375`, `patcher.py:8173` | Active la suppression groupée en 1 tour déjà codée dans `Patcher` |
| **P2 (Zéro-Tour)** | Assouplissement de `expected_heading` (ignorer `#` et numéro) + retour de `parent_content_hash` et `reparented_block_ids` dans `delete_block` | `patcher.py:5889-5910, 5801, 8346` | Évite les faux refus `expected_heading` et le `read_block` post-suppression |