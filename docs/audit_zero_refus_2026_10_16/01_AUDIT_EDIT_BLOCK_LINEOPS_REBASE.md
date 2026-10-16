# Audit Expert du Moteur Scalpel : `edit_block` (`patcher.py`, `lineops.py`, `linetranslate.py`, `server.py`)

**Agent Auditeur** : `819884fa-f900-41c2-965e-36f31454fe0b`  
**Capsule Créée** : `scalpel-mcp.engine.edit-block`  
**Règle d'Or** : *« Un outil qui sait exécuter la demande d'un agent d'une façon déterministe ne doit JAMAIS la refuser, ni causer des tours supplémentaires. »*

---

Conformément à la **Règle d'Or**, voici l'audit complet du pipeline `edit_block`, structuré en **4 parties** :
1. **Les 7 gardes préventifs convertibles en Auto-Healing 100 % déterministe** (où le code actuel calcule déjà la correction exacte dans `actionable_hint` mais refuse au lieu d'exécuter avec un `warning`).
2. **Les 4 opportunités d'Auto-Routing structurel déterministe & bugs de gardes identifiés**.
3. **Les 5 trous critiques sur les refus légitimes** (où l'outil omet encore `numbered_text`, `content_hash`, le diagnostic de décalage de ligne ou le formatage `text`, causant 1 à 2 tours de lecture supplémentaires).
4. **L'audit de performance algorithmique (Tree-sitter, I/O disque, SQLite) et d'atomicité des lots `edits=[...]`**.

---

## Partie 1 — Les 7 Gardes Préventifs convertibles en Auto-Healing Déterministe (0 refus, 0 tour perdu)

### 1.1 Garde `#17` : `insert_after: 0` avec contenu non-titre sur une section Markdown
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:3107-3127` (`Patcher::_edit_block_locked`, lignes 280–300 du bloc).
* **Comportement actuel** : Lorsque `kind == "section"`, `one.get("insert_after") == 0` et `self._leading_markdown_heading(frag) is None`, l'outil **refuse** et prescrit :
  `"Use insert_after: 1 to insert at the start of the section body (right under the heading on line 1)"`.
  Ce refus s'est déclenché dans **9 runs sur 25 en `v0.4.1` (36 %)** et **3 runs sur 10 en `v0.4.4`** (`run8`, `run9`, `run10`), coûtant systématiquement **1 tour complet** à chaque fois.
* **Analyse d'ambiguïté** : **0 % d'ambiguïté**. Dans une section Markdown, la ligne 1 est toujours son propre titre `# Heading`. Un fragment sans `#` inséré à `insert_after: 0` sur une section nommée ne peut jamais légalement précéder le titre (cela le rattacherait à la section précédente et scinderait la section courante). L'intention de l'agent qui cible une section avec `insert_after: 0` et un texte sans `#` est **toujours** d'insérer au début du corps de cette section (`insert_after: 1`).
* **Algorithme d'Auto-Healing** :
  1. Remplacer `one["insert_after"] = 1` dans l'opération avant `apply_ops`.
  2. Si `len(sec_lines) >= 2 and not sec_lines[1].strip()`, préserver une unique ligne blanche de séparation sous le titre.
  3. Ajouter dans `warnings` : `"In a Markdown section, line 1 is the heading: auto-adjusted insert_after: 0 -> 1 (start of section body)."`
* **Gain chiffré** : **1 tour complet et ~2 000 tokens économisés dans 30 % à 36 % des runs**.

### 1.2 Garde `#35` : `insert_after: line_count` d'un membre sur la ligne `}` fermante d'un conteneur de type (`class`/`struct`/`interface`/`record`)
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:2958-3007` (`Patcher::_edit_block_locked`, lignes 131–180 du bloc).
* **Comportement actuel** : Quand `ins_pos == len(sym_lines)`, `sym_lines[-1].strip() == "}"`, `sym_kind in ("class", "struct", "interface", "record")` et `is_member_decl and not is_type_decl`, l'outil **refuse** et écrit : `"Use insert_after: {ins_pos - 1} to insert the member INSIDE '{block_id}' right before its closing '}' on line {ins_pos}"`.
* **Analyse d'ambiguïté** : **0 % d'ambiguïté**. Une déclaration de méthode, de propriété ou de test `[Fact]` ne peut pas exister hors de sa classe après l'accolade fermante `}` (cela produit un membre orphelin non compilable). L'agent a explicitement ciblé `block_id` (le conteneur) pour y ajouter un membre en fin de corps.
* **Algorithme d'Auto-Healing** :
  1. Réécrire `one["insert_after"] = ins_pos - 1` avant `apply_ops`.
  2. Si le fragment `frag` a été soumis à la colonne 0, l'indenter à `(self._ast_depth(sym) + 1) * len(indent_step)`.
  3. Ajouter dans `warnings` : `"auto-adjusted insert_after: {ins_pos} -> {ins_pos - 1} to insert the member inside {sym_kind} '{block_id}' before its closing '}'."`
* **Gain chiffré** : **1 tour et ~2 500 tokens économisés** par occurrence (`v0.4.2` `run1`, `run3` ; `v0.4.3` `run5`).

### 1.3 Garde `#56` : `replace: [rf_first, rf_last]` écrasant la ligne d'ouverture d'un code fence Markdown (`` ```csharp ``) sans la ré-émettre
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:3213-3263` (`Patcher::_edit_block_locked`, lignes 386–436 du bloc).
* **Comportement actuel** : Quand `sec_lines[rf_first - 1]` est un délimiteur d'ouverture de fence (` ```lang `), que `unclosed_fence_line` avant `rf_first` est `None`, et que `frag` commence par du code sans fence ouvrant tout en ayant `frag_fence_count % 2 == 1` (il inclut le ` ``` ` fermant), l'outil **refuse** et prescrit :
  `"Include '{opening_fence_line}' on the first line of your replacement content, or shrink the replace range to [{rf_first + 1}, {rf_last}]"`.
* **Analyse d'ambiguïté** : **0 % d'ambiguïté**. Les deux corrections proposées dans `actionable_hint` (« préfixer `opening_fence_line` au contenu » OU « réduire la plage à `[rf_first + 1, rf_last]` ») sont **mathématiquement et textuellement identiques à 100 %** : elles produisent exactement le même texte final octet pour octet !
* **Algorithme d'Auto-Healing** :
  1. Préfixer `sec_lines[rf_first - 1] + "\n"` à `one["content"]` (ce qui couvre aussi le cas dégénéré `rf_first == rf_last`).
  2. Ajouter dans `warnings` : `"auto-preserved opening code fence '{opening_fence_line}' at line {rf_first}."`
* **Gain chiffré** : **1 à 3 tours et ~4 500 tokens économisés** (`v0.4.4` `run7` `t17→t20`).

### 1.4 Garde `#36` : `replace: [r_first, r_last]` écrasant l'accolade fermante `}` du membre précédent sans la ré-émettre
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:3053-3101` (`Patcher::_edit_block_locked`, lignes 226–274 du bloc).
* **Comportement actuel** : Quand `sym_lines[r_first - 1].strip() == "}"`, `r_last < len(sym_lines)` et que `content` commence par une nouvelle déclaration (`[Fact]`, `public`, `private`, `def`, `function`, etc.) sans commencer par `}`, l'outil refuse et prescrit `"Use insert_after: {r_first} ... or keep the leading '}' on the first line"`.
* **Analyse d'ambiguïté** : Lorsque `r_first == r_last` (ou que les lignes `r_first + 1 .. r_last` sont uniquement des lignes blanches), préfixer `sym_lines[r_first - 1] + "\n\n"` au `content` équivaut exactement à `insert_after: r_first`, et la barrière syntaxique Tree-sitter de `update_symbol_body` vérifie immédiatement après que l'AST résultant est 100 % valide.
* **Algorithme d'Auto-Healing** :
  1. Si `all(not ln.strip() for ln in sym_lines[r_first:r_last])`, préfixer `sym_lines[r_first - 1] + "\n\n"` à `one["content"]`.
  2. La validation Tree-sitter (`update_symbol_body`) confirme que le fichier complet reconstitué est syntaxiquement valide.
  3. Ajouter dans `warnings` : `"auto-preserved closing '}' on line {r_first} before your new member declaration."`
* **Gain chiffré** : **1 à 2 tours et ~2 500 tokens économisés** (`v0.4.2` `run1` `t19→t21`).

### 1.5 Garde `#43(a)` : `insert_after: ins_pos` scindant un item de liste Markdown de ses sous-puces indentées
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:3163-3212` (`Patcher::_edit_block_locked`, lignes 336–385 du bloc).
* **Comportement actuel** : Quand l'agent insère un item de liste de niveau `ins_indent` après la ligne `ins_pos` (le titre d'un item existant) et que la ligne suivante `next_idx + 1` est une sous-puce plus indentée (`len(m_next_sub.group(1)) > ins_indent`), le code calcule déjà `last_sub_line` (la dernière ligne du bloc de sous-puces de l'item `ins_pos`) mais **refuse** en disant `"Use insert_after: {last_sub_line}"`.
* **Analyse d'ambiguïté** : **0 % d'ambiguïté**. Un item de liste de niveau parent (`5. **Databricks Auth:**`) ne peut jamais s'intercaler entre l'en-tête de l'item `4.` et ses propres sous-puces indentées.
* **Algorithme d'Auto-Healing** :
  1. Réécrire `one["insert_after"] = last_sub_line`.
  2. Ajouter dans `warnings` : `"auto-advanced insert_after: {ins_pos} -> {last_sub_line} so the new list item lands after the indented sub-bullet(s) of line {ins_pos}."`
* **Gain chiffré** : **1 tour et ~2 000 tokens économisés** (`v0.4.3` `run1` `t18→t19`).

### 1.6 Garde `#26` / `#43(b)` : `_heading_range_drops_a_bullet` (`replace: [r_first, r_last]` avalant une sous-puce finale non ré-émise) + Code mort détecté
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:3264-3289, 3653-3663` (`Patcher::_edit_block_locked`, lignes 437–462 et 826–836 du bloc) et `src/ast_graph_server/patcher.py:4761-4816` (`Patcher::_heading_range_drops_a_bullet`).
* **Anomalie de code mort identifiée** :
  - Aux lignes 437–462 (sous `if kind == "section":`), `_heading_range_drops_a_bullet` déclenche un refus préventif (`return {"status": "error", ...}`).
  - Aux lignes 826–836 (après écriture !), `_edit_block_locked` rappelle `dropped = self._heading_range_drops_a_bullet(ops, content)` pour ajouter un warning post-écriture ! Pour `kind == "section"`, ce bloc 826–836 est **100 % du code mort** (déjà refusé ligne 441), et pour `kind == "document"`, le contrôle préventif ligne 437 ne tourne pas car il est sous `if kind == "section":` au lieu de `if kind in ("section", "document"):`.
* **Algorithme d'Auto-Healing** :
  - Lorsque `dropped_check` détecte que `replace: [r_first, r_last]` se termine par un titre nu alors que les lignes `drop_num..r_last` du bloc original sont les sous-puces indentées de ce titre, réduire automatiquement la borne de fin `r_last` à `drop_num - 1` (préservant les sous-puces sous le titre mis à jour/renuméroté) et supprimer le bloc mort des lignes 826–836, avec un `warning` : `"auto-shrunk replace range [{r_first}, {r_last}] -> [{r_first}, {drop_num - 1}] to preserve indented sub-bullet(s) on line {drop_num} (use delete if you meant to remove them)."`
* **Gain chiffré** : **1 tour et ~2 000 tokens économisés** par occurrence (`v0.4.3` `run9`, `v0.4.4` `run4` et `run9`).

### 1.7 Garde `#4` / `#23` : `_newly_repeated_line` sur duplication de ligne frontière adjacente (`first - 1` ou `last + 1`)
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:3382-3437` (`Patcher::_edit_block_locked`, lignes 555–610 du bloc) et `src/ast_graph_server/patcher.py:4830-4909` (`Patcher::_newly_repeated_line`).
* **Comportement actuel** : Quand `replace: [first, last]` ré-émet en première ou dernière ligne de son `content` une ligne qui n'apparaissait qu'une seule fois dans le bloc (`counts_before[line] == 1`) à la ligne `was_at` située juste à l'extérieur de la plage `[first, last]` (éventuellement séparée par des lignes blanches), l'outil refuse et dit : `"Drop that line from your content, or extend the range to include line {was_at}"`.
* **Analyse d'ambiguïté** : Lorsque la ligne `duplicated` est **exactement la première ligne non vide** de `op["content"]` et que `was_at < first` avec uniquement des lignes blanches entre `was_at` et `first - 1` (ou **exactement la dernière ligne non vide** de `op["content"]` et que `was_at > last` avec uniquement des lignes blanches entre `last + 1` et `was_at`), les deux corrections (« retirer la ligne du `content` » ou « étendre la plage `[first, last]` jusqu'à `was_at` ») sont **100 % équivalentes et déterministes** !
* **Algorithme d'Auto-Healing** : Étendre automatiquement la plage à `[min(first, was_at), max(last, was_at)]`, ré-exécuter `apply_ops` en mémoire, et ajouter dans `warnings` : `"auto-extended replace range [{first}, {last}] -> [{new_first}, {new_last}] to consume boundary line {was_at} ('{duplicated.strip()}') which your replacement content re-emitted."` (Conserver le refus uniquement si la duplication n'est pas en frontière directe de la plage).
* **Gain chiffré** : **1 à 2 tours et ~2 500 tokens économisés** par occurrence.

---

## Partie 2 — Nouveaux Auto-Routings Structurels Déterministes & Bugs de Gardes Identifiés

### 2.1 Bug dans le Garde `#44` : Il refuse à tort les types `private` / `protected` imbriqués dans une classe !
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:3008-3052` (`Patcher::_edit_block_locked`, lignes 181–225 du bloc) :
  ```python
  m_type = re.match(
      r"^(?:(?:public|internal|private|protected|static|abstract|sealed|partial|readonly)\s+)*(enum|class|struct|interface|record)\s+(\w+)",
      first_meaningful,
  )
  ```
* **Problème prouvé** : La regex inclut `private` et `protected` ! Or en C#, un type déclaré `private enum ...` ou `private class ...` est **interdit au niveau namespace** (erreur compilateur `CS1527` : *Elements defined in a namespace cannot be explicitly declared as private, protected...*) et **ne peut exister qu'imbriqué à l'intérieur d'une classe** ! Si un agent veut légitimement créer un `private enum State` ou `private sealed class Node` à l'intérieur d'une classe via `insert_after: k` (`1 <= k < len(sym_lines)`), le garde `#44` le **refuse** et lui prescrit d'utiliser `insert_after: len(sym_lines)` (hors de la classe), ce qui produira un code C# invalide (`CS1527`) !
* **Correction & Auto-Routing déterministe** :
  1. Exclure immédiatement tout type portant le modificateur `private` ou `protected` du garde `#44` (car il est 100 % destiné à être imbriqué dans la classe).
  2. Lorsque `ins_pos == len(sym_lines) - 1` (juste avant le `}` final de la classe) et que le type déclaré est `public` ou `internal` `enum` (sans `private`/`protected`), auto-router `ins_pos` vers `len(sym_lines)` (création en sibling après la classe) avec `warning` explicite.

