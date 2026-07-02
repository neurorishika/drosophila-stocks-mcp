"""Offline robustness/edge-case tests: malformed input, encodings, clamping."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from drosophila_stocks_mcp.flybase import FlyBaseClient
from drosophila_stocks_mcp.server import search_stocks_by_genotype

HEADER = "FBst\tcollection_short_name\tstock_type_cv\tspecies\tFB_genotype\tdescription\tstock_number\n"


def _client_for(path: Path) -> FlyBaseClient:
    c = FlyBaseClient()
    c._load_from_path(path, source="test")  # noqa: SLF001 - direct unit exercise
    return c


def test_malformed_rows_are_skipped_without_crashing(tmp_path):
    p = tmp_path / "malformed.tsv"
    p.write_text(
        HEADER
        + "\n"  # blank line
        + "too\tfew\tcols\n"  # too few columns, no valid FBst -> dropped
        + "garbage\tBDSC\t\tDmel\t\t\t\n"  # not a real FBst id, no genotype -> dropped
        + "FBst0000002\tBDSC\tstock\tDmel\tw[1118]\tdesc\t2\t\n"  # trailing tab
        + "FBst0000003\tBDSC\tstock\tDmel\tw[1118]; UAS-GFP\tdesc\t3\r\n"  # CRLF
    )
    c = _client_for(p)
    ids = {r.fbst_id for r in c.records}
    assert "garbage" not in ids
    assert "FBst0000002" in ids
    assert "FBst0000003" in ids
    rec3 = next(r for r in c.records if r.fbst_id == "FBst0000003")
    assert rec3.genotype == "w[1118]; UAS-GFP"


def test_non_utf8_bytes_do_not_crash(tmp_path):
    p = tmp_path / "badbytes.tsv"
    with open(p, "wb") as f:
        f.write(HEADER.encode("utf-8"))
        f.write(b"FBst0000004\tBDSC\tstock\tDmel\tw[1118]; " + b"\xff\xfe" + b"UAS-GFP\tdesc\t4\n")
    c = _client_for(p)
    assert any(r.fbst_id == "FBst0000004" for r in c.records)


def test_gz_and_plain_tsv_both_load(tmp_path):
    rows = HEADER + "FBst0000005\tBDSC\tstock\tDmel\tw[1118]\tdesc\t5\n"
    plain = tmp_path / "plain.tsv"
    plain.write_text(rows)
    gz = tmp_path / "gzipped.tsv.gz"
    gz.write_bytes(gzip.compress(rows.encode("utf-8")))

    c_plain = _client_for(plain)
    c_gz = _client_for(gz)
    assert [r.fbst_id for r in c_plain.records] == [r.fbst_id for r in c_gz.records] == ["FBst0000005"]


def test_unicode_in_genotype_round_trips(tmp_path):
    p = tmp_path / "unicode.tsv"
    genotype = "w[1118]; P{UAS-α-synuclein}attP2"  # Greek alpha
    p.write_text(HEADER + f"FBst0000006\tBDSC\tstock\tDmel\t{genotype}\tdesc\t6\n", encoding="utf-8")
    c = _client_for(p)
    rec = next(r for r in c.records if r.fbst_id == "FBst0000006")
    assert rec.genotype == genotype
    assert rec.to_dict()["genotype"] == genotype


def test_search_by_genotype_empty_and_whitespace_query_matches_everything(tmp_path):
    p = tmp_path / "two.tsv"
    p.write_text(
        HEADER
        + "FBst0000007\tBDSC\tstock\tDmel\tw[1118]\tdesc\t7\n"
        + "FBst0000008\tVDRC\tstock\tDmel\ty[1] w[*]\tdesc\t8\n"
    )
    c = _client_for(p)
    assert len(c.search_by_genotype("", limit=200)) == 2
    assert len(c.search_by_genotype("   ", limit=200)) == 2


def test_search_by_genotype_unknown_center_returns_empty(tmp_path):
    p = tmp_path / "one.tsv"
    p.write_text(HEADER + "FBst0000009\tBDSC\tstock\tDmel\tw[1118]\tdesc\t9\n")
    c = _client_for(p)
    assert c.search_by_genotype("w[1118]", center="NOPE", limit=200) == []


@pytest.mark.parametrize(
    "raw_limit,expected_max",
    [(0, 1), (1, 1), (99999, 200), (200, 200), (201, 200)],
)
def test_search_stocks_by_genotype_tool_clamps_limit(monkeypatch, raw_limit, expected_max):
    monkeypatch.setenv(
        "FLYBASE_STOCKS_FILE", str(Path(__file__).parent / "fixtures" / "sample_stocks.tsv")
    )
    import drosophila_stocks_mcp.server as server_mod

    server_mod._client = server_mod.FlyBaseClient()
    result = search_stocks_by_genotype("", limit=raw_limit)
    assert result["count"] <= expected_max


def test_get_stock_garbage_identifier_returns_not_found(monkeypatch):
    monkeypatch.setenv(
        "FLYBASE_STOCKS_FILE", str(Path(__file__).parent / "fixtures" / "sample_stocks.tsv")
    )
    import drosophila_stocks_mcp.server as server_mod

    server_mod._client = server_mod.FlyBaseClient()
    result = server_mod.get_stock("totally-not-a-real-id-###")
    assert result["found"] is False
