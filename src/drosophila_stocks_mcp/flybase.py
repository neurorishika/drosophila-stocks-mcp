"""Client for FlyBase data used to answer stock queries.

Two bulk precomputed files, both free for academic use, downloaded once, cached
locally, and indexed in memory:

1. **Stocks file** (``precomputed_files/stocks/stocks_FB<release>.tsv.gz``) —
   authoritative, complete list of stocks with genotypes across all centers.
2. **Synonym file** (``precomputed_files/synonyms/fb_synonym_fb_<release>.tsv.gz``)
   — maps every FlyBase gene symbol/synonym to its current ``FBgn`` id. This is
   used for gene resolution because FlyBase's REST API
   (https://flybase.github.io/api/swagger-ui/) has **no symbol-to-FBgn endpoint**
   (verified against the published OpenAPI spec) — only
   ``/gene/summaries/auto/{fbgn}``, which needs an FBgn already. The REST API is
   still used, opportunistically, to fetch a gene summary once we have an FBgn.

Both bulk files are release-stamped (no stable "_current" filename), so the real
filename is discovered by scraping the ``current/`` directory listing rather than
guessed. Rate limited by FlyBase to 3 requests/second on the REST API; we stay
well under that.

Design goals: work offline once cached, never assume an exact column order (FlyBase
file layouts have shifted between releases, so we detect columns by content), and
degrade gracefully when the network or a data source is unavailable.
"""

from __future__ import annotations

import gzip
import io
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import httpx

from .centers import resolve_center_code
from .models import StockRecord, parse_dbxref

logger = logging.getLogger("drosophila_stocks_mcp.flybase")

FBST_RE = re.compile(r"FBst\d{7,}")
FBGN_RE = re.compile(r"FBgn\d{7,}")

# Where FlyBase publishes releases. The ``current`` alias always points at the
# latest release's directory, but the files inside it are release-stamped
# (e.g. "stocks_FB2026_02.tsv.gz") -- there is no generic "_current" filename, so
# we discover the real name from the directory listing.
_RELEASES_BASE = "https://s3ftp.flybase.org/releases/current/precomputed_files"
_STOCKS_DIR_URL = f"{_RELEASES_BASE}/stocks/"
_SYNONYMS_DIR_URL = f"{_RELEASES_BASE}/synonyms/"
_STOCKS_FILENAME_RE = re.compile(r"stocks_FB\d{4}_\d{2}\.tsv\.gz")
_SYNONYM_FILENAME_RE = re.compile(r"fb_synonym_fb_\d{4}_\d{2}\.tsv\.gz")
_RELEASE_RE = re.compile(r"FB\d{4}_\d{2}")

_FLYBASE_API_BASE = "https://api.flybase.org/api/v1.0"

# Env overrides let users pin a release, point at a local file, or relocate cache.
ENV_STOCKS_URL = "FLYBASE_STOCKS_URL"
ENV_STOCKS_FILE = "FLYBASE_STOCKS_FILE"
ENV_SYNONYM_URL = "FLYBASE_SYNONYM_URL"
ENV_SYNONYM_FILE = "FLYBASE_SYNONYM_FILE"
ENV_CACHE_DIR = "DROSOPHILA_STOCKS_CACHE"
ENV_MAX_AGE_DAYS = "DROSOPHILA_STOCKS_MAX_AGE_DAYS"

_USER_AGENT = "drosophila-stocks-mcp (+https://biocontext.ai; academic research)"


def _http_client(timeout: float) -> httpx.Client:
    """Build the shared httpx.Client config.

    ``http2=True`` is required, not cosmetic: FlyBase's CloudFront-fronted
    ``s3ftp.flybase.org`` reliably answers plain HTTP/1.1 GETs on the release
    directory-listing endpoint with an empty ``202 Accepted`` body (verified
    reproducible, 5/5 requests) while the identical request over HTTP/2 (what
    curl uses by default, and what browsers use) returns the real ``200`` page.
    Without this, a cold cache (first real use) fails to resolve the download
    URL.
    """
    return httpx.Client(
        timeout=timeout, headers={"User-Agent": _USER_AGENT}, follow_redirects=True, http2=True
    )


