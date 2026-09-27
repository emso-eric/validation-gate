"""
Central policy and paths, loaded from validation-gate.yaml.

This file belongs to the central team, not to the facilities: the threshold and
the specifications version decide who gets federated. The facilities own
federation/, which says which datasets exist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

CONFIG_FILE = "validation-gate.yaml"


class ConfigError(Exception):
    """The configuration cannot be used and the gate must not guess."""


@dataclass
class Config:
    # --- verdict ---------------------------------------------------------
    threshold: float = 90.0
    operational_required: bool = True
    keywords_required: bool = True

    # --- compliance engine ----------------------------------------------
    specs_version: str = "v1.0.7"

    # --- paths (relative to repo_root) ----------------------------------
    federation_dir: str = "federation"
    datasets_xml: str = "erddap/datasets.xml"
    erddap_data_dir: str = "erddap/data"
    templates_dir: str = "erddap/templates"
    # The engine resolves its cache by the literal name '.emso' relative to the
    # working directory, which is why this path ends in it. Keep it on
    # persistent storage: a cold cache re-downloads ~50 resources.
    cache_dir: str = "cache/.emso"

    # --- database --------------------------------------------------------
    database_url: str = ""

    # --- execution -------------------------------------------------------
    workers: int = 10
    reload_method: str = "hardflag"     # hardflag | flag | none
    reload_minutes: int = 60
    keep_backups: int = 10

    repo_root: str = field(default=".", repr=False)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str = CONFIG_FILE, repo_root: str = ".") -> "Config":
        repo_root = os.path.abspath(repo_root)
        if not os.path.isabs(path):
            path = os.path.join(repo_root, path)

        if not os.path.isfile(path):
            raise ConfigError(f"no configuration file at {path}")
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        body = raw.get("config", raw)
        if not isinstance(body, dict):
            raise ConfigError(f"{path}: expected a mapping under 'config'")

        known = {f.name for f in cls.__dataclass_fields__.values()}
        unknown = set(body) - known
        if unknown:
            raise ConfigError(
                f"{path}: unknown option(s): {', '.join(sorted(unknown))}"
            )

        cfg = cls(repo_root=repo_root, **body)

        # The container and CI pass credentials this way; never commit them.
        cfg.database_url = os.environ.get("GATE_DATABASE_URL") or cfg.database_url

        if cfg.reload_method not in ("hardflag", "flag", "none"):
            raise ConfigError(
                f"{path}: reload_method must be hardflag, flag or none, "
                f"not '{cfg.reload_method}'"
            )
        if not 0.0 <= cfg.threshold <= 100.0:
            raise ConfigError(f"{path}: threshold must be a percentage")
        return cfg

    # ------------------------------------------------------------------
    def path(self, name: str) -> str:
        """Absolute path of one of the configured directories or files."""
        value = getattr(self, name)
        return value if os.path.isabs(value) else os.path.join(self.repo_root, value)

    def describe(self) -> str:
        rules = [f"required >= {self.threshold:.1f}%"]
        if self.operational_required:
            rules.append("operational")
        if self.keywords_required:
            rules.append("keywords")
        return f"{', '.join(rules)} (specs {self.specs_version})"
