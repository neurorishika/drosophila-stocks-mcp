"""Offline cache-behavior tests: download-once, refresh-on-stale, stale-cache
fallback on download failure, and hard-failure with no cache at all.

All network calls are monkeypatched; nothing here touches the real network.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from drosophila_stocks_mcp.flybase import FlyBaseClient

FIXTURE = Path(__file__).parent / "fixtures" / "sample_stocks.tsv"


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("FLYBASE_STOCKS_FILE", raising=False)
    monkeypatch.setenv("DROSOPHILA_STOCKS_CACHE", str(tmp_path))
    return tmp_path


def _fake_download_copies_fixture(cache_file: Path) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(gzip.compress(FIXTURE.read_bytes()))


def test_first_call_downloads_second_call_uses_cache(isolated_cache, monkeypatch):
    calls = {"n": 0}

    def fake_download(self, cache_file):
        calls["n"] += 1
        _fake_download_copies_fixture(cache_file)

    monkeypatch.setattr(FlyBaseClient, "_download_to_cache", fake_download)

    c1 = FlyBaseClient()
    c1.ensure_loaded()
    assert calls["n"] == 1
    assert c1.dataset_info()["source"] == "download"

    c2 = FlyBaseClient()
    c2.ensure_loaded()
    assert calls["n"] == 1  # not downloaded again
    assert c2.dataset_info()["source"] == "cache"
    assert c2.dataset_info()["record_count"] == c1.dataset_info()["record_count"]


def test_stale_cache_triggers_refresh(isolated_cache, monkeypatch):
    monkeypatch.setenv("DROSOPHILA_STOCKS_MAX_AGE_DAYS", "0")
    calls = {"n": 0}

    def fake_download(self, cache_file):
        calls["n"] += 1
        _fake_download_copies_fixture(cache_file)

    monkeypatch.setattr(FlyBaseClient, "_download_to_cache", fake_download)

    cache_file = isolated_cache / "stocks.tsv.gz"
    _fake_download_copies_fixture(cache_file)
    import os
    import time

    old_time = time.time() - 100 * 86400
    os.utime(cache_file, (old_time, old_time))

    c = FlyBaseClient()
    c.ensure_loaded()
    assert calls["n"] == 1  # stale cache was refreshed, not reused as-is
    assert c.dataset_info()["source"] == "download"


def test_download_failure_with_existing_cache_falls_back_to_stale(isolated_cache, monkeypatch):
    monkeypatch.setenv("DROSOPHILA_STOCKS_MAX_AGE_DAYS", "0")
    cache_file = isolated_cache / "stocks.tsv.gz"
    _fake_download_copies_fixture(cache_file)

    def failing_download(self, cache_file):
        raise RuntimeError("network down")

    monkeypatch.setattr(FlyBaseClient, "_download_to_cache", failing_download)

    c = FlyBaseClient()
    c.ensure_loaded()
    assert c.dataset_info()["source"] == "stale-cache"
    assert c.dataset_info()["record_count"] > 0


def test_download_failure_with_no_cache_reraises(isolated_cache, monkeypatch):
    def failing_download(self, cache_file):
        raise RuntimeError("network down")

    monkeypatch.setattr(FlyBaseClient, "_download_to_cache", failing_download)

    c = FlyBaseClient()
    with pytest.raises(RuntimeError, match="network down"):
        c.ensure_loaded()
