"""
The federation registry: federation/<Facility>/<Service>.yaml -> Dataset list.

A bare datasetID uses the defaults - tabledap, and the URL derived as
<service url>/<type>/<datasetID>. A body is only needed to override one of
those or to set a defaultQuery.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import yaml

logger = logging.getLogger("gate")

PROTOCOLS = {
    "tabledap": "EDDTableFromErddap",
    "griddap": "EDDGridFromErddap",
}

# A datasetID ERDDAP rejects is a silent hole in the federation: the whole
# datasets.xml is refused, so it is worth catching here rather than there.
DATASET_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.\-]*$")

# Two ways a dataset can have no verdict yet: absent from the database entirely,
# or mirrored by sync_federation but never validated, which v_dataset_current
# reports as 'pending'. Both mean `run new` has work to do.
NEVER_VALIDATED = frozenset({"unknown", "pending"})


class RegistryError(Exception):
    """The registry is malformed. Never guess what a facility meant."""


@dataclass
class Dataset:
    """One declared dataset, everything the gate needs about it in one place."""

    id: str
    host: str                       # e.g. https://data.obsea.es/erddap
    facility: str                   # the federation/ directory name
    service: str                    # the registry file stem
    registry_path: str
    protocol: str = "tabledap"
    default_query: str = ""
    url: str = ""                   # derived, or overridden in the YAML

    # Latest status from the database, used to pick what a run looks at.
    db_status: str = "unknown"

    def __post_init__(self) -> None:
        if not self.url:
            self.url = f"{self.host}/{self.protocol}/{self.id}"

    @property
    def erddap_type(self) -> str:
        return PROTOCOLS[self.protocol]

    @property
    def info_url(self) -> str:
        """
        The dataset URL in the form the compliance engine's parser accepts.

        It understands /erddap/tabledap/<id> and /erddap/info/<id>/index.html
        but not /erddap/griddap/<id>, so everything is normalised to the info
        form - the only one that works for both protocols.
        """
        url = self.url.split("?")[0].rstrip("/")
        for protocol in ("/tabledap/", "/griddap/"):
            if protocol in url:
                return f"{url.rsplit(protocol, 1)[0]}/info/{self.id}/index.html"
        return url

    @property
    def server(self) -> str:
        """Hostname, used to spread concurrent work across servers."""
        return urlparse(self.url).hostname or self.host

    def __str__(self) -> str:
        return f"{self.facility}/{self.id}"


@dataclass
class Federation:
    """Every dataset git declares, read from the YAML registry."""

    federation_dir: str
    database: object | None = None
    datasets: list[Dataset] = field(default_factory=list)

    # ------------------------------------------------------------------
    def build_federation(self) -> list[Dataset]:
        """Scan the registry and construct every dataset. Raises on any error."""
        if not os.path.isdir(self.federation_dir):
            raise RegistryError(f"no registry directory at {self.federation_dir}")

        datasets: list[Dataset] = []
        seen: dict[str, str] = {}

        for path in self._registry_files():
            facility = os.path.basename(os.path.dirname(path))
            service = os.path.splitext(os.path.basename(path))[0]
            for dataset in self._read_file(path, facility, service):
                if dataset.id in seen:
                    raise RegistryError(
                        f"{dataset.registry_path}: datasetID '{dataset.id}' is "
                        f"already declared in {seen[dataset.id]}. ERDDAP keys "
                        f"the whole federation on it, so it must be unique."
                    )
                seen[dataset.id] = dataset.registry_path
                datasets.append(dataset)

        self.datasets = datasets
        return datasets

    def get_datasets(self, how: str = "all") -> list[Dataset]:
        """
        The datasets a run should look at.

            all      every declared dataset
            new      never validated before
            pending  everything not currently healthy, new ones included

        Both incremental modes need the database; `all` does not.
        """
        if how not in ("all", "new", "pending"):
            raise ValueError(f"unknown selection '{how}'")

        if not self.datasets:
            self.build_federation()

        if how == "all":
            return list(self.datasets)

        if self.database is None:
            raise RegistryError(f"'{how}' needs a database connection")

        statuses = self.database.get_status()
        for dataset in self.datasets:
            dataset.db_status = statuses.get(dataset.id, "unknown")

        if how == "new":
            return [d for d in self.datasets if d.db_status in NEVER_VALIDATED]
        return [d for d in self.datasets if d.db_status != "healthy"]

    # ------------------------------------------------------------------
    def _registry_files(self) -> list[str]:
        found = []
        for root, _dirs, files in os.walk(self.federation_dir):
            for name in sorted(files):
                if name.endswith((".yaml", ".yml")):
                    found.append(os.path.join(root, name))
        return sorted(found)

    def _read_file(self, path: str, facility: str, service: str) -> list[Dataset]:
        rel = os.path.relpath(path, os.path.dirname(self.federation_dir))
        try:
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise RegistryError(f"{rel}: malformed YAML: {exc}") from exc

        body = raw.get("service")
        if not isinstance(body, dict):
            raise RegistryError(f"{rel}: expected a 'service' mapping")

        host = str(body.get("url") or "").strip().rstrip("/")
        if not host:
            raise RegistryError(f"{rel}: service has no url")
        # The registry is allowed to omit it; every ERDDAP lives under /erddap.
        if not host.endswith("/erddap"):
            host = f"{host}/erddap"

        entries = body.get("datasets")
        if not isinstance(entries, dict) or not entries:
            raise RegistryError(f"{rel}: service has no datasets")

        datasets = []
        for dataset_id, override in entries.items():
            dataset_id = str(dataset_id)
            self._check_id(dataset_id, rel)
            override = override or {}
            if not isinstance(override, dict):
                raise RegistryError(
                    f"{rel}: '{dataset_id}' must be empty or a mapping"
                )
            unknown = set(override) - {"url", "type", "defaultQuery"}
            if unknown:
                raise RegistryError(
                    f"{rel}: '{dataset_id}' has unknown key(s): "
                    f"{', '.join(sorted(unknown))}"
                )
            protocol = str(override.get("type") or "tabledap")
            if protocol not in PROTOCOLS:
                raise RegistryError(
                    f"{rel}: '{dataset_id}' has type '{protocol}'; "
                    f"expected one of {', '.join(PROTOCOLS)}"
                )
            datasets.append(Dataset(
                id=dataset_id,
                host=host,
                facility=facility,
                service=service,
                registry_path=rel,
                protocol=protocol,
                default_query=str(override.get("defaultQuery") or ""),
                url=str(override.get("url") or "").strip().rstrip("/"),
            ))
        return datasets

    @staticmethod
    def _check_id(dataset_id: str, rel: str) -> None:
        """ERDDAP rejects the whole datasets.xml over one illegal datasetID."""
        if not DATASET_ID_RE.match(dataset_id):
            raise RegistryError(
                f"{rel}: '{dataset_id}' is not a legal ERDDAP datasetID "
                f"(a letter, then letters, digits, underscore, dot or hyphen)"
            )
