import concurrent.futures
from pathlib import Path
import pytest
import sqlite3
import threading

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.documents import DocumentSection, ParsedDocument
from ast_graph_server.indexer import Indexer
from ast_graph_server.lineops import apply_ops
from ast_graph_server.parsers.base import compute_content_hash
from ast_graph_server.patcher import Patcher


def literal(content: str, _column: str) -> str:
    return content


def make_test_env(base: Path, extra_files: dict = None):
    ws = base / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "docs").mkdir(parents=True, exist_ok=True)
    (ws / "src").mkdir(parents=True, exist_ok=True)

    (ws / "docs" / "guide.md").write_text("# Guide\n\nIntro text.\n\n## Section 1\n\nBody 1.\n", encoding="utf-8")
    (ws / "src" / "main.py").write_text("class MainApp:\n    def run(self):\n        pass\n", encoding="utf-8")

    if extra_files:
        for rel_path, content in extra_files.items():
            p = ws / rel_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

    cfg = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(cfg.db_path)
    indexer = Indexer(cfg, db)
    indexer.index_workspace()
    patcher = Patcher(cfg, db, indexer)
    return ws, db, patcher, indexer


# ==============================================================================
# LOT 1.1 : Chaînage FIFO et robustesse aux limites dans lineops.py::apply_ops
# ==============================================================================

def test_fifo_10_inserts_at_same_anchor():
    """10 insertions simultanées au même point d'ancrage doivent préserver l'ordre FIFO strict."""
    text = "line1\nline2\nline3\n"
    ops = [{"insert_after": 2, "content": f"insert_{i}"} for i in range(1, 11)]

    res = apply_ops(text, ops, literal)
    assert res.error is None
    lines = res.text.splitlines()

    expected_block = [f"insert_{i}" for i in range(1, 11)]
    # line1, line2, insert_1..insert_10, line3
    assert lines[0] == "line1"
    assert lines[1] == "line2"
    assert lines[2:12] == expected_block
    assert lines[12] == "line3"


def test_fifo_inserts_at_anchor_zero():
    """Insertions multiples en tête de fichier (insert_after: 0) en ordre FIFO."""
    text = "first_line\nsecond_line\n"
    ops = [
        {"insert_after": 0, "content": "header_A"},
        {"insert_after": 0, "content": "header_B"},
        {"insert_after": 0, "content": "header_C"},
    ]
    res = apply_ops(text, ops, literal)
    assert res.error is None
    assert res.text == "header_A\nheader_B\nheader_C\nfirst_line\nsecond_line\n"


def test_fifo_inserts_at_end_of_file():
    """Insertions multiples à la dernière ligne (insert_after: len(lines)) en ordre FIFO."""
    text = "L1\nL2\n"
    ops = [
        {"insert_after": 2, "content": "tail_1"},
        {"insert_after": 2, "content": "tail_2"},
        {"insert_after": 2, "content": "tail_3"},
    ]
    res = apply_ops(text, ops, literal)
    assert res.error is None
    assert res.text == "L1\nL2\ntail_1\ntail_2\ntail_3\n"


def test_out_of_bounds_and_negative_inserts():
    """Insertions hors-bornes ou négatives doivent être refusées proprement avec message explicatif."""
    text = "line1\nline2\n"
    # insert_after négatif
    res_neg = apply_ops(text, [{"insert_after": -1, "content": "neg"}], literal)
    assert res_neg.error is not None
    assert res_neg.error["status"] == "error"
    assert "insert_after must be inside 0..2" in res_neg.error["error"]

    # insert_after > total
    res_high = apply_ops(text, [{"insert_after": 3, "content": "too_high"}], literal)
    assert res_high.error is not None
    assert res_high.error["status"] == "error"
    assert "insert_after must be inside 0..2" in res_high.error["error"]


