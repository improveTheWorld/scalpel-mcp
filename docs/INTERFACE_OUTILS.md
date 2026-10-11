# Interface des outils -- forme unifiee (28/09/2026)

Derivee des decisions : vocabulaire **path** unique ; separateur **`::`** ; listes **completes**, sans pagination ;
**un seul resolveur** pour tous les points d entree ; les surcharges et le hash ne montent jamais sur la surface.
Reference de conception : `docs/PATCH_READ_BLOCK_PATH.md`.

**Ce document n est PAS une proposition : la majeure partie est en service.** Le `§10.3` ci-dessous est
l **etat de reference de la surface**, verifie contre `src/ast_graph_server/server.py` et
`benchmarks/toolsets.py` le **30/09/2026** ; c est le seul document du depot qui la decrit correctement.

**Ce qui est APPLIQUE** (verifie, avec sa preuve) :

| du document | etat | verification |
| :--- | :--- | :--- |
| `§10.3` — la surface exposee | **FAIT** | 8 decorateurs `@self.mcp.tool` dans `server.py` (l. 418, 532, 601, 810, 889, 915, 1075, 1104) + `build` cote harnais (`toolsets.py:199`) = **9 outils** ; `tests/test_tool_surface_referential.py` |
| `§6` — le staging sort du schema **et du serveur** | **FAIT** | `stage_only` n apparait sur **aucune** des deux surfaces ; la machinerie interne de staging et `database.py` associee ont ete **entierement supprimees du patcheur le 03/10/2026** (`06ad388`) |
| `§5` — `insert_symbol` disparait | **FAIT** | aucun outil de ce nom ; `Patcher.insert_symbol` (`patcher.py`) est appele **en interne** par `insert_block` (`server.py:881`) |
| `§5`/`§8` — `delete_block` fusionne les deux suppressions | **FAIT** | `delete_block(path, expected_heading)` (`server.py:889-913`) ; la nature du bloc decide |
| `§8` — `manage_imports` absorbe | **FAIT** | la region contigue des `using` est un **enfant nomme du fichier de code** : `parsers/csharp_parser.py:126-144` cree `<fichier>.cs::using` ; verifie en direct : `read_block("T.cs::using")` rend le texte numerote et son hash, et `read_block("T.cs")` liste `using [1-2]`. `Patcher.manage_imports` et `imports.py` ont ete **entierement supprimes le 03/10/2026** (`06ad388`) |
| `§10.2` — `list_documents` fusionne dans `search_documents` | **FAIT (cote serveur)** | `server.py:1134-1144` : `query` a `default=""` et une requete vide rend la liste des documents (`self.db.list_documents`) |

**Ce qui n est PAS APPLIQUE** (mesure le 30/09/2026) :

| du document | etat | verification |
| :--- | :--- | :--- |
| `§10.1` — « le parametre `format` disparait de tous les schemas » | **NON FAIT** | `format` est encore **declare sur 5 des 8 schemas** (`build_context`, `get_project_map`, `read_block`, `search_documents`, `search_symbols`), et sur les memes 5 cote serveur. **Mais 0 appel sur 1869 ne l utilise** dans `registered_v2` (`benchmarks/.work/_g1_args_probe2.py`, **gitignore** (`.gitignore:12`) : 40 journaux, 1869 `tool_calls`) : c est `config.answer_format`, fixe au demarrage du run, qui decide (`toolsets.py:983`) |
| `§10.1` — le contrat d erreur en trois lignes (`error:` / `->` / `path:`) | **NON FAIT** | jamais pose : les refus sont des objets JSON `{status, error, actionable_hint}` |
| `§10.2` — « la requete devient optionnelle » | **APPLIQUE COTE SERVEUR, INVISIBLE COTE MODELE** | `server.py:1113` `query: str = Field(default="")` et `search_documents({})` repond `count: 1` avec la liste ; MAIS `benchmarks/toolsets.py:954` porte `"required": ["query"]` — le modele ne peut pas envoyer l appel que le serveur accepte |
| `§10.3` — « aucune fuite d identifiant interne » pour les **noms de champs** | **NON FAIT** | releve en direct : `edit_block` rend `block_id`, `section_id`, `resulting_section_ids`, `created_section_ids`, `file_path` ; `insert_block` rend `parent_id`, `where`, `block_id`, `created_block_id`, `created_section_ids`. L unification a porte sur les **parametres**, pas sur les **champs de reponse** |

**Regle de lecture des sections qui suivent.** `§1` a `§9` sont l **historique** de la conception (etat au
28/09) : elles ne decrivent plus la surface. `§10` est l etat courant, et `§10.3` le tableau de reference.
Les sections `§2`, `§7` et `§9` sont explicitement **remplacees** par `§10` pour ce qui est du schema.

