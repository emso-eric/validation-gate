"""
EMSO Validation Gate.

Central-node half of the EMSO DataOps framework. It reconciles the git-hosted
federation registry with a PostgreSQL history, validates every declared
dataset against the EMSO Metadata Specifications, and federates only the
compliant ones into the central ERDDAP by rewriting datasets.xml.

Four components:

    Federation      federation/*.yaml -> Dataset objects
    ValidationGate  the policy and the run
    GateDatabase    PostgreSQL history
    ErddapHelper    datasets.xml and per-dataset reloads
"""

from .config import Config, ConfigError
from .db import DatabaseError, GateDatabase
from .erddap import ErddapError, ErddapHelper
from .federation import Dataset, Federation, RegistryError
from .validation_gate import (
    VALIDATION_STATES, ValidationGate, ValidationReport, shuffle_datasets,
)

__version__ = "0.2.0"

__all__ = [
    "Config", "ConfigError",
    "Dataset", "Federation", "RegistryError",
    "GateDatabase", "DatabaseError",
    "ErddapHelper", "ErddapError",
    "ValidationGate", "ValidationReport", "VALIDATION_STATES",
    "shuffle_datasets", "__version__",
]
