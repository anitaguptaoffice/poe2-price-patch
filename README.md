# poe2-price-patch

POE2 item display-name price patch builder.

## What it does

- Fetches currency prices from `poecurrency.top`.
- Locates the item display-name table from a local `Bundles2` directory.
- Appends price labels to matched item names.
- Writes a patch zip containing a rewritten `_.index.bin` and `PricePatch.bundle.bin`.

Remote price fetching uses two API requests by default:

1. `/api/db/currencies`
2. `/api/db/price?limit=10000`

The tool then groups rows locally by `item_name` and `category_label`, and uses the latest `datetime` row for each item.

The generated patch is created locally from your own `Bundles2` files. The repository and GitHub Actions do not contain game assets.

## CLI usage

```bash
poe2-price-patcher \
  --bundles2 "D:/Path/To/Bundles2" \
  --fetch-prices \
  --hours 24 \
  --price-field sell1 \
  --out out
```

Local price file:

```bash
poe2-price-patcher \
  --bundles2 "D:/Path/To/Bundles2" \
  --prices prices.example.json \
  --out out
```

Output:

- `out/poe2-price-name-patch.zip`
- `out/patch-report.json`
- `out/generated-resource-map.json`
- `out/unmapped-items.json`

## GitHub Actions release flow

`.github/workflows/release.yml` runs every 10 minutes.

1. `scripts/check_price_version.py` requests recent price rows with `hours=24`.
2. It parses the latest `datetime` as Asia/Shanghai time.
3. The tag is generated as `price-YYYYMMDD-HHMM`, for example `price-20260618-1800`.
4. If that release already exists, the workflow exits.
5. If it is new, Actions builds standalone CLI binaries for Windows and macOS and publishes a Release.

## Current limitation

The index rewrite currently emits a Mermaid-compressed `_.index.bin`. PyPoE can parse it, but the game client compatibility still needs real-client verification because the official index observed in the reference patch used Hydra compression.
