"""bytegurud — демон единой модели приложений ByteguruOS."""

from __future__ import annotations

from bgd.api import Context, Service
from bgd.model import AppKind, Application

__version__ = "0.0.1"

__all__ = ["AppKind", "Application", "Context", "Service", "__version__"]