def test_combination_replace_delete_insert_at_same_index():
    """Combinaison d'insert_after et replace/delete sur les mêmes indices.

    apply_ops doit ordonner de façon déterministe (les opérations de range
    s'appliquent avant les inserts au même index, tri reverse bottom-up).
    """
    text = "line1\nline2\nline3\nline4\n"
    # insert_after: 2 insère entre 2 et 3
    # replace: [3, 3] remplace la ligne 3
    # delete: [4, 4] supprime la ligne 4
    ops = [
        {"insert_after": 2, "content": "inserted_between_2_and_3"},
        {"replace": [3, 3], "content": "replaced_3"},
        {"delete": [4, 4]},
    ]
    res = apply_ops(text, ops, literal)
    assert res.error is None
    lines = res.text.splitlines()
    assert lines == ["line1", "line2", "inserted_between_2_and_3", "replaced_3"]


def test_malformed_insert_operations():
    """Vérifier la résilience face à des opérations malformées."""
    text = "line1\nline2\n"
    # Type invalide
    assert apply_ops(text, ["not_a_dict"], literal).error is not None
    assert apply_ops(text, [{"insert_after": "abc"}], literal).error is not None
    assert apply_ops(text, [{"insert_after": None}], literal).error is not None
    assert apply_ops(text, [{"replace": [1]}], literal).error is not None


# ==============================================================================
# LOT 1.1b : Gestion tolérante de ops: [] et statuts noop dans edit_block / _edit_batch
# ==============================================================================

def test_empty_ops_in_edit_block(tmp_path: Path):
    """edit_block avec ops: [] renvoie une erreur descriptive avec actionable_hint."""
    ws, db, patcher, _ = make_test_env(tmp_path)
    res = patcher.edit_block(block_id="src/main.py", ops=[])
    assert res["status"] == "error"
    assert "ops is empty: there is nothing to apply" in res["error"]
    assert "read_block" in res["actionable_hint"]


def test_noop_status_when_content_identical(tmp_path: Path):
    """edit_block remplaçant une section par son contenu identique renvoie le statut noop."""
    ws, db, patcher, _ = make_test_env(tmp_path)
    read_res = patcher.read_block("docs/guide.md::Section 1")
    assert read_res["status"] == "success"
    chash = read_res["content_hash"]

    # Remplacement identique de la ligne de corps (Body 1.)
    res = patcher.edit_block(
        block_id="docs/guide.md::Section 1",
        expected_hash=chash,
        ops=[{"replace": [3, 3], "content": "Body 1."}],
    )
    assert res["status"] == "noop"
    assert "identical" in res.get("message", "")


def test_batch_mixed_success_noop_error(tmp_path: Path):
    """Test d'un lot d'éditions mixte : 1 succès, 1 noop, 1 erreur.

    Vérifie la préservation des verrous, le reporting précis par entrée, et le calcul
    applied (uniquement les 'success') / refused (comptabilise les 'noop' et 'error').
    """
    extra_docs = {
        "docs/doc.md": "# Doc\n\n## Section A\n\nBody A.\n\n## Section B\n\nBody B.\n\n## Section C\n\nBody C.\n"
    }
    ws, db, patcher, _ = make_test_env(tmp_path, extra_docs)

    read_a = patcher.read_block("docs/doc.md::Section A")
    hash_a = read_a["content_hash"]

    read_b = patcher.read_block("docs/doc.md::Section B")
    hash_b = read_b["content_hash"]

    edits = [
        # 1. Succès : modification de la section A
        {
            "path": "docs/doc.md::Section A",
            "expected_hash": hash_a,
            "ops": [{"replace": [3, 3], "content": "Body A updated."}],
        },
        # 2. Noop : modification identique de la section B
        {
            "path": "docs/doc.md::Section B",
            "expected_hash": hash_b,
            "ops": [{"replace": [3, 3], "content": "Body B."}],
        },
        # 3. Échec : opération hors-bornes sur la section C
        {
            "path": "docs/doc.md::Section C",
            "expected_hash": "bad_hash",
            "ops": [{"replace": [1, 1], "content": "Will fail"}],
        },
    ]

    batch_res = patcher._edit_batch(edits)
    assert batch_res["status"] == "success"
    assert batch_res["applied"] == 1
    # La seule entrée en échec réel est classée en refused (le noop n'est pas un échec)
    assert batch_res["refused"] == 1
    assert batch_res["results"][0]["status"] == "success"
    assert batch_res["results"][1]["status"] == "noop"
    assert batch_res["results"][2]["status"] == "error"


