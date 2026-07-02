"""Live tests against real FlyBase / stock-center network services.

Deselected by default (see ``addopts = "-m 'not live'"`` in pyproject.toml).
Run explicitly with: ``pytest -m live``

These exercise the assumptions documented in CLAUDE.md that could not be
verified offline: the bulk file URL/schema, gene resolution, and order-URL
templates.
"""

from __future__ import annotations

from collections import Counter

import httpx
import pytest

from drosophila_stocks_mcp.centers import STOCK_CENTERS
from drosophila_stocks_mcp.flybase import FlyBaseClient

pytestmark = pytest.mark.live

# A browser-like UA avoids bot-blocking on some center sites (verified: BDSC
# 403s the default httpx/no-UA request but 200s with this header).
_BROWSER_UA = "Mozilla/5.0 (compatible; drosophila-stocks-mcp-tests/1.0)"


@pytest.fixture(scope="module")
def live_client():
    c = FlyBaseClient()
    c.ensure_loaded()
    return c


def test_download_and_parse_real_bulk_file(live_client):
    records = live_client.records
    assert len(records) > 80_000  # FlyBase has ~80k+ stocks
    assert all(r.genotype for r in records)
    assert sum(1 for r in records if r.fbst_id) / len(records) > 0.95

    codes = Counter(r.center_code for r in records)
    assert codes["BDSC"] > 0
    assert codes["KYOTO"] > 0
    assert codes["VDRC"] > 0
    none_fraction = codes.get(None, 0) / len(records)
    assert none_fraction < 0.05


def test_known_stock_by_fbst_id(live_client):
    rec = live_client.get_stock("FBst0041157")
    assert rec is not None
    assert rec.center_code == "BDSC"
    assert rec.stock_number == "41157"
    assert "mir-932" in rec.genotype


def test_known_stock_by_center_number(live_client):
    rec = live_client.get_stock("BDSC:41157")
    assert rec is not None
    assert rec.fbst_id == "FBst0041157"


def test_resolve_gene_known_symbol(live_client):
    resolved = live_client.resolve_gene("Sxl")
    assert resolved is not None
    assert resolved["id"].startswith("FBgn")
    assert resolved["symbol"] == "Sxl"


def test_resolve_gene_bogus_symbol_returns_none(live_client):
    assert live_client.resolve_gene("NotARealGeneSymbolXYZ999") is None


def test_search_by_gene_finds_stocks(live_client):
    resolved, hits = live_client.search_by_gene("Sxl")
    assert resolved is not None
    assert len(hits) > 0
    assert all("sxl" in h.genotype.lower() for h in hits)


def _http_get(url: str) -> httpx.Response:
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    return httpx.get(url, follow_redirects=True, headers=headers, timeout=20)


# A non-404 status is a weak signal on its own: several of these deep links used
# to 200 while silently landing on a generic search/error page for the *wrong*
# stock (verified live -- see CLAUDE.md's order-URL section for what was broken).
# Where the site server-renders results (KYOTO/VDRC/FLYORF), assert the actual
# stock number shows up and no "not found" marker does. BDSC's results load via
# client-side JS (a Kendo grid) that a plain HTTP GET never executes, so its raw
# HTML is nearly identical for a real vs. bogus stock number -- this was verified
# correct with a real headless browser during development, but isn't asserted
# here since that would pull a browser binary into the test suite for one center.
@pytest.mark.parametrize(
    "code,not_found_marker",
    [
        ("KYOTO", "error:getdbname"),
        ("VDRC", "we could not find"),
        ("FLYORF", "displaying 0 to 0"),
    ],
)
def test_order_url_resolves_to_the_actual_stock(live_client, code, not_found_marker):
    rec = next((r for r in live_client.records if r.center_code == code), None)
    assert rec is not None, f"no live sample stock found for {code}"
    center = STOCK_CENTERS[code]
    url = center.order_url(rec.stock_number)
    resp = _http_get(url)
    assert resp.status_code != 404, f"{code} order URL 404'd: {url}"
    body_low = resp.text.lower()
    assert not_found_marker not in body_low, f"{code} order URL landed on a not-found page: {url}"
    bare_number = rec.stock_number.lstrip("vV") if code == "VDRC" else rec.stock_number
    assert bare_number.lower() in body_low, f"{code} order URL didn't show stock {bare_number}: {url}"


def test_order_url_bdsc_query_is_echoed(live_client):
    rec = next((r for r in live_client.records if r.center_code == "BDSC"), None)
    assert rec is not None, "no live sample stock found for BDSC"
    center = STOCK_CENTERS["BDSC"]
    url = center.order_url(rec.stock_number)
    resp = _http_get(url)
    assert resp.status_code != 404, f"BDSC order URL 404'd: {url}"
    assert rec.stock_number in resp.text, f"BDSC order URL didn't echo stock {rec.stock_number}: {url}"


def test_get_dataset_info_reports_download_source():
    fresh_client = FlyBaseClient()
    info = fresh_client.dataset_info()
    assert info["source"] in {"download", "cache"}
    assert info["release_hint"] is not None
    assert info["record_count"] > 80_000
