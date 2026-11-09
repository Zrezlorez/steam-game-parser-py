"""Domain exceptions shared by the collector.

The concrete exceptions deliberately inherit from the builtin types used by the
original implementation (``ValueError`` and ``RuntimeError``).  This keeps the
public CLI contract stable while giving callers and tests a precise type to
catch.
"""


class SteamCatalogError(Exception):
    """Base class for every expected Steam Catalog failure."""


class ConfigurationError(ValueError, SteamCatalogError):
    """Raised when settings or command arguments are invalid."""


class SourceResponseError(ValueError, SteamCatalogError):
    """Raised when a provider returns a syntactically invalid response."""


class SourceRequestError(RuntimeError, SteamCatalogError):
    """Raised when a provider request fails after retries."""


class LeaseLostError(RuntimeError, SteamCatalogError):
    """Raised when a worker tries to finish a job it no longer owns."""


class SchemaError(RuntimeError, SteamCatalogError):
    """Raised when the SQLite schema is missing, unsupported, or corrupt."""
