# poe2-price-patch

POE2 item display-name price patch builder.

## What it does

- Fetches currency prices from `poecurrency.top`.
- Reads the fixed Simplified Chinese item-name table from `Bundles2`:
  `data/balance/simplified chinese/baseitemtypes.datc64`
  (`12566211446770480856`).
- Appends price labels to matched item names.
- Writes a patch zip containing a rewritten `_.index.bin` and `PricePatch.bundle.bin`.

Remote price fetching uses one API request by default:

1. `/api/db/price?limit=10000&version=2`

The tool then groups rows locally by `item_name` and `category_label`, and uses the latest `datetime` row for each POE2 item.

The generated patch is created locally from your own `Bundles2` files. The repository and GitHub Actions do not contain game assets.

## CLI usage

```bash
poe2-price-patcher \
  --bundles2 "D:/Path/To/Bundles2" \
  --fetch-prices \
  --hours 24 \
  --price-field sell1 \
  --bundle-encoder 12 \
  --oodle-dll "D:/Path/To/Path of Exile 2/oo2core.dll" \
  --out out
```

Local price file:

```bash
poe2-price-patcher \
  --bundles2 "D:/Path/To/Bundles2" \
  --prices prices.example.json \
  --bundle-encoder 12 \
  --oodle-dll "D:/Path/To/Path of Exile 2/oo2core.dll" \
  --out out
```

Output:

- `out/poe2-price-name-patch.zip`
- `out/patch-report.json`

Install the zip by copying its `Bundles2/_.index.bin` and
`Bundles2/PricePatch.bundle.bin` over the game's `Bundles2` directory.

## GitHub Actions release flow

`.github/workflows/release.yml` runs every 10 minutes.

1. `scripts/check_price_version.py` requests recent POE2 price rows with `hours=24&version=2`.
2. It parses the latest `datetime` as Asia/Shanghai time.
3. The tag is generated as `price-YYYYMMDD-HHMM`, for example `price-20260618-1800`.
4. If that release already exists, the workflow exits.
5. If it is new, Actions builds standalone GUI/CLI packages and publishes a Release.
