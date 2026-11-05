"""SHAMS — Sequential Heterogeneous AI Manufacturing System (domain-neutral core)."""
from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("shams")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "2.0.0"
