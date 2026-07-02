"""Registry of Drosophila stock centers.

Each center gets a canonical code, a set of name/token aliases used to recognise
it in FlyBase bulk-data ``dbxref`` fields, a human-readable label, and helpers to
build links to the center's own record/order page and to the FlyBase stock report.

FlyBase is the authoritative, freely redistributable source for the stock *records*
(genotype, stock number, which center holds the line). Live *availability* / price /
shipping status is only known to each center's own ordering system; we therefore
generate deep links to those systems rather than scraping them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote


@dataclass(frozen=True)
class StockCenter:
    """Metadata for a single Drosophila stock center."""

    code: str
    name: str
    homepage: str
    #: Lower-cased tokens that may appear in a FlyBase dbxref identifying this center.
    aliases: tuple[str, ...] = field(default_factory=tuple)
    #: Template for a deep link to a specific stock, ``{num}`` -> stock number.
    #: ``None`` means we can only link to the center's search/home page.
    order_url_template: Optional[str] = None

    def order_url(self, stock_number: str | int | None) -> str:
        """Best-effort link to order / view ``stock_number`` at this center."""
        if stock_number is None or self.order_url_template is None:
            return self.homepage
        return self.order_url_template.format(num=quote(str(stock_number)))


# Canonical registry. Codes are stable; treat them as the public identifiers.
STOCK_CENTERS: dict[str, StockCenter] = {
    "BDSC": StockCenter(
        code="BDSC",
        name="Bloomington Drosophila Stock Center",
        homepage="https://bdsc.indiana.edu/",
        aliases=("bdsc", "bloomington", "bl", "indiana"),
        order_url_template="https://bdsc.indiana.edu/Home/Search?presearch={num}",
    ),
    "KYOTO": StockCenter(
        code="KYOTO",
        name="Kyoto Stock Center (DGRC, Kyoto Institute of Technology)",
        homepage="https://kyotofly.kit.jp/cgi-bin/stocks/index.cgi",
        aliases=("kyoto", "dgrc kyoto", "kit", "dgrc"),
        order_url_template=(
            "https://kyotofly.kit.jp/cgi-bin/stocks/search_res_det.cgi?DB_NUM={num}"
        ),
    ),
    "VDRC": StockCenter(
        code="VDRC",
        name="Vienna Drosophila Resource Center",
        homepage="https://shop.vbc.ac.at/vdrc_store/",
        aliases=("vdrc", "vienna"),
        # VDRC stock numbers (e.g. "v10004") are not Magento catalog product IDs;
        # there is no direct product-id deep link, so we link into their storefront
        # search instead, which does resolve to the right stock.
        order_url_template="https://shop.vbc.ac.at/vdrc_store/catalogsearch/result/?q={num}",
    ),
    "KDRC": StockCenter(
        code="KDRC",
        name="Korea Drosophila Resource Center",
        homepage="https://kdrc.kr/index.php",
        aliases=("kdrc", "korea"),
        order_url_template=None,
    ),
    "NIG": StockCenter(
        code="NIG",
        name="NIG-FLY (National Institute of Genetics, Japan)",
        homepage="https://shigen.nig.ac.jp/fly/nigfly/",
        aliases=("nig", "nig-fly", "nigfly"),
        order_url_template=None,
    ),
    "FLYORF": StockCenter(
        code="FLYORF",
        name="FlyORF (Zurich ORFeome Project)",
        homepage="https://flyorf.ch/",
        aliases=("flyorf", "orf"),
        order_url_template="https://flyorf.ch/index.php/?s={num}",
    ),
    "NDSSC": StockCenter(
        code="NDSSC",
        name="National Drosophila Species Stock Center (Cornell University)",
        homepage="https://www.drosophilaspecies.com/",
        aliases=("ndssc", "cornell"),
        order_url_template=None,
    ),
}

# Reverse lookup: every alias/token -> canonical code (built once at import).
_ALIAS_TO_CODE: dict[str, str] = {}
for _center in STOCK_CENTERS.values():
    _ALIAS_TO_CODE[_center.code.lower()] = _center.code
    for _alias in _center.aliases:
        _ALIAS_TO_CODE[_alias.lower()] = _center.code


def resolve_center_code(token: str | None) -> Optional[str]:
    """Map a free-text center token to a canonical code, or ``None`` if unknown.

    Accepts codes ("BDSC"), names ("Bloomington"), and common dbxref prefixes.
    Matching is case-insensitive and tolerant of surrounding punctuation.
    """
    if not token:
        return None
    key = token.strip().lower().replace("_", " ").replace("-", " ").strip()
    if key in _ALIAS_TO_CODE:
        return _ALIAS_TO_CODE[key]
    # Fall back to a token-wise scan: "bloomington drosophila stock center" etc.
    for word in key.split():
        if word in _ALIAS_TO_CODE:
            return _ALIAS_TO_CODE[word]
    # Also try the collapsed form ("nigfly").
    collapsed = key.replace(" ", "")
    return _ALIAS_TO_CODE.get(collapsed)


def flybase_stock_report_url(fbst_id: str) -> str:
    """FlyBase stock report page for an ``FBst`` identifier."""
    return f"https://flybase.org/reports/{fbst_id}.html"


def get_center(code: str) -> Optional[StockCenter]:
    return STOCK_CENTERS.get(code.upper())