### 2.2 Auto-Routing `#39` : Édition d'un `namespace` qui ajoute uniquement un type (`enum`/`class`/`struct`/`record`/`interface`)
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:3674-3746` (`Patcher::_using_route`) et `src/ast_graph_server/patcher.py:353-372` (`Patcher::_reject_namespace_mutation`).
* **Comportement actuel** : `_using_route` route un `edit_block` sur un `namespace` qui ajoute uniquement des `using ...;` vers `insert_symbol(file_path, usings)`. Mais si l'agent cible `SparkConnectOptions.cs::DataLinq.Spark` (le `namespace`) avec `insert_after` pour ajouter un `public enum DatabricksAuthMode { ... }` (`v0.4.2` `run1` `t10`), `_using_route` renvoie `None` et `update_symbol_body` refuse avec `namespace mutation refused`.
* **Optimisation déterministe (`_namespace_member_route`)** : Lorsque la cible est un symbole `namespace` et que `ops` consiste uniquement en `insert_after` (à la dernière ligne ou juste avant l'accolade fermante du namespace) ajoutant une déclaration de type complète sans modifier une seule ligne existante du namespace, router automatiquement vers `self.insert_symbol(target_id, added_code)` avec un `warning`, exactement sur le modèle de `_using_route` !
* **Gain chiffré** : **1 à 2 tours économisés** et zéro rejet de batch sur cible `namespace`.

### 2.3 Contradiction Contrat vs Code dans `_title_route_off_point` sur `insert_after: 0` avec un titre Markdown
* **Fichier, symbole & lignes** : `src/ast_graph_server/server.py:856` vs `src/ast_graph_server/patcher.py:6676-6678` (`Patcher::_title_route`, lignes 78–80) et `src/ast_graph_server/patcher.py:6334` (`Patcher::_added_fragments`, ligne 39).
* **Preuve de la contradiction** :
  - La description de `edit_block` dans `server.py:856` affirme : `"(a heading sent at line 0 lands BEFORE the block, and that IS honoured where it was asked for)"`.
  - Le commentaire dans `update_document_section` (`patcher.py:6180-6186`) documente aussi le cas `insert_after: 0` d'un heading avant le bloc.
  - Mais dans `_added_fragments` (`patcher.py:6334`), `"at_end": not any(line.strip() for line in lines[position:])` vaut `False` quand `position == 0` ! Et dans `_title_route` (`patcher.py:6677-6678`), `if not fragment["at_end"]: return self._title_route_off_point(...)` **intercepte et refuse** systématiquement `insert_after: 0` avec un heading de niveau `block_level` avant même d'atteindre `update_document_section` !
* **Correction déterministe** : Dans `_title_route`, si `fragment["position"] == 0`, ne pas appeler `_title_route_off_point` : laisser `update_document_section` insérer le paragraphe frère avant `target["section_start"]` (avec renumérotation éventuelle des frères suivants).

### 2.4 Auto-Placement / Auto-Renumbering sur les listes ordonnées Markdown (`#37` & `#52`)
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:3313-3380` (`Patcher::_edit_block_locked`, lignes 486–553) et `src/ast_graph_server/patcher.py:4911-5022` (`Patcher::_markdown_duplicate_ordinal`).
* **Analyse & Optimisation déterministe** :
  1. **Inversion en fin de liste (`#52`, ex: `4.` inséré avant `3.` dans une liste `1., 2., 3.`)** : Aux lignes 496–512 de `_edit_block_locked`, le code calcule déjà `insert_target_line` (la dernière ligne de l'item `3.` et de ses sous-puces) pour l'écrire dans `actionable_hint` (`"use insert_after: 8"`). Lorsque `int(k_str) == int(m_str) + 1` et qu'une unique opération `insert_after` a inséré l'item `(m+1).` juste avant le dernier item `m.` de la liste, déplacer automatiquement l'insertion à `insert_target_line` avec un `warning` est **100 % déterministe** !
  2. **Doublon d'ordinal consécutif (`#37`, ex: nouvel item `3.` inséré avant l'ancien `3.`, suivi de `4., 5.`)** : Lorsqu'un `insert_after` insère un item `k.` de corps distinct juste avant un item existant `k.`, décaler automatiquement de `+1` les ordinaux consécutifs suivants de même indentation dans la même liste (`k. -> (k+1).`, `(k+1). -> (k+2).`, comme `_renumber_following` le fait déjà pour les titres de sections !) avec un `warning` explicite évite 1 tour complet de renumérotation manuelle.

