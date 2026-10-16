# Audit Détaillé de `read_block`, du Résolveur d'Adresses Unifié (`::`) et des Parseurs AST

**Agent Auditeur** : `ad1a73ed-c78b-46f5-bb72-3a589a555ba7`  
**Capsule Créée** : `scalpel-mcp.engine.read-and-parsers`  
**Règle d'or de l'audit** : *« Un outil qui sait exécuter la demande d'un agent d'une façon déterministe ne doit JAMAIS la refuser, ni causer des tours supplémentaires (extra turns). »*

---

## Axe 1 — Lecture de fichiers entiers (`read_block("fichier.cs")`), petits fichiers et code hors-symbole

### 1.1 Petit fichier de code (`< 120` lignes) ou fichier passé dans `read_block(paths=["fichier.cs", ...])` : 1 tour supplémentaire obligatoire
* **Mécanisme et preuve dans le code** :
  - Dans `src/ast_graph_server/patcher.py:2554-2642` (`Patcher._read_block_locked`), l'appel `self.block_metrics(block_id)` invoque `_block_and_kind` (`patcher.py:482-637`), qui retourne `None` pour un chemin de fichier de code (`"src/foo.cs"`).
  - L'exécution bascule dans `_unresolved_block` (`patcher.py:1976-2123`), qui appelle `_file_outline(block_id, absolute)` (`patcher.py:1277-1301`) à la ligne `2039`.
  - `_file_outline` lit **déjà** tous les octets du fichier sur disque (`file_bytes = absolute.read_bytes()`, `patcher.py:1288`) et calcule son `content_hash` (`patcher.py:1298`), mais ne retourne **que la liste des symboles (`children`)**, sans le `numbered_text` ni les `content_hash` des symboles enfants.
  - En outre, dans `render_listing_text` (`patcher.py:1172-1247`) et `_symbol_rows` (`patcher.py:882-897`), la colonne `content_hash` n'est même pas sélectionnée par la requête SQL (`SELECT id, name, parent_id, signature, kind, start_line, end_line, start_byte, end_byte FROM symbols`).
* **Conséquence** :
  - Même pour un fichier de 30 à 120 lignes (ex. un `enum`, une petite classe d'options ou d'exception), `read_block("fichier.cs")` force systématiquement un **2e tour** `read_block("fichier.cs::Symbole")` avant toute édition.
  - Lorsqu'un agent passe un chemin de fichier dans `read_block(paths=["a.cs", "b.cs"])` (`server.py:712-738`), il demande explicitement des blocs de texte en batch mais ne reçoit que des listings nus.
* **Améliorations concrètes** :
  1. **Auto-inlining des petits fichiers (`<= 120` lignes ou 1 seul conteneur top-level)** : Dans `_file_outline` (`patcher.py:1277`) / `render_listing_text` (`patcher.py:1172`), lorsque `line_count <= 120` (ou lorsque le fichier est demandé via `read_block(paths=[...])` et pèse `<= 200` lignes), inclure directement sous l'outline les blocs feuilles/racines complets avec leur en-tête `[h:hash] (N lines) [file Lx-y]` et leur `numbered_text`.
  2. **Affichage de `[h:<hash>]` dans chaque ligne d'outline** : Ajouter `content_hash` au `SELECT` de `_symbol_rows` (`patcher.py:891`) et l'afficher dans `_listing_symbol_line` (`patcher.py:1133`).
  3. **Généralisation de l'alias `::source` / `::file_text` / `::raw`** : Dans `_file_text_block` (`patcher.py:2757-2762`), ne pas restreindre `::raw` / `::text` / `::file_text` aux seuls fichiers `.csproj`/`.xml` : l'autoriser sur tout fichier du workspace pour permettre la lecture et l'édition des zones hors-symboles.

