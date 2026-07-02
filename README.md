# Drosophila Stocks MCP

An [MCP](https://modelcontextprotocol.io) server that lets AI assistants query
*Drosophila melanogaster* **stock centers** — Bloomington (BDSC), Kyoto/DGRC,
Vienna (VDRC), Korea (KDRC), NIG-FLY, FlyORF, and the National Drosophila
Species Stock Center (NDSSC) — by genotype or by gene, using
[FlyBase](https://flybase.org)'s freely redistributable data.

FlyBase already integrates the stock *records* (genotype, stock number, holding
center) for all of these collections. This server indexes that bulk data locally
for fast search and generates deep links into each center's own ordering system
for the parts FlyBase does not track (live availability, price, shipping).

## Tools

| Tool | What it does |
| --- | --- |
| `search_stocks_by_genotype` | Substring/token search over genotype strings, optional center filter |
| `search_stocks_by_gene` | Resolve a gene (symbol/synonym/FBgn) via FlyBase, then find stocks mentioning it |
| `get_stock` | Look up one stock by `FBst` ID or `CENTER:NUMBER` (e.g. `BDSC:1234`) |
| `list_stock_centers` | List supported centers with codes and homepages |
| `resolve_gene` | Resolve a gene symbol to a FlyBase gene record |
| `get_dataset_info` | Report the loaded FlyBase release, record count, and cache age |

Every stock result includes `center_code`, `stock_number`, `genotype`,
`flybase_url`, and a best-effort `order_url`.

## Install & run

Requires Python ≥ 3.10.

```bash
uvx drosophila-stocks-mcp          # stdio transport (Claude Desktop, Cursor, VS Code)
```

### Claude Desktop

Settings → Developer → Edit Config, then add:

```json
{
  "mcpServers": {
    "drosophila-stocks": {
      "command": "uvx",
      "args": ["drosophila-stocks-mcp@latest"],
      "env": { "UV_PYTHON": "3.12" }
    }
  }
}
```

### Remote / HTTP hosting (for claude.ai custom connectors)

claude.ai's web app only accepts **remote** MCP servers over HTTPS — it can't
launch a local `stdio` process the way Claude Desktop/Code can. To use this
server there, run it with the `streamable-http` transport somewhere publicly
reachable:

```bash
MCP_HOST=0.0.0.0 MCP_TRANSPORT=streamable-http uvx drosophila-stocks-mcp
```

A `Dockerfile` is included for deploying to any container platform (Google
Cloud Run, Koyeb, Render, a VPS, ...). It listens on `$PORT` if set (the
convention most of those platforms use), or `$MCP_PORT`/`8000` otherwise:

```bash
docker build -t drosophila-stocks-mcp .
docker run -p 8000:8000 -e PORT=8000 drosophila-stocks-mcp
```

Once it's live at a public HTTPS URL (e.g. `https://your-host/mcp`), add it in
claude.ai as a custom connector: **Settings → Connectors → Add custom connector**.

## Configuration

| Env var | Purpose | Default |
| --- | --- | --- |
| `FLYBASE_STOCKS_URL` | Override the bulk stock file URL (e.g. pin a release) | discovered from FlyBase's `current` release directory |
| `FLYBASE_STOCKS_FILE` | Use a local `.tsv`/`.tsv.gz` instead of downloading | — |
| `FLYBASE_SYNONYM_URL` | Override the bulk gene-synonym file URL | discovered from FlyBase's `current` release directory |
| `FLYBASE_SYNONYM_FILE` | Use a local synonym `.tsv`/`.tsv.gz` instead of downloading | — |
| `DROSOPHILA_STOCKS_CACHE` | Cache directory | `~/.cache/drosophila-stocks-mcp` |
| `DROSOPHILA_STOCKS_MAX_AGE_DAYS` | Re-download after this many days | `30` |
| `MCP_TRANSPORT` | `stdio`, `streamable-http`, or `sse` | `stdio` |

On first use the server downloads the FlyBase precomputed stock file, caches it,
and indexes it in memory. Subsequent runs use the cache until it ages out. If a
refresh download fails, a stale cache is used rather than erroring. FlyBase's
bulk files are release-stamped (e.g. `stocks_FB2026_02.tsv.gz`, no stable
`_current` filename), so the exact filename is discovered from the `current`
release's directory listing rather than guessed.

## Data sources & limitations

- **Stock records** come from FlyBase's bulk `stocks_FB<release>.tsv.gz` file
  (free for academic use; cite FlyBase).
- **Gene resolution** (symbol/synonym → `FBgn`) uses FlyBase's bulk
  `fb_synonym_fb_<release>.tsv.gz` file, restricted to *D. melanogaster* genes —
  FlyBase's REST API has no symbol-lookup endpoint. A gene *summary* is then
  fetched opportunistically from the REST API (`/gene/summaries/auto/{fbgn}`);
  `id`/`symbol`/`name`/`synonyms` always come from the bulk file regardless.
  As of this writing, that REST endpoint sits behind an AWS WAF bot challenge
  that returns an empty `202` to any non-browser client, so `summary` is
  currently always `None` in practice — this is handled gracefully (no error),
  not a bug in this server, and may resolve itself if FlyBase changes their
  WAF rules.
- `s3ftp.flybase.org` (the bulk-file host) reliably returns an empty `202` to
  plain HTTP/1.1 requests but a real `200` over HTTP/2, so this server requires
  `httpx[http2]` and always connects over HTTP/2.
- `search_stocks_by_gene` matches the gene's symbol/synonyms as whole tokens in
  the genotype string. This is precise for most alleles but can miss lines that
  carry a gene only inside an un-symboled construct; use `search_stocks_by_genotype`
  for exhaustive curation. Precise allele→gene→stock linkage via the Chado schema
  is a planned enhancement.
- **Availability/price/shipping** are *not* in FlyBase. The `order_url` deep-links
  you to the center's own system to check and order. Every deep-link template was
  verified against the live site's actual rendered content (not just a non-404
  status — see `tests/test_live.py`): BDSC, KYOTO, VDRC, and FLYORF all resolve
  to a real, correctly-parameterized search for the specific stock. VDRC's link
  is a storefront search (not a direct product page, since its stock numbers
  aren't catalog product IDs) that requires stripping the "v" prefix FlyBase
  stores; FlyORF's link goes through its separate KonaKart webshop (not
  flyorf.ch itself, which has no search), and only finds stocks that webshop's
  own catalog has indexed — some FlyBase-listed FlyORF stocks aren't in it,
  through no fault of the query. KDRC, NIG-FLY, and NDSSC have no direct-stock
  deep link and fall back to the center homepage (KDRC's is `http://`, not
  `https://` — its HTTPS vhost is misconfigured and serves a server error).

## Development

```bash
uv venv && source .venv/bin/activate
uv sync
pre-commit install
pytest          # runs fully offline against tests/fixtures/
```

## Citation

If you use this in research, please cite **FlyBase** and the specific **stock
center(s)** you order from, in addition to this tool.

## License

MIT. Stock data © FlyBase and the respective stock centers, used under their
academic-use terms.