def _cache_dir() -> Path:
    base = os.environ.get(ENV_CACHE_DIR)
    if base:
        return Path(base).expanduser()
    return Path.home() / ".cache" / "drosophila-stocks-mcp"


@dataclass
class DatasetInfo:
    source: str
    record_count: int
    cached_path: Optional[str]
    fetched_at: Optional[float]
    release_hint: Optional[str]

    def to_dict(self) -> dict:
        age = None
        if self.fetched_at:
            age = round((time.time() - self.fetched_at) / 86400.0, 2)
        return {
            "source": self.source,
            "record_count": self.record_count,
            "cached_path": self.cached_path,
            "fetched_at_epoch": self.fetched_at,
            "cache_age_days": age,
            "release_hint": self.release_hint,
        }


def _looks_like_header(fields: list[str]) -> bool:
    joined = " ".join(fields).lower()
    return "genotype" in joined or "collection" in joined or ("stock" in joined and "id" in joined)


def _index_columns(header: list[str]) -> dict[str, int]:
    """Map logical column -> index using fuzzy header names.

    Matches the real stocks bulk file header (verified against a live download):
    ``FBst  collection_short_name  stock_type_cv  species  FB_genotype  description
    stock_number``.
    """
    idx: dict[str, int] = {}
    for i, name in enumerate(header):
        low = name.strip().lower().lstrip("#").strip()
        if ("genotype" in low) and "genotype" not in idx:
            idx["genotype"] = i
        elif "description" in low and "description" not in idx:
            idx["description"] = i
        elif ("fbst" in low or ("stock" in low and "id" in low)) and "fbst" not in idx:
            idx["fbst"] = i
        elif "stock_number" in low and "stock_number" not in idx:
            idx["stock_number"] = i
        elif ("collection" in low or "stock_center" in low or low == "center") and "center" not in idx:
            idx["center"] = i
    return idx


