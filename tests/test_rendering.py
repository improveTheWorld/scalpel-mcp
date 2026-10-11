"""Unit tests for the compact text renderings of the read tools."""

from ast_graph_server.rendering import (
    render_document_matches,
    render_documents,
    render_outline,
    render_symbol_matches,
)


def test_outline_keeps_usable_section_ids() -> None:
    text = render_outline(
        "docs/g.md",
        [
            {"id": "docs/g.md#0", "heading": "Title", "order_index": 0},
            {"id": "docs/g.md#1", "heading": "Body", "order_index": 1},
        ],
    )

    assert text.splitlines()[0] == "docs/g.md (2 sections)"
    assert "docs/g.md#1 :: Body" in text


def test_outline_falls_back_to_the_order_index() -> None:
    text = render_outline("docs/g.md", [{"heading": "Only", "order_index": 3}])
    assert "docs/g.md#3 :: Only" in text


def test_symbol_matches_are_one_line_each() -> None:
    text = render_symbol_matches(
        [
            {
                "symbol_id": "src/a.py::A.m",
                "kind": "method",
                "file": "src/a.py",
                "line": 12,
                "signature": "def m(self, x: int) -> bool",
            }
        ]
    )

    assert text == "src/a.py::A.m [method] :: def m(self, x: int) -> bool"
    # the file path is derivable from symbol_id and must not be repeated
    assert text.count("src/a.py") == 1


def test_document_matches_carry_id_heading_and_body() -> None:
    text = render_document_matches(
        [{"section_id": "docs/g.md#2", "heading": "Section", "snippet": "excerpt", "document_id": "docs/g.md"}]
    )

    assert text == "docs/g.md#2 :: Section\nexcerpt"
    assert "document_id" not in text


def test_documents_listing_is_one_line_each() -> None:
    text = render_documents(
        [{"id": "docs/g.md", "file_path": "docs/g.md", "doc_type": "markdown", "title": "Guide", "section_count": 7}]
    )

    assert text == "docs/g.md :: Guide (7 sections)"
    assert text.count("docs/g.md") == 1