### 1.2 Code hors-symbole (zones mortes entre les spans AST)
* **Preuve dans le code** :
  - Dans `_file_text_block` (`patcher.py:2777-2785`), dès qu'un fichier possède **au moins un** symbole de type `("function", "class", "method", "interface", "struct", "record", "enum")`, `_file_text_block` renvoie `None`.
  - Tout code situé en dehors des spans `[start_byte, end_byte]` de ces symboles devient **totalement invisible à `read_block` et non éditable par `edit_block`** :
    - **C#** (`csharp_parser.py:74-144`) : Attributs d'assembly `[assembly: InternalsVisibleTo("...")]` (`global_attribute_list`), directives `#nullable enable` / `#pragma`, commentaires de licence, et directives `using` placées *à l'intérieur* d'une déclaration `namespace` (`parse_file` ne cherche `using_directive` que sous `tree.root_node.children` à la ligne `97`).
    - **Python** (`python_parser.py:214-430`) : Blocs `if __name__ == "__main__":` ou `if TYPE_CHECKING:` contenant des instructions (seules les `def`/`class` imbriquées sont extraites aux lignes `344-365`, pas le bloc lui-même), appels module-level (`app.include_router(...)`), et **tous les attributs de classe** (`elif parent_id is None:` à la ligne `366` exclut les attributs de classe !).
    - **TS/JS** (`typescript_parser.py:182-631`) : Appels top-level sans fonction callback (`dotenv.config()`, `app.use(cors())`), déclarations `type`, `enum`, `namespace`.

---

## Axe 2 — Résolveur d'adresses `::` : Refus évitables face à un candidat unique déterministe

### 2.1 Chemin de fichier partiel unique dans le workspace (`SparkConnectOptions.cs::SparkConnectOptions`)
* **Preuve dans le code** (`patcher.py:1690-1710`, `_resolution_of` & `patcher.py:2065-2123`, `_unresolved_block`) :
  - Si l'agent appelle `read_block("SparkConnectOptions.cs::SparkConnectOptions")` ou `read_block("patcher.py::Patcher")` sans le préfixe de dossier :
    1. `self._existing_path(file_part)` (`patcher.py:1705`) retourne `None`, donc `_resolution_of` retourne `None`.
    2. Dans `_unresolved_block` (`patcher.py:2065`), `elif symbol is None and self._existing_path(file_part) is not None:` est `False` !
    3. L'exécution tombe dans `_unindexed_path_refusal` (`patcher.py:2123`), où le test `if "#" not in block_id and "::" not in block_id and "/" not in block_id:` (`patcher.py:2424`) est ignoré car `"::" in block_id`.
    4. **Résultat** : Le serveur répond `"No symbol or Markdown section has this identifier, and no file on disk has this path either: check the spelling"` (`patcher.py:2440`), sans aucun candidat, alors que `SparkConnectOptions.cs` est unique dans `self.db.get_indexed_paths()` !
* **Correctif concret** :
  - Ajouter un résolveur de suffixe de chemin `_resolve_unique_file_part(file_part)` dans `_existing_path` / `_resolution_of` (`patcher.py:1705`) : si `file_part` n'existe pas tel quel, chercher dans `self.db.get_indexed_paths()` les chemins qui se terminent par `"/" + file_part.lstrip("/")`. S'il n'y en a **qu'un seul**, l'utiliser directement et signaler le chemin complet dans `address_hint`. S'il y en a plusieurs, les retourner dans `candidates`.

### 2.2 Nom de bloc nu sans `::` (`read_block("SparkConnectOptions")`) unique dans le workspace
* **Preuve dans le code** (`patcher.py:2424-2439`, `_unindexed_path_refusal`) :
  - `read_block("::SparkConnectOptions")` (avec `::` en tête) est résolu automatiquement par `_resolve_workspace_member` (`patcher.py:1663-1688`) et renvoie le texte du bloc dès le 1er tour.
  - En revanche, `read_block("SparkConnectOptions")` (sans `::`) calcule `named = self._blocks_named(block_id)` (`patcher.py:2428`), constate qu'il y a **1 seul bloc** dans le workspace (`"1 block(s) in this workspace carry it"`), et **refuse** quand même la lecture !
* **Correctif concret** :
  - En lecture (`for_write=False`), lorsque `block_id` est un nom court sans `/` ni `::` qui n'est pas un fichier sur disque et que `_resolve_workspace_member([block_id])` (ou `set(self._blocks_named(block_id))`) identifie **exactement 1 bloc**, **servir directement ce bloc** avec `address_hint` indiquant son adresse canonique complète.

