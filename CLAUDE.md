# CLAUDE.md — Testing & Verification Handoff

Context for Claude Code to **extensively test and harden `drosophila-stocks-mcp`
before it is published to PyPI and submitted to the BioContextAI registry.**

## What this project is

An MCP server that lets an LLM query *Drosophila melanogaster* stock centers
(BDSC, Kyoto/DGRC, VDRC, KDRC, NIG-FLY, FlyORF, THFC) by genotype or gene. Data
comes from FlyBase's freely redistributable **bulk precomputed stock file**
(the authoritative list of stocks + genotypes across all centers), plus FlyBase's
**REST API** for resolving gene symbols to `FBgn` IDs. It generates deep links
into each center's own ordering system for live availability/price (which FlyBase
does not track).

### Layout
```
src/drosophila_stocks_mcp/
  centers.py   # StockCenter registry, alias resolution, order-URL builders
  models.py    # StockRecord dataclass, parse_dbxref()
  flybase.py   # FlyBaseClient: download/cache/parse bulk file + gene resolution
  server.py    # FastMCP server; 6 tools
tests/
  test_core.py            # 33 offline tests (pass against the fixture)
  fixtures/sample_stocks.tsv
```

### Six tools
`search_stocks_by_genotype`, `search_stocks_by_gene`, `get_stock`,
`list_stock_centers`, `resolve_gene`, `get_dataset_info`.

### Run it
```bash
uv venv && source .venv/bin/activate && uv sync   # or: pip install -e ".[dev]"
pytest -q                                          # offline suite must stay green
FLYBASE_STOCKS_FILE=tests/fixtures/sample_stocks.tsv python -m drosophila_stocks_mcp
```

Env knobs: `FLYBASE_STOCKS_URL`, `FLYBASE_STOCKS_FILE`, `DROSOPHILA_STOCKS_CACHE`,
`DROSOPHILA_STOCKS_MAX_AGE_DAYS`, `MCP_TRANSPORT`, `LOG_LEVEL`.

---

## ⚠️ CRITICAL: what was built WITHOUT live verification

The original author's sandbox could not reach `flybase.org` / `s3ftp.flybase.org`
/ `api.flybase.org`. The offline logic is tested, but **four things are educated
guesses that you must verify against the live services and fix if wrong.** Treat
these as the primary testing objectives.

### 1. The bulk stock file URL and name — `flybase.py::_DEFAULT_STOCKS_URL`
Current guess:
`http://s3ftp.flybase.org/releases/current/precomputed_files/stocks/stocks_FB_current.tsv.gz`

- Browse the real listing: `http://s3ftp.flybase.org/releases/` and a specific
  release e.g. `.../releases/FB2026_01/precomputed_files/stocks/`.
- Confirm the **exact filename** (it may be `stocks_FB2026_01.tsv.gz`, not
  `stocks_FB_current`), and whether a `current` alias/symlink exists.
- Decide whether to default to `current` or resolve the latest release explicitly
  (list releases, pick highest `FByyyy_nn`). Prefer explicit resolution so
  `get_dataset_info().release_hint` is always populated.
- Check http vs https and redirect behavior.

### 2. The stock file's real column schema — `flybase.py::_index_columns` / `_row_to_record`
The parser detects columns by fuzzy header matching and falls back to content
inference. **Download the real file, print the header line and first ~5 data
rows, and confirm the mapping is correct.** In particular:
- What are the real column names? (Historically ~3 cols: stock id, dbxref,
  genotype — but verify; layouts have shifted between releases.)
- Are there `#` comment/header lines? Does the header start with `#`?
- Is the genotype the FlyBase genotype, the "stock list description", or both?
- Confirm `_looks_like_header()` triggers on the real header.

### 3. The center encoding inside `dbxref` — `models.py::parse_dbxref`
`parse_dbxref` assumes forms like `BDSC:1234`, `Bloomington_5678`, `Kyoto 200300`.
**Verify against real rows.** The real file may encode the center in a separate
column, or use different tokens/prefixes than my `centers.py` aliases. Add any
missing aliases to `STOCK_CENTERS[*].aliases`. Check every center appears:
```python
from collections import Counter
Counter(r.center_code for r in client.records)  # expect BDSC, KYOTO, VDRC, ... ; watch for None
```
A high `None` count means the dbxref parsing/aliases are wrong.