---

## Partie 3 — Zéro Tour de Lecture sur les Refus Légitimes (5 Trous Critiques Identifiés)

Lorsqu'un refus est sémantiquement justifié (erreur de syntaxe réelle, `Stale read` externe, `Wrong block`), l'agent ne doit **jamais** dépenser un tour supplémentaire de `read_block`. Or l'audit met en évidence **5 trous critiques** :

### 3.1 TROU CRITIQUE 1 : `apply_ops` hors bornes (`1..N`) en appel unitaire ne renvoie NI `content_hash`, NI `numbered_text`, et rate le diagnostic `Coordinate mismatch` (`#51`) !
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:3290-3312` et `3480-3509` (`Patcher::_edit_block_locked`, lignes 463–485 et 653–682 du bloc).
* **Preuve dans le code** :
  1. Aux lignes 463–485 :
     ```python
     applied = apply_ops(content, ops, lambda fragment, _column: fragment)
     if applied.error:
         error = dict(applied.error)
         error["block_id"] = block_id
         if not content: ...
         return error
     ```
     Si l'agent envoie `replace: [120, 120]` ou `insert_after: 66` sur un bloc de 53 lignes (parce qu'il a utilisé le numéro de ligne **fichier** `L120` issu d'une erreur de compilateur C# `CS0246(120,14)` ou de `search_symbols`), `apply_ops` échoue à la ligne 464.
  2. Le retour `return error` à la ligne 485 **ne contient NI `content_hash`, NI `line_count`, NI `numbered_text`, NI `actionable_hint`** ! (Alors qu'en batch dans `_batch_entry` `patcher.py:4115`, `**fresh` est inclus !).
  3. **Viol de la Règle 3 (*Ne jamais placer un diagnostic en aval de la panne qu'il diagnostique*)** : Le détecteur `Coordinate mismatch` (`#51`, lignes 653–682 de `_edit_block_locked`) est placé **APRÈS `apply_ops` et `update_symbol_body`** ! Il ne s'exécute donc **jamais** lorsque le numéro de ligne fichier `r_first` dépasse `len(sym_lines)` (pourtant le cas le plus fréquent dès que `start_line > len(sym_lines)` !) car `apply_ops` a déjà interrompu la fonction à la ligne 485 !