## 1. La regle d adresse, en trois lignes

    dossier/fichier.cs                            un chemin, comme partout
    fichier.cs::Classe::Methode                   un symbole de code
    fichier.md::Section mere::Paragraphe          un paragraphe de document

Aucune autre ecriture. Le serveur replie casse, espaces, accents et ponctuation. Un nom court est accepte
tant qu il est unique dans son fichier ; sinon le refus **nomme les candidates**.

## 2. Les 15 outils (contre 20 aujourd hui) -- historique au 28/09

### Lecture -- 1 outil au lieu de 4

| outil | parametres | ce qu il fait |
| :--- | :--- | :--- |
| `read_block` | `path` (ou `paths`), `format` | liste complete des enfants d un dossier / fichier de code / .md / config ; TEXTE numerote + hash d un symbole ou d un paragraphe ; `paths=[...]` lit plusieurs blocs de texte en UN appel |

Remplace `read_block`, `get_document_section`, `get_document_sections`, `inspect_symbols`, `list_document_sections`.

### Recherche et cadrage -- 4 outils (inchanges dans leur role)

| outil | parametres |
| :--- | :--- |
| `search_symbols` | `query`, `kind`, `file_pattern`, `limit`, `view`, `format` |
| `search_documents` | `query`, `file_pattern`, `limit`, `full_top`, `include_content`, `format` |
| `list_documents` | `file_pattern`, `limit`, `format` |
| `get_project_map` | `file_pattern`, `budget_tokens`, `format` |

### Dossier initial -- 1 outil

| outil | parametres |
| :--- | :--- |
| `build_context` | `query`, `budget_tokens`, `max_level`, `breadth_ratio`, `precision`, `file_pattern`, `format` |

(`precision` et `file_pattern` viennent du lot `docs/PATCH_CARTE_AGENT.md` : ca se pose ici, ca ne se cumule pas.)

### Edition -- 3 outils

| outil | parametres | changement |
| :--- | :--- | :--- |
| `edit_block` | `path`, `ops`, `expected_hash`, `stage_only` | `block_id` -> `path` |
| `insert_block` | `parent`, `anchor`, `text` | `parent_id` -> `parent`, `where` -> `anchor` |
| `insert_symbol` | `parent`, `code`, `position`, `stage_only` | `parent_id` -> `parent` |

### Suppression -- 1 outil au lieu de 2

| outil | parametres | changement |
| :--- | :--- | :--- |
| `delete_block` | `path`, `expected_heading` | fusionne `delete_symbol` et `delete_document_section` : c est la meme operation sur deux natures de bloc |

### Fichiers et index -- 4 outils

| outil | parametres | changement |
| :--- | :--- | :--- |
| `manage_imports` | `path`, `action`, `statement` | `file_path` -> `path` |
| `get_staged_diff` | `path` | `file_path` -> `path` |
| `commit_staged_changes` | `path` | `file_path` -> `path` |
| `discard_staged_changes` | `path` | `file_path` -> `path` |

### Compilation -- 1 outil

| outil | parametres |
| :--- | :--- |
| `build` | (aucun) |

## 3. Le contrat de `read_block`, mot pour mot

```
read_block(path, paths, format)

path est la SEULE adresse d un bloc. Trois cas :

  - un dossier, un fichier de code, un fichier .md, un .csproj/.json/.yaml
      -> la LISTE COMPLETE de ses enfants : le NOM de chacun, sa portee de lignes et sa taille.
         La hierarchie se lit a l indentation. Jamais le texte. Une liste n est jamais tronquee et ne se
         pagine pas : tout ce que le bloc contient est dans la reponse.
  - un symbole de code, un paragraphe de document (leur nom figure dans la liste de leur fichier)
      -> son TEXTE, numerote, avec le hash a passer a edit_block.

Pour lire un enfant : prenez le path que vous venez d utiliser, ajoutez "::", puis le nom tel qu il est
affiche. Un paragraphe imbrique donne la chaine de ses parents, dans l ordre ou ils sont indentes :
    DataLinq/docs/LINQ-to-Spark.md::Quick Start::Connecting with the Context API
Le serveur replie lui-meme la casse, les espaces et les accents : ecrivez le nom comme il est imprime.
Un fichier dans un dossier reste un chemin ordinaire : "dossier/fichier.cs".

paths=[...] lit PLUSIEURS blocs de texte en un seul appel (symboles ou paragraphes, melanges possibles) :
une seule reponse numerotee par bloc, au lieu d un aller-retour par bloc. C est ce qui remplace
get_document_sections et inspect_symbols.
```