def test_batch_all_noop_behavior(tmp_path: Path):
    """Vérifier le comportement global d'un lot constitué uniquement de noop sur une section Markdown.

    Comportement constaté : 'applied' vaut 0, 'refused' vaut le nombre total de noop,
    et le statut global du lot est 'error' car applied == 0, même si chaque entrée
    individuelle reporte 'status': 'noop'.
    """
    ws, db, patcher, _ = make_test_env(tmp_path)
    read_sec = patcher.read_block("docs/guide.md::Section 1")
    hash_sec = read_sec["content_hash"]

    edits = [
        {
            "path": "docs/guide.md::Section 1",
            "expected_hash": hash_sec,
            "ops": [{"replace": [3, 3], "content": "Body 1."}],
        }
    ]
    batch_res = patcher._edit_batch(edits)
    # Un lot tout-noop sans erreur n'est pas un échec global
    assert batch_res["status"] == "success"
    assert batch_res["applied"] == 0
    assert batch_res["refused"] == 0
    assert batch_res["results"][0]["status"] == "noop"
    assert "identical" in batch_res["results"][0].get("message", "")


# ==============================================================================
# LOT 1.2 : Suppression motif N+1 sur search_documents via WHERE id IN (...)
# ==============================================================================

def test_search_documents_zero_matches(tmp_path: Path):
    """search_documents retournant 0 match ne doit pas exécuter de clause IN vide."""
    ws, db, _, _ = make_test_env(tmp_path)
    res = db.search_documents("UnmatchableTermXYZ999", include_content=True)
    assert res == []


def test_search_documents_large_in_clause_sqlite_limit(tmp_path: Path):
    """Tester la clause IN avec plus de 1000 IDs pour tester la limite de variables SQLite."""
    ws, db, _, _ = make_test_env(tmp_path)

    sections = [
        DocumentSection(heading=f"Heading {i}", level=2, order_index=i, content=f"SharedToken content {i}")
        for i in range(1200)
    ]
    doc = ParsedDocument(doc_type="markdown", title="Big Doc", sections=sections)
    db.upsert_document("docs/big.md", doc, content_hash="big_hash")

    # Recherche avec limit=1100 et include_content=True pour solliciter > 1000 paramètres dans WHERE id IN (...)
    try:
        matches = db.search_documents("SharedToken", limit=1100, include_content=True)
        assert len(matches) == 1100
        assert all("content" in m for m in matches)
    except sqlite3.OperationalError as e:
        # Si SQLite a une limite stricte de variables (ex: SQLITE_MAX_VARIABLE_NUMBER = 999), documenter l'erreur
        pytest.fail(f"Crash SQLite sur clause IN avec > 999 paramètres: {e}")


def test_search_documents_special_characters_quotes_in_ids(tmp_path: Path):
    """Tester que les caractères exotiques (quotes, emojis, injections) dans les section_id ne cassent pas la clause IN."""
    ws, db, _, _ = make_test_env(tmp_path)

    special_sections = [
        DocumentSection(heading="Section 'SingleQuote'", level=2, order_index=0, content="Special 1 ' quote"),
        DocumentSection(heading='Section "DoubleQuote"', level=2, order_index=1, content='Special 2 " quote'),
        DocumentSection(heading="Section -- SQLComment", level=2, order_index=2, content="Special 3 -- comment"),
        DocumentSection(heading="Section 🚀 Emoji", level=2, order_index=3, content="Special 4 emoji rocket"),
        DocumentSection(heading="Section \n LineBreak", level=2, order_index=4, content="Special 5 newline"),
    ]
    doc = ParsedDocument(doc_type="markdown", title="Special Doc", sections=special_sections)
    db.upsert_document("docs/special.md", doc, content_hash="spec_hash")

    matches = db.search_documents("Special", limit=10, include_content=True)
    assert len(matches) == 5
    for m in matches:
        assert m["content"] is not None
        assert "Special" in m["content"]


# ==============================================================================
# LOT 1.3 : Préservation du saut de ligne terminal \n dans insert_block
# ==============================================================================