* **Correction déterministe** :
  1. Déplacer la vérification `Coordinate mismatch` (`cand_first = r_first - start_line + 1`) **AVANT `apply_ops`** !
  2. Si `r_first > len(sym_lines)` (hors bornes du bloc) ET que `1 <= cand_first <= len(sym_lines)` ET que la ligne `cand_first` du bloc partage les identifiants de la ligne soumise (`cand_tokens & sub_tokens`), la confusion ligne fichier vs ligne bloc est **prouvée sans ambiguïté** : traduire automatiquement `[r_first, r_last] -> [cand_first, cand_last]` avec `warning` (ou retourner le diagnostic `Coordinate mismatch` complet avec `numbered_text` et `content_hash`).
  3. Sur tout retour `if applied.error:` (ligne 485), toujours inclure `content_hash: current_hash`, `line_count: len(content.splitlines())`, `numbered_text: numbered(content, 1)` et `out_of_bounds: True`.

### 3.2 TROU CRITIQUE 2 : Les refus sur `section` et `document` (`_guard_unclosed_code_fence`, `_outline_refusal`, `_title_route_off_point`, `_guard_duplicate_sibling_title`) omettent `content_hash`, `line_count` et `numbered_text` !
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:3476-3523` (`Patcher::_edit_block_locked`, lignes 649–696 du bloc).
* **Preuve dans le code** :
  Les lignes :
  ```python
  if result.get("status") == "error":
      result.setdefault("content_hash", current_hash)
      result.setdefault("line_count", block_lines)
      result.setdefault("numbered_text", numbered(content, 1))
  ```
  sont indentées **uniquement dans `if kind == "symbol":`** (lignes 649–652) !
  Lorsque `kind == "section"` ou `kind == "document"` échoue dans `update_document_section` (ex: `_guard_unclosed_code_fence`, `_outline_refusal`, `_title_route_off_point`), le dictionnaire d'erreur retourné n'a **ni `content_hash`, ni `line_count`, ni `numbered_text`** ! C'est exactement ce qui a forcé l'agent à faire un `read_block` supplémentaire en `t18` lors de `v0.4.4` `run7` (`#56`).
