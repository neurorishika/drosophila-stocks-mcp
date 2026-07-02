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


@pytest.mark.parametrize(
    "code",
    ["BDSC", "KYOTO", "VDRC", "FLYORF"],
)
def test_order_url_resolves_for_known_stock(live_client, code):
    rec = next((r for r in live_client.records if r.center_code == code), None)
    assert rec is not None, f"no live sample stock found for {code}"
    center = STOCK_CENTERS[code]
    url = center.order_url(rec.stock_number)
    resp = httpx.get(url, follow_redirects=True, headers={"User-Agent": _BROWSER_UA}, timeout=20)
    assert resp.status_code != 404, f"{code} order URL 404'd: {url}"


def test_get_dataset_info_reports_download_source():
    fresh_client = FlyBaseClient()
    info = fresh_client.dataset_info()
    assert info["source"] in {"download", "cache"}
    assert info["release_hint"] is not None
    assert info["record_count"] > 80_000
