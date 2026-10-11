"""Tests for context tiering configuration and multi-tier deepening in ContextBuilder."""

import json
from pathlib import Path
from ast_graph_server.config import ServerConfig
from ast_graph_server.context_config import ContextTieringConfig, TieringSettings
from ast_graph_server.context_builder import ContextBuilder
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer


def make_test_env(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "docs").mkdir(parents=True)
    (ws / "src" / "service.py").write_text(
        "class PaymentService:\n"
        "    def execute_payment(self, user_id: str, amount: float) -> bool:\n"
        '        """Execute a user payment."""\n'
        "        return True\n",
        encoding="utf-8",
    )
    (ws / "docs" / "guide.md").write_text(
        "# Integration Guide\n\n"
        "## Payment API\n\n"
        "Detailed payment integration instructions and protocol.\n\n"
        "## General Notes\n\n"
        "Some general notes.\n",
        encoding="utf-8",
    )
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    Indexer(config, db).index_workspace()
    return ws, config, db


def test_context_tiering_config_defaults() -> None:
    cfg = ContextTieringConfig.default()
    assert cfg.initial.enabled is True
    assert cfg.initial.min_score == 5.0
    assert cfg.initial.deepen_min_score == 10.0
    assert cfg.initial.low_tier_max_level == 1

    assert cfg.subsequent.enabled is False
    assert cfg.subsequent.min_score == 5.0


def test_context_tiering_config_load_custom(tmp_path: Path) -> None:
    conf_file = tmp_path / "custom_conf.json"
    conf_file.write_text(
        json.dumps({
            "initial": {
                "enabled": False,
                "min_score": 7.0,
                "deepen_min_score": 12.0,
                "low_tier_max_level": 2,
            },
            "subsequent": {
                "enabled": True,
                "min_score": 8.0,
                "deepen_min_score": 15.0,
                "low_tier_max_level": 1,
            }
        }),
        encoding="utf-8",
    )
    cfg = ContextTieringConfig.load(conf_file)
    assert cfg.initial.enabled is False
    assert cfg.initial.min_score == 7.0
    assert cfg.initial.deepen_min_score == 12.0
    assert cfg.initial.low_tier_max_level == 2

    assert cfg.subsequent.enabled is True
    assert cfg.subsequent.min_score == 8.0
    assert cfg.subsequent.deepen_min_score == 15.0
    assert cfg.subsequent.low_tier_max_level == 1


def test_tiering_behavior_in_context_builder(tmp_path: Path) -> None:
    ws, config, db = make_test_env(tmp_path)
    builder = ContextBuilder(config, db)

    # Custom config: initial enabled with high deepen threshold so entries stay at level 1
    custom_cfg = ContextTieringConfig(
        initial=TieringSettings(enabled=True, min_score=0.0, deepen_min_score=999.0, low_tier_max_level=1),
        subsequent=TieringSettings(enabled=False, min_score=0.0, deepen_min_score=0.0, low_tier_max_level=1),
    )

    # 1. Under initial scope (tiering enabled, deepen threshold 999)
    builder.set_scope("initial")
    res_initial = builder.build("Payment API user", budget_tokens=4000, tiering_config=custom_cfg)
    assert res_initial["tiering"]["enabled"] is True
    assert res_initial["tiering"]["scope"] == "initial"
    # All entries must be capped at level 1
    for entry in res_initial["entries"]:
        assert entry["level"] <= 1, f"Entry {entry['ref_id']} exceeded level 1 in tiering"

    # 2. Under subsequent scope (tiering disabled)
    builder.set_scope("subsequent")
    res_subsequent = builder.build("Payment API user", budget_tokens=4000, tiering_config=custom_cfg)
    assert res_subsequent["tiering"]["enabled"] is False
    assert res_subsequent["tiering"]["scope"] == "subsequent"
    # Deepening should reach level 3
    has_l3 = any(entry["level"] >= 3 for entry in res_subsequent["entries"])
    assert has_l3, "Expected at least one level 3 entry when tiering is disabled"