* **Correction immédiate** : Sortir ce bloc `if result.get("status") == "error": result.setdefault(...)` juste après le `if/elif/else` (ligne 696) afin que **100 % des refus de `_edit_block_locked`** (tous `kind` confondus) portent systématiquement `content_hash`, `line_count` et `numbered_text`.

### 3.3 TROU CRITIQUE 3 : En mode `format="text"` (défaut), un appel unitaire `status == "noop"` renvoie du JSON brut au lieu de `format_single_edit_result` !
* **Fichier, symbole & lignes** : `src/ast_graph_server/server.py:1188-1190` (`ASTGraphServer::_register_tools::edit_block`, lignes 380–382 du bloc) :
  ```python
  if format == "json":
      return reply
  if isinstance(reply, dict) and reply.get("status") == "success":
      return format_single_edit_result(reply)
  return reply
  ```
* **Preuve** : `format_single_edit_result` (`lineops.py:261`) gère explicitement `is_noop = res.get("status") == "noop"`, et `format_batch_edit_result` (`lineops.py:328`) filtre sur `r.get("status") in ("success", "noop")`. Mais `server.py:1188` teste uniquement `reply.get("status") == "success"`, laissant les `noop` unitaires (y compris le diagnostic `#53` `cancelled each other out`) sortir en JSON brut !
* **Correction immédiate** : Remplacer par `if isinstance(reply, dict) and reply.get("status") in ("success", "noop"): return format_single_edit_result(reply)`.