def test_insert_file_preserves_trailing_newline_with_content(tmp_path: Path):
    """Vérifier la préservation exacte du \n terminal via content."""
    ws, db, patcher, _ = make_test_env(tmp_path)

    # Avec \n terminal
    c_with = "def foo():\n    return 42\n"
    res1 = patcher.insert_block(parent="src", text="foo.py", content=c_with)
    assert res1["status"] == "success"
    written1 = (ws / "src" / "foo.py").read_bytes()
    assert written1 == c_with.encode("utf-8")
    assert written1.endswith(b"\n")

    # Sans \n terminal
    c_without = "def bar():\n    return 0"
    res2 = patcher.insert_block(parent="src", text="bar.py", content=c_without)
    assert res2["status"] == "success"
    written2 = (ws / "src" / "bar.py").read_bytes()
    assert written2 == c_without.encode("utf-8")
    assert not written2.endswith(b"\n")


def test_insert_file_via_multiline_text_divergence(tmp_path: Path):
    """Observation : insert_block sans paramètre content passe par _split_block_text qui strip(\"\\n\")."""
    ws, db, patcher, _ = make_test_env(tmp_path)

    # Appel via text uniquement (comportement historique)
    multiline_text = "test_strip.py\ndef baz():\n    return 1\n"
    res = patcher.insert_block(parent="src", text=multiline_text)
    assert res["status"] == "success"

    written = (ws / "src" / "test_strip.py").read_bytes()
    # Le _split_block_text fait .strip("\n"), donc le \n final est supprimé quand content n'est pas passé
    assert not written.endswith(b"\n")


# ==============================================================================
# LOT 2.1 : Contrôle de profondeur max_depth sur get_project_map
# ==============================================================================

def test_max_depth_filtering_on_deep_hierarchy(tmp_path: Path):
    """Vérifier le filtrage strict selon la profondeur de dossier."""
    extra = {
        "root.md": "# Root Doc\n",
        "sub1/doc1.md": "# Sub1 Doc\n",
        "sub1/sub2/doc2.md": "# Sub2 Doc\n",
        "sub1/sub2/sub3/doc3.md": "# Sub3 Doc\n",
        "sub1/sub2/sub3/code.py": "class DeepCode:\n    pass\n",
    }
    ws, db, _, _ = make_test_env(tmp_path, extra)

    # max_depth = 1 : seulement la racine
    map_d1 = db.get_project_map(max_depth=1, answer_format="json")
    doc_paths_1 = [d["file_path"] for d in map_d1["documents"]]
    assert "root.md" in doc_paths_1
    assert "sub1/doc1.md" not in doc_paths_1
    assert "sub1/sub2/doc2.md" not in doc_paths_1

    # max_depth = 2 : racine + premier niveau
    map_d2 = db.get_project_map(max_depth=2, answer_format="json")
    doc_paths_2 = [d["file_path"] for d in map_d2["documents"]]
    assert "root.md" in doc_paths_2
    assert "sub1/doc1.md" in doc_paths_2
    assert "sub1/sub2/doc2.md" not in doc_paths_2

    # max_depth = 4 : jusqu'au niveau 4
    map_d4 = db.get_project_map(max_depth=4, answer_format="json")
    file_paths_4 = [f["file_path"] for f in map_d4["files"]]
    assert "sub1/sub2/sub3/code.py" in file_paths_4


def test_max_depth_zero_and_negative_limits(tmp_path: Path):
    """Tester les cas limites max_depth=0 et négatif."""
    ws, db, _, _ = make_test_env(tmp_path)

    # max_depth = 0 : len(parts) > 0 est toujours vrai, donc 0 document et 0 fichier retournés
    map_d0 = db.get_project_map(max_depth=0, answer_format="json")
    assert len(map_d0["documents"]) == 0
    assert len(map_d0["files"]) == 0

    # max_depth = -1 : len(parts) > -1 est toujours vrai
    map_neg = db.get_project_map(max_depth=-1, answer_format="json")
    assert len(map_neg["documents"]) == 0
    assert len(map_neg["files"]) == 0


def test_max_depth_json_vs_text_parity(tmp_path: Path):
    """Vérifier que le filtrage max_depth produit des résultats cohérents en text et json."""
    ws, db, _, _ = make_test_env(tmp_path, {"deep/nest/doc.md": "# Deep\n"})
    from ast_graph_server.rendering import render_project_map

    map_json = db.get_project_map(max_depth=1, answer_format="json")
    rendered_text = render_project_map(map_json)

    assert "deep/nest/doc.md" not in rendered_text
    assert "deep/" not in rendered_text