## 4. Ce que l agent voit d une liste (forme unique)

```
id: DataLinq.Enterprise/spark/src/DataLinq.Framework.SparkQuery/SparkConnectOptions.cs
    123 lignes, 5606 octets, 13 blocs
    SparkConnectOptions                          L10-122      5285 o
        Config                                       L20            62 o
        OAuthClientId                                L39            46 o
        OAuthClientSecret                            L44            50 o
        BuildAuthenticationConfig                    L64-91       1254 o
        AutoStartBackend                             L121           54 o
```

## 5. REVISION du 28/09 : les symboles ne sont plus un cas particulier

Un symbole n est pas une espece a part : c est **l enfant d un fichier de code**, comme un paragraphe est
l enfant d un document et un fichier l enfant d un dossier. Donc les quatre gestes le traitent comme un bloc,
sans outil dedie :

| geste | qui le fait |
| :--- | :--- |
| lire un symbole | `read_block(<fichier.cs>::Classe::Methode)` |
| editer un symbole | `edit_block(<le meme path>, ops)` |
| supprimer un symbole | `delete_block(<le meme path>)` |
| creer un symbole | `insert_block(<fichier.cs>, anchor, text)` -- **la nature du parent decide de la syntaxe** : du C# sous un fichier de code, du Markdown sous un document, un nom sous un dossier |

`insert_symbol` disparait donc, et son parametre `position` devient l `anchor` commun. Consequence : un seul
jeu de quatre gestes pour toutes les natures de bloc, et l agent n a jamais a savoir qu un symbole demande un
outil different.

## 6. REVISION du 28/09 : le staging sort du schema

Verification demandee par le proprietaire : « l agent n a-t-il pas acces a PowerShell et a git ? »

**Mesure sur 55 runs** (5 campagnes, 2 bras, journaux depouilles) :

| outil | appels | par run |
| :--- | ---: | ---: |
| `commit_staged_changes` | **0** | 0,00 |
| `discard_staged_changes` | **0** | 0,00 |
| `get_staged_diff` | 8 | 0,15 |
| `stage_only=True` (sur n importe quelle edition) | **0** | 0,00 |

La machinerie de staging est **morte** : jamais un commit, jamais un abandon, huit diffs. Elle sort du schema,
avec le parametre `stage_only`.

**Ce que ca change dans le harnais** (a savoir) : dans le banc d essai, le bras Scalpel n a **aucun shell** -- ses
20 outils sont ceux du schema plus `build`, et c est tout (le bras baseline, lui, a `grep`/`read_file`/`edit_file`).
Retirer le staging ne lui retire donc pas un doublon de git : ca lui retire sa seule vue de diff. En pratique
cette vue etait quasi inutilisee (0,15 appel/run), et l agent relit de toute facon ce qu il vient d editer avec
`read_block` (9,2 appels/run) et compile avec `build` (4,6/run). Dans un client reel, le shell et git sont la et
la question ne se pose pas.

## 7. Schema final propose : 11 outils (contre 20)

| # | outil | parametres |
| ---: | :--- | :--- |
| 1 | `read_block` | `path` ou `paths`, `format` |
| 2 | `search_symbols` | `query`, `kind`, `file_pattern`, `limit`, `view`, `format` |
| 3 | `search_documents` | `query`, `file_pattern`, `limit`, `full_top`, `include_content`, `format` |
| 4 | `list_documents` | `file_pattern`, `limit`, `format` |
| 5 | `get_project_map` | `file_pattern`, `budget_tokens`, `format` |
| 6 | `build_context` | `query`, `budget_tokens`, `max_level`, `breadth_ratio`, `precision`, `file_pattern`, `format` |
| 7 | `edit_block` | `path`, `ops`, `expected_hash` |
| 8 | `insert_block` | `parent`, `anchor`, `text` |
| 9 | `delete_block` | `path`, `expected_heading` |
| 10 | `manage_imports` | `path`, `action`, `statement` |
| 11 | `build` | (aucun) |

Reste a trancher : `manage_imports` (0,95 appel/run -- il sert) pourrait lui aussi devenir un bloc si les
directives `using` etaient indexees comme tels, mais rien ne l exige.

## 8. REVISION du 28/09 : `manage_imports` est absorbe lui aussi

Question du proprietaire : « si l interface de `manage_imports` utilise le meme type que celle des blocs, on
unifie et on absorbe ».

