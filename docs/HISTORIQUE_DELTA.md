# Système d'Historique Delta Atomique & Time-Travel (Lots 1, 2, 3)

Ce document décrit l'architecture, le modèle relationnel, le protocole de résilience aux crashs et les algorithmes du moteur d'historique et de voyage dans le temps intégré à Scalpel.

---

## 1. Vision & Architecture Globale

Scalpel opère sur le code source via un **journal de commits append-only adressés par contenu**, déconnecté du Git hôte et fondé sur les principes transactionnels ACID (inspirés de Delta Lake et du modèle CAS de Git).

```
<workspace_root>/
  ├── .agent_graph.db          # Graphe AST et recherche (jetable / reconstructible)
  └── .scalpel/
      └── history/             # Stockage persistant du versionnage
          ├── history.db       # WAL SQLite : commits, commit_files, branches, meta
          └── blobs/           # Magasin de contenus immuables SHA-256 dédupliqués
              ├── 3a/
              │   └── 3ab4f2...# Contenu physique brut
              └── e7/
                  └── e78c90...
```

### Principes Fondamentaux :
1. **Les octets (`bytes`) sont la seule vérité** : L'historique stocke les états physiques complets avant/après mutation, jamais des listes d'opérations diff textuelles volatiles.
2. **Isolation physique** : L'historique vit dans `.scalpel/history/`, strictement séparé de l'index de graphe (`.agent_graph.db`). Réindexer ou supprimer `.agent_graph.db` ne détruit jamais l'historique de versionnage.
3. **Zéro pollution Git** : `.scalpel/` est ignoré par le Git du projet hôte.

---

## 2. Lot 1 : Journal 2PC, CAS Blobs & Crash Recovery

### Protocole 2-Phase Commit Anti-Crash
Pour éliminer tout état indécidable lors d'un crash violent (ex: coupure de courant ou arrêt de processus entre `os.replace` et l'écriture en base) :

1. **Sauvegarde des blobs CAS** : `history.save_blob(content_bytes)` écrit dans `.scalpel/history/blobs/xx/xxxx...` via fichier temporaire + `os.replace`. Idempotent et dédupliqué.
2. **Phase PENDING (SQLite)** : Enregistrement de la transaction avec `status = 'pending'`, associant les chemins relatifs et les empreintes `(before_sha, after_sha)`.
3. **Mutation Physique Disque** : Appel préventif à `suppression.suppress(path)` pour neutraliser le watcher, puis remplacement atomique `tmp + os.replace`.
4. **Phase COMMITTED (SQLite)** : Promotion de la transaction en `status = 'committed'` et avance du pointeur `HEAD`.

### Récupération au Démarrage (`recover_pending()`)
Au redémarrage du composant :
- **Cas A (Promotion)** : Si pour chaque fichier du commit `pending`, le hash sur disque correspond exactement à `after_sha`, le commit est promu en `committed`.
- **Cas B (Avortement)** : Si le disque correspond à `before_sha`, le commit est marqué `aborted`.

### Rollback du Sommet (`rollback_head()`)
- Vérifie la conformité de l'espace de travail : refuse le retour arrière si un fichier a divergé de son `after_sha` (`"modified since HEAD"`).
- Supprime physiquement les fichiers créés (`before_sha is None`) et les retire de l'index (`indexer.remove_file`).
- Restaure les fichiers modifiés depuis les blobs d'origine (`before_sha`) et les réindexe (`indexer.index_file`).
- Recule le pointeur `HEAD` vers le parent.

---

## 3. Lot 2 : Revert Non-Sommet & 3-Way Merge Inverse

La méthode `revert_commit(commit_id)` permet d'annuler un commit historique $C_k$ alors que d'autres commits lui ont succédé sur la branche active.

### Algorithme de Réconciliation :
1. **Cas Création historique** : Supprime le fichier s'il est resté inchangé depuis sa création ; refuse sinon.
2. **Cas Suppression historique** : Recrée le fichier à partir du blob `before_sha` ; refuse si un fichier du même nom existe déjà.
3. **Cas Modification historique** :
   - Si le fichier sur disque est intact ($C == B$) : restauration bit-exacte vers $A = \text{blob}(before\_sha)$.
   - Si le fichier sur disque a évolué ($C \neq B$) : exécution d'un **3-way merge inverse** fondé sur `difflib.SequenceMatcher` appliquant la variation $(B \to A)$ sur le texte courant $C$.
   - **Détection des conflits** : Tout chevauchement d'opérations non identique est qualifié de conflit direct. Le revert est **immédiatement refusé sans toucher au disque** (garantie tout-ou-rien).
4. **Traçabilité ACID** : En cas de succès, un nouveau commit avec `kind = 'revert'` est ajouté au sommet de HEAD.

---

## 4. Lot 3 : Branches Internes & Navigation Temporelle (Time Travel)

### Gestion des Branches
- **Structure** : Table `branches (name TEXT PRIMARY KEY, head_commit_id TEXT, created_at REAL NOT NULL)`.
- **Branche active** : Enregistrée dans `history_meta(key='active_branch')` (`'main'` par défaut).
- `create_branch(name)` : Crée un nouveau pointeur nommé vers le HEAD courant.
- `list_branches()` : Liste les branches avec leur tête et repère la branche active.
- `delete_branch(name)` : Supprime une branche (refuse de supprimer la branche active).

### Navigation Temporelle & Permutation de Disque
- **Reconstruction cartographique (`_get_file_map_at_commit`)** : Remonte la généalogie des parents depuis le commit cible pour reconstruire la cartographie exacte `{file_path: sha}`.
- **Garde Anti-Dérive (`_is_workspace_clean`)** : Vérifie rigoureusement que le disque ne contient aucune modification sauvage non enregistrée par rapport au HEAD courant avant toute permutation.
- **Permutation (`_apply_file_map`)** :
  - Supprime les fichiers qui n'existent pas dans l'état cible.
  - Écrit les fichiers cibles depuis les blobs CAS via `tmp + os.replace`.
  - Appelle préventivement `suppression.suppress(...)` et synchronise l'index AST (`indexer.remove_file` / `indexer.index_file`).
- `checkout_commit(commit_id)` : Positionne l'espace de travail sur n'importe quel commit historique.
- `switch_branch(name)` : Permute les fichiers physiques vers la tête de la branche demandée et bascule la branche active.

---

## 5. Synthèse des Garanties

| Capacité | Niveau de Sécurité | Comportement en cas de conflit / anomalie |
| :--- | :--- | :--- |
| **Écritures atomiques** | 2-Phase Commit (2PC) | `recover_pending` réconcilie automatiquement au redémarrage. |
| **Stockage CAS** | SHA-256 dédupliqué | Écriture idempotente, zéro duplication physique d'octets identiques. |
| **Rollback HEAD** | Bit-exact | Refus si le fichier a été altéré depuis le commit HEAD. |
| **Revert Historique** | 3-way merge inverse | Refus strict tout-ou-rien si conflit direct de lignes. Disque intact. |
| **Branches & Time Travel** | Permutation synchronisée | Refus de bascule si l'espace de travail est « dirty ». |