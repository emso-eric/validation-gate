"""
Central policy and paths, loaded from validation-gate.yaml.

This file belongs to the central team, not to the facilities: the threshold and
the specifications version decide who gets federated. The facilities own
federation/, which says which datasets exist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import quote

import yaml

CONFIG_FILE = "validation-gate.yaml"

# The deployment's environment file, at the repository root. Deliberately not
# configurable: it is the same file docker-compose.yaml reads, and every
# address, port and password for every service lives in it. Service-specific
# settings live in <service>/.env and are none of the gate's business.
DATABASE_ENV_FILE = ".env"

# The database connection string is built from exactly these five variables of
# that file:
#
#     postgresql://POSTGRES_USER:POSTGRES_PASSWORD@POSTGRES_HOST:POSTGRES_PORT/POSTGRES_DB
#
# POSTGRES_USER, POSTGRES_PASSWORD and POSTGRES_DB are the same variables the
# postgres image itself consumes, so they cannot drift from the running server.
# POSTGRES_HOST and POSTGRES_PORT are ignored by that image and exist only to
# tell the gate where to reach it.
DATABASE_ENV_VARS = (
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
)


class ConfigError(Exception):
    """The configuration cannot be used and the gate must not guess."""


def read_env_file(path: str) -> dict[str, str]:
    """
    KEY=VALUE pairs from a .env file.

    Deliberately not a shell `source`: this file holds a password, and sourcing
    it would execute anything else it contained. Comments, blank lines and an
    `export ` prefix are tolerated; ${...} interpolation is not performed,
    because the postgres image does not perform it either.
    """
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


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
    # Nothing here: the credentials come from DATABASE_ENV_FILE, which is not
    # configurable. See DATABASE_ENV_VARS for the variables read from it.

    # --- execution -------------------------------------------------------
    workers: int = 10
    reload_method: str = "hardflag"     # hardflag | flag | none
    reload_minutes: int = 60
    keep_backups: int = 10

    repo_root: str = field(default=".", repr=False)

    def __post_init__(self) -> None:
        # Resolved on first use rather than at load time, because `check` runs
        # offline: it must work on a checkout that has no .env, which is every
        # CI checkout, since .env is gitignored.
        self._database_url: str | None = None

    # ------------------------------------------------------------------
    @property
    def database_url(self) -> str:
        """
        The PostgreSQL connection string.

        $GATE_DATABASE_URL wins, which is how docker-compose.yaml and CI pass a
        database that is not on this filesystem. Otherwise it is built from
        DATABASE_ENV_VARS in the repository-root .env.
        """
        if self._database_url is None:
            self._database_url = self._resolve_database_url()
        return self._database_url

    def _resolve_database_url(self) -> str:
        from_env = os.environ.get("GATE_DATABASE_URL")
        if from_env:
            return from_env

        path = os.path.join(self.repo_root, DATABASE_ENV_FILE)
        if not os.path.isfile(path):
            raise ConfigError(
                f"no environment file at {path}. Create it with "
                f"'cp .env.template .env', or pass the whole connection string "
                f"in $GATE_DATABASE_URL."
            )

        env = read_env_file(path)
        missing = [name for name in DATABASE_ENV_VARS if not env.get(name)]
        if missing:
            raise ConfigError(
                f"{path}: missing or empty {', '.join(missing)}. The connection "
                f"string is built from {', '.join(DATABASE_ENV_VARS)}."
            )

        # Percent-encoded: a password containing @ : / ? # or % is legal in
        # PostgreSQL and would otherwise silently corrupt the URL.
        user = quote(env["POSTGRES_USER"], safe="")
        password = quote(env["POSTGRES_PASSWORD"], safe="")
        database = quote(env["POSTGRES_DB"], safe="")
        host = env["POSTGRES_HOST"]
        port = env["POSTGRES_PORT"]
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"

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

        for gone in ("database_url", "database_env_file"):
            if gone in body:
                raise ConfigError(
                    f"{path}: {gone} is no longer read from this file, which is "
                    f"committed. The gate reads the repository-root .env - the "
                    f"same file docker-compose.yaml uses - or "
                    f"$GATE_DATABASE_URL. Delete the line."
                )

        known = {f.name for f in cls.__dataclass_fields__.values()}
        unknown = set(body) - known
        if unknown:
            raise ConfigError(
                f"{path}: unknown option(s): {', '.join(sorted(unknown))}"
            )

        cfg = cls(repo_root=repo_root, **body)

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