**Etat verifie** : aujourd hui les directives `using` ne sont **pas** des blocs -- l index ne contient
**0 symbole de kind `using`** (kinds presents : namespace 552, class 1416, method 6058, property 2099,
constructor 411, record 153, function 123, enum 20, interface 15, struct 7). C est precisement pourquoi l outil
existe, avec sa petite langue a lui (`action=list|add|remove` + `statement`).

**Mais le precedent existe deja, cote documents** : le preambule -- le texte qu aucun titre ne possede -- est un
bloc a part entiere, adresse par le chemin du fichier (`test_document_root.py` : « The indexed preamble row
and the document root are the same block »). Il n y a aucune raison d excepter le haut d un fichier de code.

**Donc** : la region des `using` devient un **enfant nomme du fichier de code**, et les quatre gestes suffisent :

| besoin | aujourd hui | apres |
| :--- | :--- | :--- |
| voir les imports | `manage_imports(action=list)` | `read_block("fichier.cs::using")` -- TEXTE numerote + hash |
| ajouter un import | `manage_imports(action=add, statement=...)` | un **op** dans `edit_block("fichier.cs::using", ops)` |
| retirer un import | `manage_imports(action=remove, statement=...)` | le meme `edit_block`, un op de suppression |

La langue `action`/`statement` disparait, et la region apparait dans la liste du fichier avec sa portee de lignes
et sa taille, comme n importe quel enfant.

