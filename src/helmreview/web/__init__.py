"""Self-hosted web UI for helmreview (`helmreview serve`)."""

from .server import WebApp, serve

__all__ = ["WebApp", "serve"]
