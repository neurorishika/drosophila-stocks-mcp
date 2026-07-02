"""Tests for the offline logic (parsing, center resolution, search).

Network-dependent paths (bulk download, gene REST resolution) are exercised via a
local fixture file set through FLYBASE_STOCKS_FILE, so the suite runs fully offline.
"""

import os
from pathlib import Path

import pytest

from drosophila_stocks_mcp.centers import (
    STOCK_CENTERS,
    flybase_stock_report_url,
    resolve_center_code,
)
from drosophila_stocks_mcp.models import StockRecord, parse_dbxref
from drosophila_stocks_mcp.flybase import (
    FlyBaseClient,
    _index_columns,
    _looks_like_header,
    _fuzzy_match,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_stocks.tsv"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("FLYBASE_STOCKS_FILE", str(FIXTURE))
    c = FlyBaseClient()
    c.ensure_loaded()
    return c


# --------------------------------------------------------------- centers
def test_all_centers_have_codes_and_homepages():
    for code, center in STOCK_CENTERS.items():
        assert center.code == code
        assert center.homepage.startswith("http")


@pytest.mark.parametrize(
    "token,expected",
    [
        ("BDSC", "BDSC"),
        ("bloomington", "BDSC"),
        ("Bloomington Drosophila Stock Center", "BDSC"),
        ("kyoto", "KYOTO"),
        ("vdrc", "VDRC"),
        ("Vienna", "VDRC"),
        ("nig-fly", "NIG"),
        ("nigfly", "NIG"),
        ("flyorf", "FLYORF"),
        ("ndssc", "NDSSC"),
        ("nonsense", None),
        ("", None),
        (None, None),
    ],
)
def test_resolve_center_code(token, expected):
    assert resolve_center_code(token) == expected


def test_order_url_builds_with_number():
    bdsc = STOCK_CENTERS["BDSC"]
    url = bdsc.order_url("1234")
    assert "1234" in url and url.startswith("https://bdsc")


def test_order_url_vdrc_uses_storefront_search():
    # VDRC stock numbers ("v10004") are not Magento product IDs, so the deep link
    # is a storefront search rather than a direct product-view URL.
    vdrc = STOCK_CENTERS["VDRC"]
    url = vdrc.order_url("v27251")
    assert "catalogsearch/result" in url and "v27251" in url


def test_order_url_falls_back_to_homepage():
    kdrc = STOCK_CENTERS["KDRC"]  # no template
    assert kdrc.order_url("99") == kdrc.homepage


def test_flybase_report_url():
    assert flybase_stock_report_url("FBst0041157").endswith("FBst0041157.html")


# --------------------------------------------------------------- dbxref parse
@pytest.mark.parametrize(
    "dbxref,code,number",
    [
        ("BDSC:1234", "BDSC", "1234"),
        ("Bloomington_5678", "BDSC", "5678"),
        ("Kyoto 200300", "KYOTO", "200300"),
        ("VDRC:v27251", "VDRC", "v27251"),
        ("unknown:1", None, "1"),
    ],
)
def test_parse_dbxref(dbxref, code, number):
    c, n = parse_dbxref(dbxref)
    assert c == code
    assert n == number


# --------------------------------------------------------------- real schema detection
def test_looks_like_header_matches_real_columns():
    header = [
        "FBst",
        "collection_short_name",
        "stock_type_cv",
        "species",
        "FB_genotype",
        "description",
        "stock_number",
    ]
    assert _looks_like_header(header)


def test_index_columns_matches_real_header():
    header = [
        "FBst",
        "collection_short_name",
        "stock_type_cv",
        "species",
        "FB_genotype",
        "description",
        "stock_number",
    ]
    idx = _index_columns(header)
    assert idx == {"fbst": 0, "center": 1, "genotype": 4, "description": 5, "stock_number": 6}


# --------------------------------------------------------------- loading
def test_loads_all_records(client):
    assert len(client.records) == 9
    info = client.dataset_info()
    assert info["record_count"] == 9
    # The real bulk file has no "#"-comment release marker; a locally-supplied
    # fixture without one should report release_hint=None rather than raise.
    assert info["release_hint"] is None


def test_release_hint_from_comment_header(tmp_path, monkeypatch):
    fixture = tmp_path / "with_comment.tsv"
    fixture.write_text(
        "## Generated for testing, release FB2026_01\n"
        "FBst\tcollection_short_name\tstock_type_cv\tspecies\tFB_genotype\tdescription\tstock_number\n"
        "FBst0000001\tBloomington\tliving stock ; FBsv:0000002\tDmel\tw[1118]\tw[1118]\t1\n"
    )
    monkeypatch.setenv("FLYBASE_STOCKS_FILE", str(fixture))
    c = FlyBaseClient()
    c.ensure_loaded()
    assert c.dataset_info()["release_hint"] == "FB2026_01"


def test_genotype_falls_back_to_description_when_blank(client):
    rec = client.get_stock("FBst0500600")
    assert rec is not None
    assert rec.genotype == "w[*]; P{UAS-Dcr-2.D}"


def test_records_have_parsed_centers(client):
    codes = {r.center_code for r in client.records}
    assert "BDSC" in codes
    assert "KYOTO" in codes
    assert "VDRC" in codes


# --------------------------------------------------------------- search
def test_search_by_genotype_substring(client):
    hits = client.search_by_genotype("UAS-GFP")
    assert len(hits) == 1
    assert hits[0].fbst_id == "FBst0000001"


def test_search_by_genotype_token_and(client):
    # both tokens must appear
    hits = client.search_by_genotype("UAS Dcr-2")
    assert len(hits) == 1
    assert hits[0].stock_number == "200300"


def test_search_by_genotype_center_filter(client):
    hits = client.search_by_genotype("Sxl", center="VDRC")
    assert len(hits) == 1
    assert hits[0].center_code == "VDRC"


def test_search_by_genotype_limit(client):
    hits = client.search_by_genotype("w", limit=2)
    assert len(hits) <= 2


# --------------------------------------------------- fuzzy abbreviation fallback
# _fuzzy_match needs the ORIGINAL case of both arguments -- that's how it finds
# camelCase/acronym segment boundaries -- so these tests deliberately pass cased
# strings (e.g. "CsChrimson"), not pre-lowered ones.
def test_fuzzy_match_bidirectional_trailing_truncation():
    # "tdT" is an arbitrary-length trailing truncation of "tdTomato" -- allowed
    # unconditionally, regardless of case/segment structure.
    assert _fuzzy_match("tdTomato", "tdT")
    assert _fuzzy_match("tdT", "tdTomato")


def test_fuzzy_match_leading_truncation_at_segment_boundary():
    # "Chrimson" drops the "Cs" modifier tag off the front of "CsChrimson" -- the
    # drop point is a genuine camelCase segment boundary ("Cs" + "Chrimson"), so
    # this is allowed regardless of how long the dropped tag is.
    assert _fuzzy_match("CsChrimson", "Chrimson")
    assert _fuzzy_match("Chrimson", "CsChrimson")


def test_fuzzy_match_respects_min_length():
    # "td" is under the fuzzy-term length floor, so it must not loosely match.
    assert not _fuzzy_match("td", "tdT")


def test_fuzzy_match_rejects_embedded_non_prefix_suffix():
    # "Rim" (the gene *Rim*) is embedded inside "CsChrimson" ("Ch-RIM-son") but is
    # neither a prefix nor a suffix of it, so it must not fuzzy-match -- otherwise
    # searching "CsChrimson" would spuriously return unrelated Rim-carrying stocks.
    assert not _fuzzy_match("CsChrimson", "Rim")
    assert not _fuzzy_match("Rim", "CsChrimson")


def test_fuzzy_match_rejects_leading_truncation_off_segment_boundary():
    # "Son" (the gene *Son*) IS a genuine suffix of "CsChrimson" ("CsChrim-SON"),
    # but that drop point isn't a segment boundary of "CsChrimson" (whose only
    # segments are "Cs"+"Chrimson"), so this must not fuzzy-match even though a
    # naive prefix-or-suffix check alone would allow it.
    assert not _fuzzy_match("CsChrimson", "Son")
    assert not _fuzzy_match("Son", "CsChrimson")


def test_fuzzy_match_handles_arbitrary_length_segment_tags():
    # The segment-boundary rule generalizes to tags of any length, not just
    # 2-character ones like "Cs" -- "Gt" (2 chars) and "myr" (3 chars) both sit at
    # real segment boundaries and should be droppable just the same.
    assert _fuzzy_match("GtACR1", "ACR1")
    assert _fuzzy_match("myrGFP", "GFP")


def test_search_by_genotype_matches_abbreviated_reporter(client):
    # Real genotype only spells the reporter "tdT", not the full "tdTomato".
    hits = client.search_by_genotype("Chrimson tdTomato")
    assert {h.fbst_id for h in hits} == {"FBst0605687"}


def test_search_by_genotype_matches_embedded_abbreviation(client):
    # Real genotype only spells the effector "Chrimson", not the full "CsChrimson".
    # Stocks carrying the unrelated genes "Rim" (embedded substring, not a
    # prefix/suffix) and "Son" (a genuine suffix, but behind too long a dropped
    # prefix to be a plausible modifier tag) must not show up as false hits.
    hits = client.search_by_genotype("CsChrimson")
    assert {h.fbst_id for h in hits} == {"FBst0605687"}


def test_search_by_genotype_fuzzy_does_not_relax_other_terms(client):
    # A genuinely absent term still yields zero hits even with a fuzzy-matchable term.
    hits = client.search_by_genotype("tdTomato Foobar123zzz")
    assert hits == []


def test_suggest_alternatives_finds_abbreviated_token(client):
    suggestions = client.suggest_alternatives("tdTomato")
    assert "tdt" in suggestions


def test_suggest_alternatives_finds_embedded_token(client):
    suggestions = client.suggest_alternatives("CsChrimson")
    assert "chrimson" in suggestions


def test_suggest_alternatives_empty_when_nothing_similar(client):
    assert client.suggest_alternatives("Zzzznotarealtoken12345") == []


def test_search_tool_returns_no_match_hint(monkeypatch):
    from drosophila_stocks_mcp import server

    monkeypatch.setattr(server, "_client", FlyBaseClient())
    monkeypatch.setenv("FLYBASE_STOCKS_FILE", str(FIXTURE))
    result = server.search_stocks_by_genotype("tdTomato Foobar123zzz")
    assert result["count"] == 0
    assert "no_match_hint" in result
    assert "tdt" in result["no_match_hint"].lower()


def test_search_tool_no_hint_when_hits_found(monkeypatch):
    from drosophila_stocks_mcp import server

    monkeypatch.setattr(server, "_client", FlyBaseClient())
    monkeypatch.setenv("FLYBASE_STOCKS_FILE", str(FIXTURE))
    result = server.search_stocks_by_genotype("UAS-GFP")
    assert result["count"] == 1
    assert "no_match_hint" not in result


def test_get_stock_by_fbst(client):
    rec = client.get_stock("FBst0041157")
    assert rec is not None
    assert rec.center_code == "BDSC"
    assert rec.stock_number == "41157"


def test_get_stock_by_center_number(client):
    rec = client.get_stock("BDSC:1234")
    assert rec is not None
    assert rec.fbst_id == "FBst0000001"


def test_get_stock_missing(client):
    assert client.get_stock("BDSC:999999") is None


def test_search_by_gene_offline(client, monkeypatch):
    # Force gene resolution to fail (offline); genotype token match should still work.
    monkeypatch.setattr(client, "resolve_gene", lambda q: None)
    resolved, hits = client.search_by_gene("Sxl")
    assert resolved is None
    # Sxl[f1] and P{KK}Sxl[GD] both mention Sxl as a token
    assert {h.fbst_id for h in hits} == {"FBst0000002", "FBst0300400"}


def test_to_dict_enriches_urls(client):
    rec = client.get_stock("FBst0041157")
    d = rec.to_dict()
    assert d["center_name"].startswith("Bloomington")
    assert d["flybase_url"].endswith("FBst0041157.html")
    assert "41157" in d["order_url"]


def test_record_dataclass_roundtrip():
    r = StockRecord(fbst_id="FBst1", center_code="BDSC", stock_number="7", genotype="w")
    d = r.to_dict()
    assert d["genotype"] == "w"
    assert d["center_code"] == "BDSC"
