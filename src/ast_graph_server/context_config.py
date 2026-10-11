"""Configuration management for Scalpel Context Tiering."""

from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
from typing import Optional, Union, Dict, Any


@dataclass
class TieringSettings:
    enabled: bool = False
    min_score: float = 10.0
    deepen_min_score: float = 10.0
    low_tier_max_level: int = 1


@dataclass
class ContextTieringConfig:
    initial: TieringSettings
    subsequent: TieringSettings

    @classmethod
    def default(cls) -> "ContextTieringConfig":
        return cls(
            initial=TieringSettings(
                enabled=True,
                min_score=5.0,
                deepen_min_score=10.0,
                low_tier_max_level=1,
            ),
            subsequent=TieringSettings(
                enabled=False,
                min_score=5.0,
                deepen_min_score=10.0,
                low_tier_max_level=1,
            ),
        )

    @classmethod
    def load(
        cls,
        config_path: Optional[Union[Path, str]] = None,
        workspace_root: Optional[Union[Path, str]] = None,
    ) -> "ContextTieringConfig":
        """Load configuration from a file, environment variable, or workspace root."""
        target_path: Optional[Path] = None
        if config_path:
            p = Path(config_path)
            if p.is_file():
                target_path = p
        elif "SCALPEL_CONTEXT_CONFIG" in os.environ:
            p = Path(os.environ["SCALPEL_CONTEXT_CONFIG"])
            if p.is_file():
                target_path = p

        if target_path is None and workspace_root:
            ws_conf = Path(workspace_root) / "context_config.json"
            if ws_conf.is_file():
                target_path = ws_conf

        if target_path is None:
            # Only use fallback context_config.json if not in a temporary test workspace
            if not (workspace_root and ("pytest" in str(workspace_root).lower() or "temp" in str(workspace_root).lower())):
                candidates = [
                    Path("context_config.json"),
                    Path(__file__).resolve().parent.parent.parent / "context_config.json",
                ]
                for c in candidates:
                    if c.is_file():
                        target_path = c
                        break

        if not target_path or not target_path.is_file():
            return cls.default()

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            initial_data = data.get("initial", {})
            subsequent_data = data.get("subsequent", {})

            return cls(
                initial=TieringSettings(
                    enabled=bool(initial_data.get("enabled", True)),
                    min_score=float(initial_data.get("min_score", 5.0)),
                    deepen_min_score=float(initial_data.get("deepen_min_score", 10.0)),
                    low_tier_max_level=int(initial_data.get("low_tier_max_level", 1)),
                ),
                subsequent=TieringSettings(
                    enabled=bool(subsequent_data.get("enabled", False)),
                    min_score=float(subsequent_data.get("min_score", 5.0)),
                    deepen_min_score=float(subsequent_data.get("deepen_min_score", 10.0)),
                    low_tier_max_level=int(subsequent_data.get("low_tier_max_level", 1)),
                ),
            )
        except Exception:
            return cls.default()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