class FlyBaseClient:
    """Loads, caches, and searches FlyBase stock data; resolves gene symbols."""

    def __init__(self, *, timeout: float = 60.0) -> None:
        self._records: list[StockRecord] = []
        self._info: Optional[DatasetInfo] = None
        self._timeout = timeout
        self._gene_cache: dict[str, Optional[dict]] = {}
        self._download_release_hint: Optional[str] = None
        self._synonym_index: Optional[dict[str, dict]] = None

    # ------------------------------------------------------------------ loading
    def _max_age_seconds(self) -> float:
        days = float(os.environ.get(ENV_MAX_AGE_DAYS, "30"))
        return days * 86400.0

    def ensure_loaded(self) -> None:
        if self._records:
            return
        # 1. Explicit local file wins (great for tests / air-gapped use).
        local = os.environ.get(ENV_STOCKS_FILE)
        if local:
            self._load_from_path(Path(local).expanduser(), source=f"local:{local}")
            return
        # 2. Cached download, if fresh enough.
        cache_file = _cache_dir() / "stocks.tsv.gz"
        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age <= self._max_age_seconds():
                self._download_release_hint = self._read_release_sidecar(cache_file)
                self._load_from_path(cache_file, source="cache", fetched=cache_file.stat().st_mtime)
                return
        # 3. Fresh download.
        try:
            self._download_to_cache(cache_file)
            self._load_from_path(cache_file, source="download", fetched=time.time())
        except Exception as exc:  # network down but stale cache present -> use it
            if cache_file.exists():
                logger.warning("Download failed (%s); using stale cache.", exc)
                self._download_release_hint = self._read_release_sidecar(cache_file)
                self._load_from_path(cache_file, source="stale-cache", fetched=cache_file.stat().st_mtime)
            else:
                raise

    @staticmethod
    def _release_sidecar(cache_file: Path) -> Path:
        return cache_file.with_suffix(cache_file.suffix + ".release")

    def _read_release_sidecar(self, cache_file: Path) -> Optional[str]:
        sidecar = self._release_sidecar(cache_file)
        return sidecar.read_text().strip() if sidecar.exists() else None

    def _resolve_download_url(self, dir_url: str, filename_re: re.Pattern) -> str:
        """Discover the release-stamped filename in a FlyBase directory listing.

        FlyBase's bulk files don't have a stable "_current" name; the ``current``
        release *directory* is stable but the file inside is named e.g.
        ``stocks_FB2026_02.tsv.gz``. We scrape the (plain HTML) directory listing
        for the first filename matching ``filename_re``.
        """
        with _http_client(self._timeout) as client:
            resp = client.get(dir_url)
            resp.raise_for_status()
            match = filename_re.search(resp.text)
        if not match:
            raise RuntimeError(f"Could not find a matching file at {dir_url}")
        return dir_url + match.group(0)

    def _download_to_cache(self, cache_file: Path) -> None:
        url = os.environ.get(ENV_STOCKS_URL) or self._resolve_download_url(_STOCKS_DIR_URL, _STOCKS_FILENAME_RE)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Downloading FlyBase stock data from %s", url)
        with _http_client(self._timeout) as client:
            resp = client.get(url)
            resp.raise_for_status()
            cache_file.write_bytes(resp.content)
        m = _RELEASE_RE.search(url)
        self._download_release_hint = m.group(0) if m else None
        if self._download_release_hint:
            self._release_sidecar(cache_file).write_text(self._download_release_hint)

    def _open_text(self, path: Path) -> Iterable[str]:
        raw = path.read_bytes()
        if path.suffix == ".gz" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return io.StringIO(raw.decode("utf-8", errors="replace")).readlines()

    def _load_from_path(self, path: Path, *, source: str, fetched: Optional[float] = None) -> None:
        lines = self._open_text(path)
        records: list[StockRecord] = []
        col_idx: Optional[dict[str, int]] = None
        release_hint: Optional[str] = None
        for line in lines:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            if line.startswith("#"):
                m = re.search(r"FB\d{4}_\d{2}", line)
                if m and not release_hint:
                    release_hint = m.group(0)
                fields = line.lstrip("#").strip().split("\t")
                if _looks_like_header(fields):
                    col_idx = _index_columns(fields)
                continue
            fields = line.split("\t")
            if col_idx is None and _looks_like_header(fields):
                col_idx = _index_columns(fields)
                continue
            rec = self._row_to_record(fields, col_idx)
            if rec is not None:
                records.append(rec)
        self._records = records
        self._info = DatasetInfo(
            source=source,
            record_count=len(records),
            cached_path=str(path),
            fetched_at=fetched,
            # The current bulk file has no "#"-comment release marker; fall back
            # to the release stamp parsed from the downloaded filename, if any.
            release_hint=release_hint or self._download_release_hint,
        )
        logger.info("Loaded %d stock records (%s).", len(records), source)

    def _row_to_record(self, fields: list[str], col_idx: Optional[dict[str, int]]) -> Optional[StockRecord]:
        def get(key: str) -> str:
            i = col_idx.get(key) if col_idx else None
            return fields[i].strip() if i is not None and i < len(fields) else ""

        if col_idx:
            fbst = get("fbst")
            center_name = get("center")
            stock_number = get("stock_number")
            genotype = get("genotype")
            description = get("description")
        else:
            # No header detected: infer by content.
            fbst = next((f for f in fields if FBST_RE.fullmatch(f.strip())), "")
            center_name = next((f for f in fields if resolve_center_code(f) is not None), "")
            description = ""
            genotype = max(fields, key=len).strip() if fields else ""
            stock_number = next(
                (f.strip() for f in reversed(fields) if f.strip().isdigit()), ""
            )
        # The canonical FlyBase genotype is occasionally blank; the stock-list
        # description (verified on live data, e.g. Kyoto Dsim stocks) still carries
        # useful text in that case.
        display_genotype = genotype or description
        # Guard against stray bytes/malformed trailing rows (e.g. a lone Ctrl-Z/EOF
        # marker byte was observed at the end of a real downloaded file) -- a
        # non-empty ``fbst`` alone isn't enough; it must look like a real FBst id.
        if not display_genotype and not FBST_RE.fullmatch(fbst):
            return None
        center_code = resolve_center_code(center_name) if center_name else None
        return StockRecord(
            fbst_id=fbst or None,
            center_code=center_code,
            stock_number=stock_number or None,
            genotype=display_genotype,
            description=description or None,
        )

    # ------------------------------------------------------------------ queries
    @property
    def records(self) -> list[StockRecord]:
        self.ensure_loaded()
        return self._records

    def dataset_info(self) -> dict:
        self.ensure_loaded()
        assert self._info is not None
        return self._info.to_dict()

    def search_by_genotype(
        self, query: str, *, center: Optional[str] = None, limit: int = 25
    ) -> list[StockRecord]:
        """Token-AND substring search over genotype+description, optional center filter."""
        self.ensure_loaded()
        terms = [t.lower() for t in re.split(r"\s+", query.strip()) if t]
        want_center = center.upper() if center else None
        hits: list[StockRecord] = []
        for rec in self._records:
            if want_center and rec.center_code != want_center:
                continue
            g = rec.genotype.lower()
            if rec.description:
                g = g + " " + rec.description.lower()
            if all(t in g for t in terms):
                hits.append(rec)
                if len(hits) >= limit:
                    break
        return hits

    def get_stock(self, identifier: str) -> Optional[StockRecord]:
        """Look up by FBst id ('FBst0041157') or 'CENTER:NUMBER' ('BDSC:1234')."""
        self.ensure_loaded()
        ident = identifier.strip()
        if FBST_RE.fullmatch(ident):
            return next((r for r in self._records if r.fbst_id == ident), None)
        code, number = parse_dbxref(ident)
        if code and number:
            return next(
                (r for r in self._records if r.center_code == code and r.stock_number == number),
                None,
            )
        return None

    def search_by_gene(
        self, gene: str, *, center: Optional[str] = None, limit: int = 25
    ) -> tuple[Optional[dict], list[StockRecord]]:
        """Resolve gene, then find stocks whose genotype mentions it.

        Returns (resolved_gene_or_None, matching_records). Matching is heuristic:
        we look for the canonical symbol and any synonyms as whole tokens inside the
        genotype field. Precise allele->gene->stock linkage would require the Chado
        relationship tables; this covers the common case well and is documented.
        """
        self.ensure_loaded()
        resolved = self.resolve_gene(gene)
        symbols: set[str] = {gene}
        if resolved:
            if resolved.get("symbol"):
                symbols.add(resolved["symbol"])
            for syn in resolved.get("synonyms", []) or []:
                symbols.add(syn)
        # Build word-boundary patterns; allele notation often appends [..] to symbols.
        patterns = [re.compile(rf"(?<![A-Za-z0-9]){re.escape(s)}(?![A-Za-z0-9])") for s in symbols if s]
        want_center = center.upper() if center else None
        hits: list[StockRecord] = []
        for rec in self._records:
            if want_center and rec.center_code != want_center:
                continue
            if any(p.search(rec.genotype) for p in patterns):
                hits.append(rec)
                if len(hits) >= limit:
                    break
        return resolved, hits

    # ------------------------------------------------------------ gene resolve
    def resolve_gene(self, query: str) -> Optional[dict]:
        """Resolve a gene symbol/synonym/ID to {id, symbol, name, synonyms, summary}.

        Symbol/synonym resolution uses FlyBase's bulk synonym file (there is no
        symbol-to-FBgn endpoint in the REST API -- verified against the published
        OpenAPI spec, see module docstring). The gene *summary* is fetched
        opportunistically from the REST API (``/gene/summaries/auto/{fbgn}``),
        which does exist and work. Cached in-process. Returns ``None`` only when
        the symbol/synonym can't be resolved at all; a missing summary (e.g.
        network down) still returns the symbol/name/synonyms from the bulk index.
        """
        q = query.strip()
        if q in self._gene_cache:
            return self._gene_cache[q]
        result: Optional[dict] = None
        try:
            if FBGN_RE.fullmatch(q):
                entry = self._synonym_entry_by_fbgn(q)
                result = entry or {"id": q, "symbol": None, "name": None, "synonyms": []}
            else:
                entry = self._synonym_lookup(q)
                if entry:
                    result = dict(entry)
        except Exception as exc:
            logger.warning("Gene synonym lookup failed for %r: %s", q, exc)
            result = None
        if result is not None:
            try:
                result["summary"] = self._gene_summary(result["id"])
            except Exception as exc:
                logger.warning("Gene summary fetch failed for %r: %s", result["id"], exc)
                result.setdefault("summary", None)
        self._gene_cache[q] = result
        return result

    # ---- bulk synonym index (symbol/synonym -> FBgn; offline, cached like stocks)
    def _ensure_synonym_index(self) -> dict[str, dict]:
        if self._synonym_index is not None:
            return self._synonym_index
        local = os.environ.get(ENV_SYNONYM_FILE)
        if local:
            path = Path(local).expanduser()
        else:
            path = _cache_dir() / "synonyms.tsv.gz"
            stale = not path.exists() or (time.time() - path.stat().st_mtime) > self._max_age_seconds()
            if stale:
                url = os.environ.get(ENV_SYNONYM_URL) or self._resolve_download_url(
                    _SYNONYMS_DIR_URL, _SYNONYM_FILENAME_RE
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                with _http_client(self._timeout) as client:
                    resp = client.get(url)
                    resp.raise_for_status()
                    path.write_bytes(resp.content)
        entries = []
        for line in self._open_text(path):
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 6:
                continue
            fbgn, organism, symbol, name, fullname_syns, symbol_syns = fields[:6]
            fbgn, symbol, name = fbgn.strip(), symbol.strip(), name.strip()
            # The file covers every species FlyBase tracks; the same bare symbol
            # (e.g. "Sxl") can be a synonym of a *different* species' ortholog
            # (verified live: "Sxl" alone is also a Dvir\Sxl synonym). This server
            # is scoped to D. melanogaster stocks, so only index Dmel genes.
            if organism.strip() != "Dmel" or not fbgn or not symbol:
                continue
            synonyms = sorted(
                {s.strip() for s in (fullname_syns + "|" + symbol_syns).split("|") if s.strip()}
            )
            entries.append({"id": fbgn, "symbol": symbol, "name": name or None, "synonyms": synonyms})
        # Two passes so a gene's *current* symbol always wins a lookup over some
        # other gene's *synonym* colliding with the same text.
        index: dict[str, dict] = {}
        for entry in entries:
            index.setdefault(entry["symbol"].lower(), entry)
        for entry in entries:
            for syn in entry["synonyms"]:
                index.setdefault(syn.lower(), entry)
        self._synonym_index = index
        return index

    def _synonym_lookup(self, symbol: str) -> Optional[dict]:
        return self._ensure_synonym_index().get(symbol.lower())

    def _synonym_entry_by_fbgn(self, fbgn: str) -> Optional[dict]:
        return next((e for e in self._ensure_synonym_index().values() if e["id"] == fbgn), None)

    # ---- REST API (only the endpoint that's actually real: summary-by-FBgn)
    def _http_json(self, url: str) -> Optional[dict]:
        with _http_client(self._timeout) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return None
            try:
                return resp.json()
            except Exception:
                return None

    def _gene_summary(self, fbgn: str) -> Optional[str]:
        data = self._http_json(f"{_FLYBASE_API_BASE}/gene/summaries/auto/{fbgn}")
        if isinstance(data, dict):
            resultset = data.get("resultset") or {}
            rows = resultset.get("result") or []
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                return rows[0].get("summary")
        return None