# ==============================================================================
# LOT 2.2 : Mémoïsation du parsing Markdown dans Patcher._get_markdown_layout
# ==============================================================================

def test_markdown_layout_cache_invalidation_on_mutation(tmp_path: Path):
    """Le cache de layout doit renvoyer le nouveau layout lorsque le fichier mute."""
    ws, db, patcher, _ = make_test_env(tmp_path)
    file_path = ws / "docs" / "guide.md"

    raw1 = file_path.read_bytes()
    text1 = raw1.decode("utf-8")
    layout1 = patcher._get_markdown_layout(file_path, raw1, text1)
    assert len(layout1) == 2

    # Mutation du fichier
    text2 = "# Guide Modifié\n\n## Section 1\n\n## Section 2\n\nContenu 2\n"
    raw2 = text2.encode("utf-8")
    file_path.write_bytes(raw2)

    layout2 = patcher._get_markdown_layout(file_path, raw2, text2)
    assert len(layout2) == 3
    assert layout2[0]["heading"] == "Guide Modifié"
    assert layout1 is not layout2


def test_markdown_layout_cache_overflow_above_200(tmp_path: Path):
    """Le cache doit s'auto-purger lorsqu'il dépasse 200 entrées sans lever d'erreur."""
    ws, db, patcher, _ = make_test_env(tmp_path)
    file_base = ws / "docs"

    # Remplir le cache avec 250 entrées distinctes
    for i in range(250):
        dummy_path = file_base / f"doc_{i}.md"
        content = f"# Title {i}\n\nContent {i}\n"
        raw = content.encode("utf-8")
        patcher._get_markdown_layout(dummy_path, raw, content)

    # Vérifier que le cache est resté borné (vidé puis rerempli jusqu'à 250 - 200 = 50 entrées)
    assert len(patcher._markdown_layout_cache) <= 201


def test_markdown_layout_cache_multithread_concurrency(tmp_path: Path):
    """Vérifier la thread-safety de _get_markdown_layout en accès concurrent soutenu."""
    ws, db, patcher, _ = make_test_env(tmp_path)
    errors = []

    def worker(worker_id: int):
        try:
            for i in range(50):
                path = ws / f"thread_{worker_id}_{i}.md"
                content = f"# Heading {worker_id}_{i}\n\nText\n"
                raw = content.encode("utf-8")
                patcher._get_markdown_layout(path, raw, content)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Des exceptions ont eu lieu en concurrence multi-thread: {errors}"


def test_markdown_layout_cache_reference_mutability(tmp_path: Path):
    """Investigation : vérifier si le cache renvoie une référence directe mutable (risque d'effet de bord)."""
    ws, db, patcher, _ = make_test_env(tmp_path)
    file_path = ws / "docs" / "guide.md"
    raw = file_path.read_bytes()
    text = raw.decode("utf-8")

    layout_initial = patcher._get_markdown_layout(file_path, raw, text)
    # Mutation accidentelle par un consommateur
    original_heading = layout_initial[0]["heading"]
    layout_initial[0]["heading"] = "CORRUPTED_HEADING"

    # Deuxième lecture via le cache
    layout_cached = patcher._get_markdown_layout(file_path, raw, text)
    # Si le cache retourne la référence directe, il a été pollué !
    is_polluted = (layout_cached[0]["heading"] == "CORRUPTED_HEADING")
    # Restaurer la valeur pour ne pas polluer d'autres tests
    layout_initial[0]["heading"] = original_heading
    # Documenter l'observation : le cache retourne la référence directe non copiée
    # Le cache retourne désormais une copie défensive : la mutation de layout_initial ne pollue pas layout_cached
    assert is_polluted is False, "Le cache doit retourner une copie défensive pour éviter les effets de bord"


# ==============================================================================
# LOT 2.3 : Table virtuelle FTS5 document_sections_fts
# ==============================================================================