### 4. The FlyBase REST API gene endpoints — `flybase.py::_symbol_to_fbgn` / `_gene_report`
These are the **most speculative** part. I guessed:
- base `https://api.flybase.org/api/v1.0`
- `/gene/{symbol}` for symbol→FBgn
- `/gene/summaries/auto/{fbgn}` for a summary

**Verify against the real API docs** before trusting these:
- Swagger UI: `https://flybase.github.io/api/swagger-ui/`
- OpenAPI JSON: `https://api.swaggerhub.com/apis/FlyBase/FlyBase/1.0`

Find the correct endpoints for (a) validating/resolving a symbol or synonym to a
current `FBgn`, and (b) a gene summary. Rewrite `_symbol_to_fbgn`/`_gene_report`
to match the **actual response JSON shapes**, and populate `symbol`, `name`, and
`synonyms` properly (right now `_gene_report` returns them empty, which weakens
`search_stocks_by_gene` synonym matching). FlyBase rate limit is **3 req/sec** —
stay under it; add a small delay if you batch.

### 5. Order-URL templates — `centers.py::STOCK_CENTERS[*].order_url_template`
Each center's deep-link template is a guess. **Verify each resolves to the right
stock (HTTP 200, correct page)**, especially:
- **VDRC** (currently a Magento `product/view/id/{num}` path) — almost certainly
  wrong; VDRC uses catalog/transformant IDs, not Magento product IDs. Find the
  real query pattern.
- **BDSC** `Home/Search?presearch={num}`, **Kyoto** `DB_NUM={num}`, **FlyORF**
  `?s={num}` — confirm or fix.
- For centers with `order_url_template=None` (KDRC, NIG, THFC), see if a stable
  deep-link pattern exists and add it; otherwise leave as homepage fallback.

---

## Ground-truth data (verified real, use as test oracles)

- **`FBst0041157`** = BDSC stock **41157**, genotype
  `w[1118]; P{y[+t7.7] w[+mC]=UAS-LUC-mir-932.T}attP2`
  (FlyBase genotype: `w1118; P{UAS-LUC-mir-932.T}attP2`). Living stock, D. melanogaster.
- `FBst0009528` is a valid FlyBase stock report ID.
- FlyBase current release at handoff time: **FB2026_01** (released 2026-03-12).
- Pick a well-known gene as an oracle, e.g. `Sxl`, and confirm its real `FBgn`
  via the API rather than hardcoding — then assert the resolver returns it.

---

## Test plan to implement

Keep the existing offline suite green. Add a **live, opt-in** suite so CI stays
hermetic.

