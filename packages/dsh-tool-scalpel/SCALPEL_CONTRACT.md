# Contrat Scalpel : ce qui ne vaut que dans DSH

Le flux de travail ci-dessus est celui du banc de mesure, repris mot pour mot. Ce contrat n'ajoute que ce qui est propre à DSH.

## 1. Le shell ne lit ni n'édite le code source

Le terminal (`pwsh` / `bash`) sert aux builds, aux tests, à Git et aux scripts d'environnement. Lire ou éditer un fichier source par le shell (`Get-Content`, `Set-Content`, `cat`, `sed`…) est interdit.

## 2. Limites connues de Scalpel aujourd'hui

- Le contenu racine hors symbole d'un fichier indexé (appels `test(...)`, instructions, fonctions imbriquées) n'est ni lisible ni modifiable par bloc.
- Un fichier YAML rend un plan sans bloc : ni `read_block` ni les outils de repli ne donnent son texte.
- `insert_block` ne remplace jamais un fichier existant ; `delete_block` ne supprime ni fichier ni dossier.

Face à l'une d'elles : signalez-la dans votre rapport (fichier, opération, message exact), sans contourner par le shell.

## 3. Fichiers sans grammaire (blocs uniques)

Les fichiers que Scalpel ne découpe pas en arbre AST (.txt, .ps1, scripts, configs, YAML...) sont traités directement comme un bloc unique par les outils Scalpel standard :
- `read_block` renvoie son texte numéroté et son `content_hash`.
- `edit_block` modifie ses lignes avec `expected_hash` (les mêmes opérations `replace`, `delete`, `insert_after` s'appliquent).
- `delete_block` supprime le fichier entier (avec `expected_heading` égal au nom du fichier comme garde-fou).
- `insert_block` crée un nouveau fichier (parent = dossier existant, text = nom du fichier, content = contenu initial).

## 4. Bilan & Retour d'expérience (Scalpel & AI Agentic OS)

En fin de tâche, sur demande de bilan, ou dès qu'une anomalie est rencontrée, utilise l'outil `scalpel_report` pour consigner un retour d'expérience complet :
- **Bugs, erreurs et blocages** (`blockages_and_refusals`) avec les éléments exacts de reproduction (`reproduction_data`),
- **Avis sur l'expérience d'utilisation de l'AI Agentic OS** (`ai_os_experience` / `summary` / `successes` : capsules d'experts, `expert_wakeup`, `expert_push_forward`, `expert_ask`, `expert_ticket`, `expert_board`, `expert_mitosis`, compaction, sous-agents, ergonomie du harnais),
- **Souhaits d'amélioration et propositions d'évolution** pour Scalpel et l'AI Agentic OS (`recommendations` / `improvement_wishes`).