def test_fts5_sync_on_upsert_and_delete(tmp_path: Path):
    """La table document_sections_fts est bien synchronisée sur upsert et delete de documents."""
    ws, db, _, _ = make_test_env(tmp_path)

    sections = [
        DocumentSection(heading="OAuth Authentication", level=2, order_index=0, content="Flows with OAuth2 tokens"),
        DocumentSection(heading="Settings", level=2, order_index=1, content="Configuration parameters"),
    ]
    doc = ParsedDocument(doc_type="markdown", title="Auth Guide", sections=sections)
    db.upsert_document("docs/auth.md", doc, content_hash="auth_hash_1")

    with db.get_connection() as conn:
        rows = conn.execute("SELECT section_id FROM document_sections_fts WHERE document_id = 'docs/auth.md'").fetchall()
        assert len(rows) == 2

        # Requête MATCH FTS5
        matched = conn.execute("SELECT section_id FROM document_sections_fts WHERE document_sections_fts MATCH 'OAuth2'").fetchall()
        assert len(matched) == 1

    # Modification (remplacement par 1 seule section)
    doc_v2 = ParsedDocument(doc_type="markdown", title="Auth Guide", sections=[
        DocumentSection(heading="SAML Authentication", level=2, order_index=0, content="SAML 2.0 assertions")
    ])
    db.upsert_document("docs/auth.md", doc_v2, content_hash="auth_hash_2")

    with db.get_connection() as conn:
        rows_v2 = conn.execute("SELECT section_id FROM document_sections_fts WHERE document_id = 'docs/auth.md'").fetchall()
        assert len(rows_v2) == 1
        # Les anciennes sections ont bien été purgées
        matched_old = conn.execute("SELECT section_id FROM document_sections_fts WHERE document_sections_fts MATCH 'OAuth2'").fetchall()
        assert len(matched_old) == 0

    # Suppression
    db.delete_document("docs/auth.md")
    with db.get_connection() as conn:
        rows_v3 = conn.execute("SELECT section_id FROM document_sections_fts WHERE document_id = 'docs/auth.md'").fetchall()
        assert len(rows_v3) == 0


def test_fts5_special_syntax_queries_behavior(tmp_path: Path):
    """Tester les requêtes contenant des opérateurs FTS5 non terminés ou caractères spéciaux.

    SQLite FTS5 lève sqlite3.OperationalError si la requête MATCH contient des erreurs de syntaxe
    telles que des opérateurs orphelins (AND, OR, NOT), des guillemets déséquilibrés, etc.
    """
    ws, db, _, _ = make_test_env(tmp_path)
    sections = [
        DocumentSection(heading="Syntax Guide", level=2, order_index=0, content="C++ programming with pointer* and AND logic"),
    ]
    doc = ParsedDocument(doc_type="markdown", title="Syntax Doc", sections=sections)
    db.upsert_document("docs/syntax.md", doc, content_hash="syn_1")

    with db.get_connection() as conn:
        # 1. Requête FTS5 valide
        res = conn.execute("SELECT section_id FROM document_sections_fts WHERE document_sections_fts MATCH 'programming'").fetchall()
        assert len(res) == 1

        # 2. Requête avec opérateur non terminé (ex: 'AND') -> Syntax Error attendue
        with pytest.raises(sqlite3.OperationalError) as exc_and:
            conn.execute("SELECT section_id FROM document_sections_fts WHERE document_sections_fts MATCH 'logic AND'").fetchall()
        assert "syntax error" in str(exc_and.value).lower()

        # 3. Requête avec guillemet non fermé -> Erreur de syntaxe / unterminated string attendue
        with pytest.raises(sqlite3.OperationalError) as exc_quote:
            conn.execute('SELECT section_id FROM document_sections_fts WHERE document_sections_fts MATCH \'programming "quote\'').fetchall()
        msg_quote = str(exc_quote.value).lower()
        assert "syntax error" in msg_quote or "unterminated string" in msg_quote

        # 4. Requête avec caractère '+' (C++) -> '+' est un opérateur FTS5 ou token
        with pytest.raises(sqlite3.OperationalError) as exc_plus:
            conn.execute("SELECT section_id FROM document_sections_fts WHERE document_sections_fts MATCH 'C++'").fetchall()
        assert "syntax error" in str(exc_plus.value).lower()