### 2.3 Section Markdown avec hiérarchie partielle (`doc.md::H1::H3` ou `doc.md::H3::H4` sous `H1::H2`)
* **Preuve dans le code** (`patcher.py:1730-1748`, `_resolve_section_names`) :
  - Lignes `1730-1734` :
    ```python
    matches = [
        index for index, chain in enumerate(chains)
        if chain and (self._folded_chain(chain) == wanted
                      or (len(chain) > 1 and self._folded_chain(chain[1:]) == wanted))
    ]
    ```
  - Contrairement à `_resolve_symbol_names` (`patcher.py:1837` : `self._folded_chain(chains[row["id"]][-len(names):]) == wanted`, qui accepte **tout suffixe** de la chaîne d'ancêtres), `_resolve_section_names` n'accepte que `chain` entière, `chain[1:]` (omission du seul H1), ou `len(segments) == 1` (`L1735`) !
  - **Deux échecs déterministes** :
    1. Si un document a `H1 -> H2 -> H3 -> H4` et que l'agent demande `doc.md::H3::H4` (`len(segments) == 2`), `chain[1:]` vaut `H2::H3::H4` ($\ne$ `H3::H4`) et `len(segments) != 1` : **refusé**, alors que `doc.md::H4` (moins précis) marche !
    2. Si l'agent demande `doc.md::H1::H3` (en sautant `H2`), ni `chain` ni `chain[1:]` ne matchent : **refusé** !
* **Correctif concret** :
  - Dans `_resolve_section_names` (`patcher.py:1730-1748`) :
    1. Accepter tout suffixe `len(chain) >= len(segments) and self._folded_chain(chain[-len(segments):]) == wanted`.
    2. En repli (si `not matches`), vérifier si les `segments` forment une **sous-séquence ordonnée** de `chain` aboutissant à la feuille `chain[-1]`. Si un seul paragraphe correspond (`len(matches) == 1`), le résoudre automatiquement.

### 2.4 Surcharges (`overloads`), préfixes de `kind` copiés de l'outline, et omission de `candidates` en mode texte batch
* **Preuve dans le code** (`patcher.py:1820-1866`, `patcher.py:2047-2114`, `server.py:723-727`) :
  1. **Omission de `candidates` dans `read_block(paths=[...])` en mode texte (`server.py:723-727`)** :
     - Lorsqu'une entrée de `paths=[...]` échoue, `server.py` imprime `error` et `actionable_hint` (*"The blocks of '...' that are nearest to the name you sent are in 'candidates' (path, name, lines)"*), **mais n'imprime pas `block["candidates"]`** !
  2. **Préfixe de `kind` copié depuis l'outline (`file.py::variable _FENCE` ou `file.py::function foo`)** :
     - `read_block` et `get_project_map` impriment `variable _FENCE` ou `::function foo`. Copier ce segment dans `::` échoue car `_resolve_symbol_names` ne retire pas le mot-clé `variable `/`function `/`class `/`method `.
  3. **Refus sec sur les surcharges en lecture (`len(candidates) > 1`, `patcher.py:2047-2064`)** :
     - En **lecture** (`for_write=False`), lorsque `len(candidates) > 1` désigne les surcharges d'un même nom de méthode dans un fichier et que leur volume cumulé est modéré ($\le 160$ lignes au total), **retourner directement le texte numéroté et le `[h:hash]` de chacune des surcharges** dans la réponse !

---

## Axe 3 — Granularité et Trous d'Angle Mort des Parseurs (`src/ast_graph_server/parsers/`)

### 3.1 `csharp_parser.py` : TOUS les champs (`field_declaration`) C# sont silencieusement ignorés !
* **Bug critique prouvé dans le code** (`csharp_parser.py:48-62, 154-159, 243-246`) :
  - `MEMBER_KINDS` déclare bien `"field_declaration": "field"` et `"event_field_declaration": "event"`.
  - Mais dans `_walk` (`csharp_parser.py:243`), le parseur appelle `name = self._name_of(child, content)` (`csharp_parser.py:154-159`) :
    ```python
    def _name_of(self, node: Any, content: bytes) -> Optional[str]:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
    ```
  - Or, dans la grammaire Tree-sitter C#, un nœud `field_declaration` ou `event_field_declaration` **n'a pas de champ `name` direct** : il contient un enfant `variable_declaration`, qui contient un ou plusieurs `variable_declarator`, lesquels portent le champ `name` (ou premier enfant `identifier`) !
  - **Conséquence** : `self._name_of(child, content)` renvoie **`None` pour 100 % des champs C#** (`private readonly ...`, `public const ...`, `public static readonly ...`), et la ligne `244` (`if not name: continue`) les jette tous !
* **Correctif concret** :
  - Dans `CSharpParser._name_of` (`csharp_parser.py:154`), si `name_node is None` et `node.type in ("field_declaration", "event_field_declaration")`, descendre dans `variable_declaration -> variable_declarator -> child_by_field_name("name")`.

### 3.2 `python_parser.py` : Attributs de classe (`Patcher.ADDRESS_FIELDS`, `CSharpParser.MEMBER_KINDS`, champs Pydantic/dataclass) totalement ignorés
* **Preuve dans le code** (`python_parser.py:366`) :
  - Dans `_extract_symbols_and_calls`, l'extraction des `assignment` est restreinte par `elif parent_id is None:` (`python_parser.py:366`).
  - **Constat vécu durant cet audit** : Pour inspecter `CSharpParser.MEMBER_KINDS` dans `csharp_parser.py` ou `Patcher.ADDRESS_FIELDS` dans `patcher.py`, aucun symbole `CSharpParser::MEMBER_KINDS` ou `Patcher::ADDRESS_FIELDS` n'existe ! Dans `patcher.py`, lire un attribut de classe oblige à lire la classe `Patcher` entière (**8 375 lignes**).
* **Autre bug dans `python_parser.py::parse_file`** (`python_parser.py:103-114`) :
  - La collecte de `::imports` ne fait `break` que sur `class_definition`, `function_definition`, `decorated_definition` ou `assignment`. Si un module contient un `if`/`try` entre deux `import` (ex. `try: import uvloop except ImportError: pass`), `head` fusionne les imports avant et après le `try`, et le span `[head[0].start_byte, head[-1].end_byte]` englobe le `try/except` ! Il faut faire `break` sur tout nœud autre qu'`import_statement`, `import_from_statement`, `future_import_statement`, `comment`, ou la docstring initiale.

### 3.3 `typescript_parser.py` : `type`, `enum`, `namespace`, champs de classe et `::imports`
* **Preuve dans le code** (`typescript_parser.py:100-119, 182-631`) :
  1. **`type_alias_declaration`, `enum_declaration`, `internal_module` / `module` ignorés** : Un fichier TypeScript déclarant `export type Config = { ... }` ou `export enum Mode { ... }` ne produit **aucun symbole** !
  2. **Effet domino sur `::imports`** (`typescript_parser.py:101`) : `if head and symbols:` n'extrait `::imports` que si `symbols` est non vide. Un fichier `.ts` contenant des `import` et des `export type` / `export interface` a `symbols == []` (pour `type`), ou si tous les `import` ne sont pas contigus (`head = [c for c in tree.root_node.children if c.type == "import_statement"]` sans `break` sur déclaration intermédiaire), `head[0]..head[-1]` englobe du code non-import !
  3. **Champs de classe (`public_field_definition` / `field_definition`) et `get`/`set`** : Dans `class_body`, les propriétés de classe ne sont pas extraites, et un couple `get prop()` / `set prop(v)` produit deux méthodes de même nom `prop` sans distinction `get`/`set` dans le label.
  4. **Destructuration top-level (`const { a, b } = ...`)** (`typescript_parser.py:443`) : `if name_node.type == "identifier":` ignore `object_pattern` et `array_pattern`.

### 3.4 `json_parser.py` et `yaml_parser.py` : Conflit avec `_structured_summary` et `_file_text_block`
* **Preuve dans le code** (`patcher.py:1354-1409, 2769`) :
  1. **Impossibilité de lire/éditer le texte complet d'un `.json` ou `.yaml`** :
     - Dans `_file_text_block` (`patcher.py:2769`), `if not is_xml_project and self._structured_summary(lookup_id, abs_file) is not None: return None` bloque toute résolution en `file_text` pour `.json`, `.yaml` et `.yml` (même avec `::raw` / `::text`).
     - Pire : si un fichier `.json` a une liste de primitives au top-level (`["a", "b", "c"]`), `_structured_summary` (`patcher.py:1369-1377`) renvoie **`status: "error"`** (`"is valid JSON, but its top level is a list, which has no keys to list"`), rendant le fichier **100 % illisible et inéditable** par Scalpel !
  2. **Clés JSON/YAML de ponctuation (`"."`, `"./*"`) impossibles à résoudre via `::`** :
     - Dans `_fold_name` (`patcher.py:1559`), `re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()` transforme la clé `"."` (très courante dans `package.json -> exports -> "."`) en `""` (chaîne vide). `read_block("package.json::exports::.")` est donc impossible à résoudre !
     - *Correctif* : Dans `_fold_name`, si le résultat alphanumérique est vide mais que l'entrée `text.strip()` ne l'était pas, conserver `text.strip()`.

---

## Axe 4 — Performance de `read_block(paths=[...])` sur de grands lots (20 à 50 blocs)

### 4.1 Explosion des connexions SQLite (~400 connexions et ~1 600 `PRAGMA` pour 50 blocs)
* **Preuve dans le code** (`database.py:117-150`, `DatabaseManager.get_connection`) :
  - `get_connection()` ouvre à **chaque appel** un nouveau `sqlite3.connect()` et exécute **4 requêtes `PRAGMA`** (`busy_timeout`, `journal_mode = WAL`, `synchronous = NORMAL`, `foreign_keys = ON`).
  - Pour **chaque** élément `one` de `paths` (ex: `file.cs::Class::Method`, alors que l'ID stocké en base est `file.cs::Class.Method` avec un point `.`) :
    1. `_block_and_kind` appelle `self.db.get_symbol(one)` (`patcher.py:501`) $\rightarrow$ **Connexion #1**.
    2. `_resolve_displayed_path(one)` rappelle `self.db.get_symbol(one)` (`patcher.py:1658`) $\rightarrow$ **Connexion #2** (redondante avec la ligne 501 !).
    3. `_resolve_symbol_names` appelle `_symbol_rows(file_part)` (`patcher.py:1822`) $\rightarrow$ **Connexion #3** (`SELECT ... FROM symbols WHERE file_path = ?`).
    4. `_block_and_kind` appelle `self.db.get_symbol(resolved)` (`patcher.py:507`) $\rightarrow$ **Connexion #4**.
    5. Dans `server.py::_text_block` (`server.py:773`), `self._referenced_files(payload["numbered_text"])` appelle `self.db.get_indexed_paths()` (`server.py:361`) $\rightarrow$ **Connexion #5** (`SELECT file_path FROM indexed_files` sur toute la base)... **alors que le champ `payload["referenced_files"]` est ignoré et jeté aux lignes `777-805` en mode `format="text"` (le mode par défaut) !**
    6. `self.patcher.unify_addresses(payload, used=[one])` (`server.py:776`) appelle `displayed_id` sur `payload["id"]` ET `_address_hint` $\rightarrow$ `displayed_id(one)` (`patcher.py:8150, 8183`), qui rappellent chacun `self.db.get_symbol(...)` et `self._symbol_rows(symbol.file_path)` $\rightarrow$ **Connexions #6, #7, #8, #9** !

### 4.2 Le cache `_get_markdown_layout` (Lot 2C) est contourné en 5 endroits de `patcher.py` !
* **Preuve dans le code** :
  - Bien que `Patcher._get_markdown_layout` (`patcher.py:664-675`) ait été créé pour mémoïser `markdown_layout`, **5 méthodes appelées pendant `read_block` appellent encore directement `markdown_layout(text)` sans passer par le cache** :
    1. `_live_section` (`patcher.py:413`)
    2. `_resolve_section_names` (`patcher.py:1717`)
    3. `_near_addresses` (`patcher.py:2199`)
    4. `displayed_section_id` (`patcher.py:8018`)
    5. `document_addresses` (`patcher.py:8078`)
  - De plus, pour **1 seule** section Markdown lue via `read_block("doc.md::Section")`, le fichier Markdown est lu sur disque (`read_bytes()`) **5 fois de suite**. Sur un lot de 20 sections, cela représente **100 lectures disque et 60 parsings Markdown complets non cachés** !

### 4.3 Plan d'action concret pour l'Axe 4
1. **Connexion SQLite persistante par thread (`threading.local()`)** dans `DatabaseManager.get_connection()` (`database.py:117`) : configure `WAL` et `busy_timeout` une seule fois à l'ouverture de la connexion du thread.
2. **Court-circuiter `self._referenced_files(...)` en mode `text`** dans `server.py::_text_block` (`server.py:772`) : ne l'exécuter que si `effective_format == "json"`.
3. **Remplacer les 5 appels directs `markdown_layout(text)` par `self._get_markdown_layout(...)`** dans `patcher.py` (`L413`, `L1717`, `L2199`, `L8018`, `L8078`).
4. **Ajouter un contexte de lecture groupée dans `Patcher` (`read_blocks(paths, answer_format)`)** : mémoïser `(raw_bytes, symbol_rows, symbol_labels, symbol_id_chains)` par `file_path` le temps de l'appel `read_block(paths=[...])` sous un verrou par fichier distinct.