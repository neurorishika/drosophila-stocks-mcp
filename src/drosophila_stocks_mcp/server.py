"""Drosophila Stocks MCP server.

Exposes tools that let an LLM query Drosophila melanogaster stock centers
(BDSC, Kyoto/DGRC, VDRC, KDRC, NIG-FLY, FlyORF, NDSSC, THFC) by genotype or
gene, backed by FlyBase's freely redistributable bulk stock and gene-synonym
data, plus its REST API for gene summaries.

Run with::

    uvx drosophila-stocks-mcp            # stdio (Claude Desktop, Cursor, ...)
    MCP_TRANSPORT=streamable-http uvx drosophila-stocks-mcp   # remote/HTTP
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .centers import STOCK_CENTERS
from .flybase import FlyBaseClient

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

mcp = FastMCP(
    "drosophila-stocks",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8000")),
)
_client = FlyBaseClient()

_ORDER_NOTE = (
    "Availability, price, and shipping status are held only by each center's own "
    "ordering system, not by FlyBase. Use `order_url` to check and place an order."
)


@mcp.tool()
def list_stock_centers() -> dict:
    """List the Drosophila stock centers this server can search.

    Returns each center's canonical code, full name, and homepage. Use a code
    (e.g. "BDSC") as the `center` argument to other tools to restrict results.
    """
    return {
        "centers": [
            {"code": c.code, "name": c.name, "homepage": c.homepage}
            for c in STOCK_CENTERS.values()
        ],
        "note": _ORDER_NOTE,
    }


@mcp.tool()
def get_dataset_info() -> dict:
    """Report which FlyBase stock dataset is loaded and how fresh it is.

    Includes the data source (download/cache/local), record count, the FlyBase
    release hint if detectable, and the cache age in days. Useful for citing the
    exact data version in a methods section.
    """
    return _client.dataset_info()


@mcp.tool()
def search_stocks_by_genotype(
    query: str,
    center: Optional[str] = None,
    limit: int = 25,
) -> dict:
    """Search stocks whose genotype matches all whitespace-separated terms in `query`.

    Matching is case-insensitive substring AND-matching over the full genotype
    string, so "UAS-GFP" or "attP2 mir" both work. Terms of 3+ characters also fall
    back to fuzzy substring matching against genotype tokens (in either direction),
    so a term like "tdTomato" will still match a genotype that only spells it "tdT",
    and "CsChrimson" will match one that only spells it "Chrimson" -- FlyBase
    construct names abbreviate reporters/effectors inconsistently across releases
    (tdT, mCh, GCaMP6 vs GCaMP6f, etc.), so if you get zero results for a well-known
    reporter/effector, retry with a shorter/truncated form of the term; the
    response's `no_match_hint` will suggest real tokens found in the dataset when
    available. Optionally restrict to one center by code (e.g. "BDSC", "VDRC").
    Returns up to `limit` records, each with center, stock number, genotype,
    FlyBase report URL, and a deep `order_url`.

    Example: query="Sxl RNAi", center="VDRC".
    """
    limit = max(1, min(int(limit), 200))
    hits = _client.search_by_genotype(query, center=center, limit=limit)
    result = {
        "query": query,
        "center": center,
        "count": len(hits),
        "results": [h.to_dict() for h in hits],
        "note": _ORDER_NOTE,
    }
    if not hits:
        suggestions = _client.suggest_alternatives(query, center=center)
        if suggestions:
            result["no_match_hint"] = (
                "No exact hits, but the dataset contains similar tokens that may be "
                "abbreviated forms of your query terms: " + ", ".join(suggestions) + ". "
                "FlyBase construct names truncate reporters/effectors inconsistently "
                "(e.g. tdTomato -> tdT, mCherry -> mCh) -- try retrying with one of these."
            )
    return result


@mcp.tool()
def search_stocks_by_gene(
    gene: str,
    center: Optional[str] = None,
    limit: int = 25,
) -> dict:
    """Find stocks associated with a gene (symbol, synonym, or FBgn ID).

    Resolves `gene` against FlyBase, then returns stocks whose genotype mentions
    the gene's symbol or a known synonym as a whole token (so "w" won't match
    "white-adjacent" noise). Optionally restrict to one center by code.

    Note: this is a genotype-text match, which is precise for most alleles but can
    miss stocks that carry a gene only via an un-symboled construct. Combine with
    `search_stocks_by_genotype` for exhaustive curation.

    Example: gene="Sxl", center="BDSC".
    """
    limit = max(1, min(int(limit), 200))
    resolved, hits = _client.search_by_gene(gene, center=center, limit=limit)
    return {
        "gene_query": gene,
        "resolved_gene": resolved,
        "center": center,
        "count": len(hits),
        "results": [h.to_dict() for h in hits],
        "note": _ORDER_NOTE,
    }


@mcp.tool()
def get_stock(identifier: str) -> dict:
    """Fetch one stock by FlyBase ID ("FBst0041157") or "CENTER:NUMBER" ("BDSC:1234").

    Returns the full record (center, stock number, genotype, FlyBase report URL,
    order URL) or an error if no matching stock is found in the current dataset.
    """
    rec = _client.get_stock(identifier)
    if rec is None:
        return {"identifier": identifier, "found": False, "error": "No matching stock in current dataset."}
    out = rec.to_dict()
    out["found"] = True
    out["note"] = _ORDER_NOTE
    return out


@mcp.tool()
def resolve_gene(query: str) -> dict:
    """Resolve a gene symbol/synonym/ID to a FlyBase gene record.

    Returns {id (FBgn), symbol, name, summary, synonyms} when resolvable. Returns
    found=False if FlyBase can't resolve it or the API is unreachable.
    """
    resolved = _client.resolve_gene(query)
    if resolved is None:
        return {"query": query, "found": False}
    return {"query": query, "found": True, **resolved}


def main() -> None:
    """Console-script entry point."""
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
    else:
        # streamable-http / sse for remote hosting
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()