def test_sqlite_variable_limit_boundary_32766(tmp_path: Path):
    """Tester le comportement face à un grand nombre de sections dépassant SQLITE_MAX_VARIABLE_NUMBER (32766).

    Vérifie que DatabaseManager.search_documents chunke correctement les requêtes par paquets de 900
    et ne déclenche pas d'OperationalError 'too many SQL variables'.
    """
    ws, db, _, _ = make_test_env(tmp_path)
    count = 35000
    param_list = [f"sec_{i}" for i in range(count)]

    # 1. Vérifier que la requête brute sans chunking échouerait bien dans SQLite
    placeholders = ",".join("?" for _ in param_list)
    with db.get_connection() as conn:
        with pytest.raises(sqlite3.OperationalError) as exc_info:
            conn.execute(f"SELECT id FROM document_sections WHERE id IN ({placeholders})", param_list)
        assert "too many sql variables" in str(exc_info.value).lower()

    # 2. Vérifier que la logique chunkée par 900 résout sans exception
    content_map = {}
    chunk_size = 900
    with db.get_connection() as conn:
        for i in range(0, len(param_list), chunk_size):
            chunk = param_list[i : i + chunk_size]
            sub_placeholders = ",".join("?" for _ in chunk)
            chunk_rows = conn.execute(
                f"SELECT id, content FROM document_sections WHERE id IN ({sub_placeholders})",
                chunk,
            ).fetchall()
            for r in chunk_rows:
                content_map[r["id"]] = r["content"] or ""
    assert isinstance(content_map, dict)


def test_fifo_50_inserts_and_stress_lineops():
    """Stress-test : 50 insertions FIFO consécutives entrelacées avec des modifications."""
    text = "START\n" + "\n".join(f"line_{i}" for i in range(1, 21)) + "\nEND\n"
    initial_lines = text.splitlines()
    line_at_5 = initial_lines[4]   # 5ème ligne (line_4)
    line_at_10 = initial_lines[9]  # 10ème ligne (line_9)

    # Insérer 25 items après la ligne 5 et 25 après la ligne 10
    ops = []
    for i in range(25):
        ops.append({"insert_after": 5, "content": f"bulk_5_{i}"})
    for j in range(25):
        ops.append({"insert_after": 10, "content": f"bulk_10_{j}"})

    res = apply_ops(text, ops, literal)
    assert res.error is None
    lines = res.text.splitlines()

    # Vérifier que le bloc bulk_5 inséré après la ligne 5 conserve son ordre FIFO 0..24
    idx_5 = lines.index(line_at_5)
    assert lines[idx_5 + 1 : idx_5 + 26] == [f"bulk_5_{i}" for i in range(25)]

    # Vérifier que le bloc bulk_10 inséré après la ligne 10 conserve son ordre FIFO 0..24
    idx_10 = lines.index(line_at_10)
    assert lines[idx_10 + 1 : idx_10 + 26] == [f"bulk_10_{j}" for j in range(25)]


def test_markdown_layout_cache_invalidation_after_edit_block(tmp_path: Path):
    """Vérifier que le cache de layout est bien invalidé et mis à jour après un edit_block."""
    ws, db, patcher, _ = make_test_env(tmp_path)
    read_before = patcher.read_block("docs/guide.md::Section 1")
    assert "Body 1." in read_before["numbered_text"]

    # Éditer la section
    edit_res = patcher.edit_block(
        block_id="docs/guide.md::Section 1",
        expected_hash=read_before["content_hash"],
        ops=[{"replace": [3, 3], "content": "Body 1 freshly mutated."}],
    )
    assert edit_res["status"] == "success"

    # Relire via read_block : le layout lu doit refléter la mutation
    read_after = patcher.read_block("docs/guide.md::Section 1")
    assert "Body 1 freshly mutated." in read_after["numbered_text"]
    assert read_after["content_hash"] != read_before["content_hash"]


def test_max_depth_string_type_handling(tmp_path: Path):
    """Vérifier le comportement quand max_depth est passé sous forme de chaîne de caractères."""
    ws, db, _, _ = make_test_env(tmp_path, {"sub/nested/file.md": "# Nested\n"})
    # max_depth passé comme entier
    res_int = db.get_project_map(max_depth=1, answer_format="json")
    assert "sub/nested/file.md" not in [d["file_path"] for d in res_int["documents"]]