**Ce que ca coute, dit franchement** : c est le seul point de cette revision qui demande de toucher un
**parseur** (emettre la region contigue de `using` en tete de fichier C# comme un symbole de kind `using`), plus
ses tests. Les autres points ne changent que la surface. Usage mesure : 0,95 appel/run sur 55 runs, et 7 appels
seulement sur les 6 runs de la derniere campagne (3 list, 2 add, 2 remove) -- petit, mais c est exactement le
genre de residue qui fait six ecritures d adresse differentes.

## 9. (historique) Schema intermediaire a 10 outils

| # | outil | parametres |
| ---: | :--- | :--- |
| 1 | `read_block` | `path` ou `paths`, `format` |
| 2 | `search_symbols` | `query`, `kind`, `file_pattern`, `limit`, `view`, `format` |
| 3 | `search_documents` | `query`, `file_pattern`, `limit`, `full_top`, `include_content`, `format` |
| 4 | `list_documents` | `file_pattern`, `limit`, `format` |
| 5 | `get_project_map` | `file_pattern`, `budget_tokens`, `format` |
| 6 | `build_context` | `query`, `budget_tokens`, `max_level`, `breadth_ratio`, `precision`, `file_pattern`, `format` |
| 7 | `edit_block` | `path`, `ops`, `expected_hash` |
| 8 | `insert_block` | `parent`, `anchor`, `text` |
| 9 | `delete_block` | `path`, `expected_heading` |
| 10 | `build` | (aucun) |

**Les quatre gestes couvrent tout** : lire (1), creer (8), modifier (7), supprimer (9). Les six autres outils
cherchent (2, 3, 4, 5), preparent (6) ou compilent (10).

## 10. REVISION FINALE (28/09) -- 9 outils, et TOUT en texte

> **LIRE CE CHAPITRE EN PREMIER.** C est l ETAT COURANT de la surface (au 30/09/2026, avec ses deux
> reserves mesurees), et le `§10.3` qui suit en est le tableau de reference. Les `§1` a `§9` sont
> l historique de la conception. Etat par point : le tableau en tete de ce document.

Cette section **remplace** les sections 2, 7 et 9 pour ce qui est du schema.

### 10.1 Une seule forme de sortie : du TEXTE, pour tous les outils

Mesure sur les 55 runs, 12 outils les plus appeles : **12,7 M de caracteres lus**, dont **1,6 M (12 %)** de
bruit d enveloppe JSON (cles, ponctuation, identifiants repetes). Par outil : 4 a 10 % sur les gros lecteurs
(recherche, carte, Dossier), **19 a 21 % sur `read_block` et `edit_block`**, **41 a 57 % sur les
reponses courtes** (`insert_block`, `insert_symbol`). Exemple reel : un refus d `edit_block` fait
626 caracteres et ecrit le **meme chemin deux fois** (`section_id` *et* `block_id`).

Mais la vraie raison n est pas le volume, c est la coherence : **la meme information doit avoir la meme forme
partout**. Aujourd hui sept outils declarent `format: text|json` et treize sont JSON-only -- c est une
seconde langue, comme `#` a cote de `::`.

**Donc :** le parametre `format` **disparait de tous les schemas** (un parametre que l agent ne peut pas
juger, meme regle que `offset`), et le contrat d erreur s ecrit en trois lignes :

    error: <la cause, avec le fait verifie>
      -> <la porte de sortie>
    path: <le path, ecrit UNE fois>

### 10.2 `list_documents` fusionne dans `search_documents`

La requete devient **optionnelle** et le filtre s appelle **`path`** (le vocabulaire unifie) :

| appel | ce qu il rend |
| :--- | :--- |
| `search_documents(path="DataLinq.Enterprise/spark")` | les documents de ce sous-arbre, **complets** |
| `search_documents()` | **tous** les documents |
| `search_documents(query="oauth token")` | la recherche d aujourd hui |
| `search_documents(path=..., query=...)` | la recherche **restreinte** a ce sous-arbre |

Faisable parce que la population est petite : **289 documents**, listing complet = **33 057 caracteres**
(113 par ligne en moyenne), tres en dessous du budget d un resultat d outil (200 000). Et ca supprime le
**plafond muet de 200** : aujourd hui `count: 200` sur 289 documents se lit comme « il y en a 200 ».

**L asymetrie est assumee** : le meme tour ne marche PAS pour les symboles (**10 854** -> un listing complet
ferait ~759 k caracteres). Donc `search_symbols` garde une requete **obligatoire** et se contente de renommer
`file_pattern` -> `path`. Ce n est pas une perte : la liste des enfants d un fichier de code, c est
`read_block(<fichier.cs>)`.

### 10.3 Schema final : 9 outils (contre 20)

| # | outil | parametres |
| ---: | :--- | :--- |
| 1 | `read_block` | `path` ou `paths` |
| 2 | `search_documents` | `path`, `query` (optionnelle), `limit`, `full_top`, `include_content` |
| 3 | `search_symbols` | `query` (obligatoire), `path`, `kind`, `limit`, `view` |
| 4 | `get_project_map` | `path`, `budget_tokens` |
| 5 | `build_context` | `query`, `budget_tokens`, `max_level`, `breadth_ratio`, `precision` |
| 6 | `edit_block` | `path`, `ops`, `expected_hash` |
| 7 | `insert_block` | `parent`, `anchor`, `text` |
| 8 | `delete_block` | `path`, `expected_heading` |
| 9 | `build` | (aucun) |

**Les quatre gestes couvrent tout** : lire (1), creer (7), modifier (6), supprimer (8). Les quatre autres
cherchent (2, 3, 4) ou preparent (5), et le dernier compile (9).

**Aucune fuite d identifiant interne** : plus de `file.md#slug` dans une reponse (y compris
`resulting_section_ids` et `created_section_ids`), plus de `#hash` de surcharge, et
`insert_block(anchor=...)` accepte un **nom** (`::`) comme le reste.

> **VERIFIE LE 30/09/2026 -- ce qui tient, et ce qui ne tient pas.** Ce §10.3 est l **etat de reference
> de la surface** : les 9 outils ci-dessus sont exactement ceux que le serveur enregistre (8
> decorateurs `mcp.tool`) plus `build` cote harnais ; `tests/test_tool_surface_referential.py` le
> tient. Deux reserves mesurees, a ne pas escamoter :
> 1. **L adresse `::` tient, les NOMS DE CHAMPS non.** Les parametres sont bien `path`/`parent`/
>    `anchor` ; mais les **reponses** portent encore les noms internes : `edit_block` rend `block_id`,
>    `section_id`, `resulting_section_ids`, `created_section_ids`, `file_path` et `insert_block` rend
>    `parent_id`, `where`, `block_id`, `created_block_id`, `created_section_ids` (releve en direct
>    sur le serveur). Le point 6 de cette section decrit l adresse imprimee, pas le nom du champ.
> 2. **`query` est optionnelle COTE SERVEUR seulement** : `server.py:1113` a `default=""` et
>    `search_documents({})` rend la liste des documents ; `benchmarks/toolsets.py:954` porte encore
>    `"required": ["query"]`, donc le modele ne peut pas l omettre. Meme remarque pour
>    `build_context` : le harnais en declare **dix** (`benchmarks/toolsets.py`,
>    `SCALPEL_SCHEMAS`), le serveur quatre de plus que lui (`map_ratio`, `map_tokens`,
>    `map_detail`, `detail_level`), et `path` n existe sur aucune des deux.
> Enfin, `format` : declare sur 5 des 8 schemas, il n est utilise par **aucun** des 1869 appels de
> `registered_v2` (`benchmarks/.work/_g1_args_probe2.py`, **gitignore** (`.gitignore:12`)) -- c est `config.answer_format` qui decide.
