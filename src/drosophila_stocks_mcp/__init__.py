"""Drosophila Stocks MCP: query fly stock centers via FlyBase data."""

__version__ = "0.1.0"

from .server import mcp, main

__all__ = ["mcp", "main", "__version__"]