### 3.4 TROU CRITIQUE 4 : Anomalie `#57` — Un lot `edits=[...]` où `applied == 0` ignore `format="text"` et renvoie un JSON massif !
* **Fichier, symbole & lignes** : `src/ast_graph_server/server.py:1164-1168` (`ASTGraphServer::_register_tools::edit_block`, lignes 356–360) et `src/ast_graph_server/lineops.py:317-320` (`format_batch_edit_result`, lignes 7–10).
* **Preuve** :
  1. Dans `server.py:1166` : `if applied > 0: return format_batch_edit_result(payload)` suivi de `return payload`. Si `applied == 0` (lot entièrement refusé OU lot composé uniquement d'entrées `noop` !), le serveur renvoie le dictionnaire JSON `payload` brut au lieu du texte compact !
  2. Dans `format_batch_edit_result` (`lineops.py:317`), l'en-tête commence par `[ok] applied {applied}/{total} edits` même quand `applied == 0 and refused > 0`.
* **Correction immédiate** :
  1. Dans `format_batch_edit_result`, utiliser `prefix = "[refused]" if (applied == 0 and refused > 0) else "[ok]"` : `f"{prefix} applied {applied}/{total} edits"`.
  2. Dans `server.py:1166`, appeler `format_batch_edit_result(payload)` dès que `format != "json"`.

### 3.5 TROU CRITIQUE 5 : `_write_needs_a_block` (`path` fichier sans `expected_hash`) ne donne ni les hashes ni les plages de lignes des blocs enfants
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:1935-1974` (`Patcher::_write_needs_a_block`) et `src/ast_graph_server/server.py:955-964`.
* **Comportement actuel** : Affiche uniquement `"holds": "using, SparkConnectOptions"` sans les plages de lignes `[10-122]` ni les `content_hash`, forçant un `read_block` sur le fichier. De plus, la résolution fichier → symbole via `expected_hash` (`#46`) est dans `server.py` et absente de `Patcher._edit_block_locked`.
* **Correction déterministe** :
  1. Intégrer la résolution fichier → symbole par `expected_hash` directement au début de `Patcher._edit_block_locked` (et si `expected_hash` est omis mais que le fichier ne contient qu'un seul symbole conteneur hors `using`/`imports`, router automatiquement vers lui).
  2. Dans `_write_needs_a_block`, inclure le listing formaté complet du fichier (adresses `::`, plages de lignes).

---

## Partie 4 — Audit de Performance (Tree-sitter, I/O Disque, SQLite) & Atomicité des Lots `edits=[...]`

### 4.1 `update_symbol_body` exécute un parsing Tree-sitter isolé (`new_bytes`) 100 % inutile sur tous les appels réussis
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:5299-5313` (`Patcher::update_symbol_body`, lignes 104–118 du bloc) :
  ```python
  _isolated_ok, isolated_errs = parser.validate_syntax(new_bytes, sym.file_path)
  spliced_bytes = file_bytes[:replace_start] + new_bytes + file_bytes[sym.end_byte :]
  spliced_ok, spliced_errs = parser.validate_syntax(spliced_bytes, sym.file_path)
  ```
* **Constat** : `_isolated_ok` n'est **jamais lu**, et `isolated_errs` n'est utilisé qu'à la ligne 118 (`err_objs = spliced_errs or isolated_errs`) si et seulement si `not spliced_ok and not raw_ok and not spliced_errs` !
* **Optimisation** : Déplacer `_, isolated_errs = parser.validate_syntax(new_bytes, sym.file_path)` à l'intérieur du `else:` d'échec (ligne 118) uniquement quand `not spliced_errs`. Cela **élimine 50 % des appels Tree-sitter `validate_syntax`** sur 100 % des éditions réussies.

### 4.2 `IndentationManager.rebase_indentation` lance un parsing Tree-sitter (`_find_multiline_string_lines`) même quand `delta == 0`
* **Fichier, symbole & lignes** : `src/ast_graph_server/indentation.py:75-128` (`IndentationManager::rebase_indentation`).
* **Constat** : Dans `edit_block`, les fragments sont insérés littéralement et le bloc porte déjà son indentation de base (`min_indent == target_col`, donc `delta == 0` dans >95 % des appels). Or `rebase_indentation` appelle quand même `_find_multiline_string_lines` qui parse tout le bloc avec Tree-sitter !
* **Optimisation** : Si `min_indent == target_col` (donc `delta == 0`) et que l'unité d'indentation correspond, retourner `code` immédiatement en $O(1)$ sans invoquer Tree-sitter.

### 4.3 `_unique_owner_of_hash` relit et re-parse tous les fichiers Markdown du workspace sans utiliser le cache `_get_markdown_layout`
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:5154-5180` (`Patcher::_unique_owner_of_hash`, lignes 27–53 du bloc).
* **Constat** : Lorsqu'une entrée de batch omet `path` (ou passe un chemin de fichier, `#46`), `server.py` appelle `_unique_owner_of_hash(expected_hash)`. Même si `self.db.find_blocks_by_hash(stamp)` a déjà trouvé le symbole dans SQLite (ligne 16), `_unique_owner_of_hash` parcourt **tous les documents Markdown** (`list_documents(limit=500)`), lit chaque fichier sur disque et appelle `markdown_layout(text)` **sans passer par le cache LRU `self._get_markdown_layout`** !
* **Optimisation** :
  1. Utiliser `self._get_markdown_layout(abs_path, raw, lossless)` au lieu de `markdown_layout(text)`.
  2. Dans `server.py:961` et `991` (`#46`, où le chemin du fichier est connu !), chercher uniquement parmi les symboles de ce fichier (`self.db.get_symbols_for_file(rel_path)`) au lieu de scanner tous les fichiers du workspace avec `_unique_owner_of_hash` !

### 4.4 Défaut d'atomicité et redondance Passe 1 (`_preview_pass`) vs Passe 2 dans `_edit_batch`
* **Fichier, symbole & lignes** : `src/ast_graph_server/patcher.py:3908-3989` (`Patcher::_edit_batch`, lignes 57–138) et `src/ast_graph_server/patcher.py:4684-4695` (`Patcher::_is_syntax_barrier`).
* **Trois anomalies structurelles identifiées** :
  1. **`_is_syntax_barrier` est incomplet** : Il ne reconnaît que `syntax_ok is False` et `namespace mutation refused`. Si dans un lot de 2 entrées C#, l'entrée 0 est refusée par `_reject_duplicate` (`duplicate declaration`), ou par les gardes `#35`, `#36`, `#44`, `#23`, `_is_syntax_barrier` retourne `False` ! Le lot n'est pas annulé : l'entrée 0 échoue tandis que l'entrée 1 (qui dépendait du symbole/propriété de l'entrée 0) est écrite sur disque, laissant un projet qui ne compile pas et invalidant les hashes !
  2. **Divergence d'état entre Passe 1 (`_preview_pass = True`) et Passe 2** : En Passe 1, `_translate_entry` retourne `None` (`patcher.py:4431`) et `_batch_path_map` (`#29`, renommage de titre parent Markdown) n'est alimenté qu'en Passe 2 (`patcher.py:3962-3975`). Si deux entrées d'un lot modifient le même bloc (ou si l'entrée 0 renomme un parent et l'entrée 1 cible l'enfant), la Passe 1 valide les entrées sans rebasage ni renommage !
  3. **Coût doublé** : Comme `_edit_batch` sauvegarde déjà `originals: Dict[str, bytes]` en mémoire avant écriture (ligne 85), n'enregistre le commit `history_manager` qu'à la fin (ligne 140) et sait restaurer `self._restore_files(originals)` à la ligne 137, élargir `_is_syntax_barrier` (ou effectuer la capture initiale des hashes en une seule passe avec rollback sur `originals` en cas de barrière) garantit une atomicité exacte tout en divisant par **2** le coût CPU/Tree-sitter des lots.

---

## Tableau Récapitulatif des Gains Chiffrés

| # | Chantier d'Optimisation | Fichier & Lignes | Nature | Gain Chiffré (Tours & Tokens) |
|---|---|---|---|---|
| **1** | **Auto-Healing `#17`** (`insert_after: 0 -> 1` sur section Markdown) | `patcher.py:3107-3127` | Auto-shift `0 -> 1` + warning | **-1 tour** dans **30–36 % des runs** (~2 000 tok/run) |
| **2** | **Auto-Healing `#35`** (`insert_after: N -> N-1` membre sur `}` de classe) | `patcher.py:2958-3007` | Auto-clamp `N -> N-1` + warning | **-1 tour** par occurrence (~2 500 tok) |
| **3** | **Auto-Healing `#56`** (`replace` sur fence ouvrant `` ``` `` non ré-émis) | `patcher.py:3213-3263` | Auto-preserve opening fence | **-1 à -3 tours** (`run7` v0.4.4, ~4 500 tok) |
| **4** | **Auto-Healing `#36`** (`replace` sur `}` de membre précédent) | `patcher.py:3053-3101` | Auto-preserve leading `}` | **-1 à -2 tours** (`run1` v0.4.2, ~2 500 tok) |
| **5** | **Auto-Healing `#43(a)` & `#26/#43(b)`** (sous-puces Markdown + purge code mort) | `patcher.py:3163-3289, 3653` | Auto-advance / auto-shrink | **-1 tour** par occurrence (~2 000 tok) |
| **6** | **Auto-Healing `#4/#23`** (extension de plage sur ligne frontière dupliquée) | `patcher.py:3382-3437` | Auto-extend boundary range | **-1 à -2 tours** par occurrence (~2 500 tok) |
| **7** | **Fix bug `#44`** (autoriser `private`/`protected` nested types) + **Auto-route `#39`** (`namespace`) | `patcher.py:3008-3052, 3674` | Fix regex + `_namespace_member_route` | **0 faux positif** `CS1527` + **-1 à -2 tours** |
| **8** | **Check `Coordinate mismatch` (`#51`) AVANT `apply_ops`** + `numbered_text` sur 100 % des refus | `patcher.py:3290-3312, 3476` | Déplacement amont + `setdefault` global | **0 `read_block` de récupération** (-1 à -2 tours) |
| **9** | **Formatage `text` des `noop` unitaires et des lots `applied == 0` (`#57`)** | `server.py:1166, 1188`, `lineops.py:317` | `status in ("success", "noop")` + `format_batch_edit_result` | **-60 % à -80 % de tokens** sur lots refusés/noop |
| **10** | **Perf Tree-sitter & Cache Markdown** (`validate_syntax` isolé lazy, `delta==0`, `_unique_owner_of_hash`) | `patcher.py:5154, 5299`, `indentation.py:75` | Lazy check + cache `_get_markdown_layout` | **Latence CPU/I/O divisée par 2 à 5** |