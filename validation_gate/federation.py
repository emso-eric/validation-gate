"""
The federation registry: federation/<Facility>/<Service>.yaml -> Dataset list.

A bare datasetID uses the defaults - tabledap, and the URL derived as
<service url>/<type>/<datasetID>. A body is only needed to override one of
those or to set a defaultQuery.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
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

# ---------------------------------------------------------------------------
# Where a facility may point the gate
#
# `url:` is fetched by the compliance engine running on the central node - the
# same host as PostgreSQL, Grafana and the central ERDDAP. Unchecked, a registry
# entry is a request issued from inside that network position, and a facility
# file may declare any number of datasets, each with its own url:, so one merge
# can mean hundreds of such requests in a single run.
#
# What this is: a guard against pointing the gate at infrastructure, by accident
# or casually. It stops http://127.0.0.1:5432, the cloud metadata address, and
# `10.0.0.5.nip.io`-style wrappers around a private address.
#
# What this is not: containment. The engine fetches with `requests`, which
# follows redirects by default, so a public host that passes here can still
# answer 302 and send the fetch anywhere. Only connect-time egress control
# closes that, and it is deliberately not attempted here. Do not describe this
# function as preventing SSRF.
SCHEMES = frozenset({"http", "https"})

# Private-address ERDDAPs the central node may reach, by hostname. Empty on
# purpose: every federated service today is on a public name. If a facility ever
# runs an ERDDAP the central node can only reach internally, add its hostname
# here - this file is administrator-owned, so that is already a deliberate and
# reviewed decision rather than something a facility can grant itself.
ALLOWED_PRIVATE_HOSTS: frozenset[str] = frozenset()


class RegistryError(Exception):
    """The registry is malformed. Never guess what a facility meant."""


def _addresses_of(hostname: str) -> list[ipaddress._BaseAddress]:
    """
    Every address *hostname* resolves to, or none if it cannot be resolved.

    A literal address needs no lookup, which is what keeps the obvious case -
    a facility writing an IP straight into the YAML - decided without a network
    call. `gate.py check` is documented as working offline, and resolution
    failing must therefore not fail the registry: an unresolvable hostname is
    reported as unreachable at run time, which is the honest verdict for it.
    """
    try:
        return [ipaddress.ip_address(hostname)]
    except ValueError:
        pass
    try:
        info = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    return [ipaddress.ip_address(item[4][0]) for item in info]


def _reject_unroutable(url: str, rel: str, what: str) -> None:
    """Refuse a URL that does not point at a public HTTP service."""
    parsed = urlparse(url)

    if parsed.scheme not in SCHEMES:
        raise RegistryError(
            f"{rel}: {what} must start with http:// or https://, not "
            f"'{parsed.scheme or url}'"
        )
    if parsed.username or parsed.password:
        raise RegistryError(f"{rel}: {what} must not carry credentials: '{url}'")

    hostname = parsed.hostname
    if not hostname:
        raise RegistryError(f"{rel}: {what} has no hostname: '{url}'")
    if hostname in ALLOWED_PRIVATE_HOSTS:
        return

    # is_global is the whole test rather than a list of is_private/is_loopback/
    # is_link_local: it is false for every range that is not routable on the
    # public internet, including the ones a hand-written list forgets - carrier
    # NAT, the documentation ranges, IPv6 unique-local.
    for address in _addresses_of(hostname):
        if not address.is_global:
            via = "" if str(address) == hostname else f" (resolves to {address})"
            raise RegistryError(
                f"{rel}: {what} points at '{hostname}'{via}, which is not a "
                f"public address. The gate fetches this URL from the central "
                f"node, so it may only name a server reachable on the public "
                f"internet. If this facility really serves ERDDAP on an "
                f"internal address, an administrator must add it to "
                f"ALLOWED_PRIVATE_HOSTS in validation_gate/federation.py."
            )


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
        # Checked here, before any dataset is built: every dataset that does not
        # override url: derives it from this host, so one check covers them all.
        _reject_unroutable(host, rel, "the service url")

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
            # A per-dataset url: bypasses the service url entirely, so it needs
            # the same check. This is the field that makes a single file able to
            # name hundreds of separate targets.
            dataset_url = str(override.get("url") or "").strip().rstrip("/")
            if dataset_url:
                _reject_unroutable(dataset_url, rel, f"the url for '{dataset_id}'")
            datasets.append(Dataset(
                id=dataset_id,
                host=host,
                facility=facility,
                service=service,
                registry_path=rel,
                protocol=protocol,
                default_query=str(override.get("defaultQuery") or ""),
                url=dataset_url,
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
