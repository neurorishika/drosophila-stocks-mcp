"""Typed data structures returned by the MCP tools."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from .centers import (
    STOCK_CENTERS,
    flybase_stock_report_url,
    get_center,
    resolve_center_code,
)


@dataclass
class StockRecord:
    """A single Drosophila stock as recorded in FlyBase bulk data.

    ``center_code`` is one of the canonical codes in :data:`centers.STOCK_CENTERS`
    (e.g. ``"BDSC"``). ``stock_number`` is the number *within* that center that a
    researcher would actually order by. ``fbst_id`` is FlyBase's stable identifier.
    """

    fbst_id: Optional[str]
    center_code: Optional[str]
    stock_number: Optional[str]
    genotype: str
    #: FlyBase's "stock list description", when it differs from ``genotype`` (the
    #: canonical FB_genotype) -- e.g. it may carry a center's own transformant ID.
    description: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        center = get_center(self.center_code) if self.center_code else None
        d["center_name"] = center.name if center else None
        d["flybase_url"] = (
            flybase_stock_report_url(self.fbst_id) if self.fbst_id else None
        )
        d["order_url"] = center.order_url(self.stock_number) if center else None
        return d


def parse_dbxref(dbxref: str) -> tuple[Optional[str], Optional[str]]:
    """Split a FlyBase stock dbxref into (canonical_center_code, stock_number).

    FlyBase encodes the holding center + local number in a single field whose exact
    punctuation has varied across releases, e.g. ``BDSC:1234``, ``Bloomington_1234``,
    ``Kyoto 101234``. We split on the first run of separator characters, resolve the
    left side to a canonical center code, and keep the right side as the number.
    """
    if not dbxref:
        return None, None
    text = dbxref.strip()
    # Find the boundary between the center label and the numeric/id portion.
    sep_positions = [i for i, ch in enumerate(text) if ch in ":_/ \t"]
    if sep_positions:
        # Split on the first separator, but coalesce consecutive separators.
        idx = sep_positions[0]
        left = text[:idx]
        right = text[idx:].lstrip(":_/ \t")
    else:
        # No separator: try to peel a trailing number off the end.
        j = len(text)
        while j > 0 and (text[j - 1].isdigit()):
            j -= 1
        left, right = text[:j], text[j:]
    code = resolve_center_code(left)
    number = right.strip() or None
    return code, number


def known_center_codes() -> list[str]:
    return list(STOCK_CENTERS.keys())