### A. Register a `live` marker and gate network tests
In `pyproject.toml`:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["live: hits real FlyBase/stock-center network (deselected by default)"]
addopts = "-m 'not live'"
```
Run live with `pytest -m live`.

### B. `tests/test_live.py` (marked `@pytest.mark.live`)
1. **Download + parse the real bulk file** (no `FLYBASE_STOCKS_FILE` set): assert
   record count is plausible (FlyBase has ~80k+ stocks), every record has a
   genotype, most have an `FBst` id, and `Counter(center_code)` includes BDSC,
   KYOTO, VDRC with a low `None` fraction (set a threshold, e.g. <5%).
2. **Known-stock lookups**: `get_stock("FBst0041157")` returns BDSC/41157 with the
   mir-932 genotype; `get_stock("BDSC:41157")` returns the same record.
3. **Gene resolution**: `resolve_gene("Sxl")` returns a valid `FBgn`; a bogus
   symbol returns `None` without raising.
4. **Gene search**: `search_stocks_by_gene("Sxl")` returns >0 stocks whose
   genotype contains `Sxl`.
5. **Order URLs resolve**: for one real stock per center, `httpx.get(order_url,
   follow_redirects=True)` is not 404 (allow 200/redirect; some sites block HEAD).
6. **`get_dataset_info()`** reports `source="download"`, a non-null `release_hint`,
   and a sane `record_count`.

### C. Cache-behavior tests (offline, use fixture + monkeypatched download)
- First call downloads (mock `_download_to_cache` to copy the fixture); second
  call loads from cache without re-downloading.
- Stale cache (`DROSOPHILA_STOCKS_MAX_AGE_DAYS=0`, old mtime) triggers refresh.
- Download failure with an existing cache falls back to `source="stale-cache"`
  (mock `_download_to_cache` to raise; pre-seed cache).
- No cache + download failure re-raises.

### D. MCP protocol tests
- Start under **stdio** and list tools via an MCP client (e.g. the `mcp` SDK's
  client session, or the FastMCP in-memory client). Assert 6 tools with correct
  names + non-empty descriptions + input schemas.
- Start under **streamable-http** (`MCP_TRANSPORT=streamable-http`) on a test port
  and confirm it serves `/mcp` and lists tools.
- Manual smoke test with the Inspector:
  `npx @modelcontextprotocol/inspector uvx drosophila-stocks-mcp`.

### E. Robustness / edge cases (offline)
- Malformed rows: too few columns, empty genotype, blank lines, trailing tabs,
  CRLF line endings, non-UTF-8 bytes in the file → parser skips gracefully, no crash.
- `.tsv` vs `.tsv.gz` (magic-byte sniffing in `_open_text`) both load.
- Unicode/special chars in genotypes survive round-trip to `to_dict()`.
- `search_stocks_by_genotype` with empty query, whitespace query, `limit=0`,
  `limit=99999`; verify clamping (1–200) and token-AND semantics.
- Unknown center filter (`center="NOPE"`) returns 0, not an error.
- `get_stock` with garbage identifier returns `found=False`.

### F. Performance / footprint (live or against a downloaded file)
- Time the parse of the full file; note peak memory of the in-memory index.
- If parse is slow or memory-heavy at ~80k+ records, consider streaming the parse
  and/or building a lowercased genotype index once instead of lowercasing per query
  in `search_by_genotype` (current code lowercases every genotype on every call —
  worth optimizing with a precomputed index).

### G. `search_stocks_by_gene` accuracy characterization
Document (and test) its known heuristic limits: it token-matches the gene
symbol/synonyms in the genotype string. Once `_gene_report` returns real synonyms,
re-check recall. Note false-negative case (gene present only via un-symboled
construct) in the README. Consider adding the Chado allele→gene→stock path later.

---

## Definition of done (publish checklist)

- [ ] Offline suite green; new live suite green against real FlyBase.
- [ ] Bulk file URL/name verified; release resolution populates `release_hint`.
- [ ] Column mapping + `parse_dbxref` verified on real rows; `None` center rate low.
- [ ] Gene API endpoints verified against swagger; `symbol`/`name`/`synonyms` filled.
- [ ] All order-URL templates verified to resolve (VDRC fixed).
- [ ] Cache download/refresh/stale-fallback paths tested.
- [ ] MCP stdio + streamable-http both list 6 tools; Inspector smoke test done.
- [ ] Edge/robustness tests pass; parse performance acceptable at full scale.
- [ ] README limitations section matches actual behavior.
- [ ] Replace placeholders: `YOUR_GH_USER`, author name/email in `pyproject.toml`
      and `meta.yaml`.
- [ ] `python -m build` produces wheel+sdist; `twine check dist/*` clean.
- [ ] PyPI trusted publishing configured; tag a release (CI `release.yml` publishes).
- [ ] Fork `github.com/biocontext-ai/registry`, add `meta.yaml` under `servers/`,
      run their pre-commit schema hook, open PR. Validate/generate via
      `https://biocontext.ai/registry/editor`.

## Useful references
- FlyBase dev docs / API: https://flybase.github.io/
- Bulk releases: http://s3ftp.flybase.org/releases/
- BioContextAI registry repo: https://github.com/biocontext-ai/registry
- MCP Inspector: https://github.com/modelcontextprotocol/inspector
