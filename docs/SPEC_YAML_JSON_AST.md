

# Spécification technique : Gestion AST YAML et JSON dans Scalpel

Date : 10/10/2026. Auteur : Agent d'ingénierie Scalpel.

## 1. Contexte et clarification majeure (Constat F7)
L'enquête a permis de lever une ambiguïté centrale sur le constat F7 (YAML indexé mais inadressable) :
Scalpel possède DÉJÀ un parseur AST YAML (YAMLParser) et un parseur AST JSON (JSONParser) opérationnels, basés sur tree-sitter (src/ast_graph_server/parsers/yaml_parser.py et json_parser.py).
Les tests existants (tests/test_yaml_editing.py et tests/test_json_yaml_array_indexing.py) confirment que :
- Les clés YAML/JSON sont indexées comme symboles AST (propriétés/mappings).
- L'adressage read_block('docker-compose.yml::services.web') fonctionne.
- L'édition edit_block('docker-compose.yml::services.web.image', ops=[...]) fonctionne.

## 2. Pourquoi le trou F7 est apparu
Lorsqu'un agent ou un outil appelle read_block('fichier.yml') sans clé :
1. Dans patcher.py (_unresolved_block), la méthode _structured_summary est appelée en priorité avant _file_outline.
2. Pour .yaml et .yml, _structured_summary extrait les clés via une simple regex et renvoie top_level_keys sans tableau children ni actionable_hint expliquant que les clés sont adressables par fichier.yml::cle.
3. L'agent suppose alors que le fichier n'a pas de bloc adressable et tente edit_unindexed, qui refuse car le fichier est indexé.

## 3. Ce qui reste à implémenter côté Moteur (Lot E2)
1. Exposer les symboles YAML/JSON dans le listing de fichier : faire en sorte que read_block sur un fichier YAML/JSON retourne les enfants ou un actionable_hint clair indiquant l'adressage par ::clé.
2. Écart D5 (saut de ligne final) : dans patcher.py::_split_block_text, body_text = '\n'.join(...).strip('\n') retire le \n final. Pour préserver les fins de lignes POSIX des fichiers créés, le strip ne doit pas supprimer le dernier \n.

