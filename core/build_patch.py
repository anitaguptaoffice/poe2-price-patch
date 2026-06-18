#!/usr/bin/env python3
import argparse
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import TemporaryDirectory


TARGET_MARKERS = [
    "Metadata/Items/Currency/CurrencyRerollRare",
    "Metadata/Items/Currency/CurrencyModValues",
    "Metadata/Items/Currency/CurrencyDuplicate",
]


def fail(message):
    raise SystemExit("ERROR: " + message)


def run_ooz(args):
    bundled_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    candidates = [
        bundled_dir / ("ooz.exe" if os.name == "nt" else "ooz"),
        Path(sys.executable).parent / ("ooz.exe" if os.name == "nt" else "ooz"),
        Path("ooz.exe" if os.name == "nt" else "ooz"),
    ]
    ooz = next((str(path) for path in candidates if path.exists()), "ooz")
    result = subprocess.run([ooz] + args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace"))


def install_pypoe_decompress_patch(bundle_module):
    def fixed_decompress(self, start=0, end=None):
        if end is None:
            end = self.entry_count

        last = self.entry_count - 1
        with TemporaryDirectory() as tempdir:
            for i in range(start, end):
                stem = Path(tempdir) / ("chunk%s" % i)
                if i != last:
                    size = self.chunk_size
                else:
                    size = self.size_decompressed % self.chunk_size or self.chunk_size

                stem.with_suffix(".in").write_bytes(struct.pack("<Q", size) + self.data[i])
                run_ooz(["-d", str(stem.with_suffix(".in")), str(stem.with_suffix(".out"))])
                self.data[i] = stem.with_suffix(".out").read_bytes()

        self.data = b"".join(self.data.values())

    bundle_module.Bundle.decompress = fixed_decompress


def compress_raw_chunk(raw, compressor, oodle_dll=None, level=4):
    with TemporaryDirectory() as tempdir:
        src = Path(tempdir) / "chunk.raw"
        dst = Path(tempdir) / "chunk.ooz"
        src.write_bytes(raw)
        if oodle_dll:
            tool = Path(__file__).parent / "tools" / "oodle_compress.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(tool),
                    "--dll",
                    str(oodle_dll),
                    "--compressor",
                    compressor,
                    "--level",
                    str(level),
                    str(src),
                    str(dst),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.decode(errors="replace"))
        else:
            run_ooz(["-z", "--" + compressor, str(src), str(dst)])
        compressed = dst.read_bytes()

    if struct.unpack_from("<Q", compressed, 0)[0] != len(raw):
        fail("ooz compressed size prefix does not match chunk size")
    return compressed[8:]


def build_bundle(raw_data, output_path, encoder=9, chunk_size=262144, oodle_dll=None, level=4):
    compressor_by_encoder = {
        9: "mermaid",
        12: "hydra",
    }
    compressor = compressor_by_encoder.get(encoder)
    if compressor is None:
        fail("unsupported bundle encoder: %s" % encoder)

    chunks = []
    for start in range(0, len(raw_data), chunk_size):
        chunks.append(
            compress_raw_chunk(
                raw_data[start : start + chunk_size],
                compressor,
                oodle_dll=oodle_dll,
                level=level,
            )
        )

    entry_count = len(chunks)
    data_size = sum(len(chunk) for chunk in chunks)
    head_size = 12 + 48 + entry_count * 4

    header = bytearray()
    header += struct.pack("<III", len(raw_data), data_size, head_size)
    header += struct.pack(
        "<IIQQIIIIII",
        encoder,
        0,
        len(raw_data),
        data_size,
        entry_count,
        chunk_size,
        0,
        0,
        0,
        0,
    )
    header += struct.pack("<%sI" % entry_count, *(len(chunk) for chunk in chunks))
    output_path.write_bytes(bytes(header) + b"".join(chunks))


def read_utf16_string(blob, offset):
    if offset < 0 or offset >= len(blob):
        return None
    end = blob.find(b"\x00\x00\x00\x00", offset)
    while end >= 0 and (end - offset) % 2:
        end = blob.find(b"\x00\x00\x00\x00", end + 1)
    if end < 0:
        return None
    try:
        return blob[offset:end].decode("utf-16le")
    except UnicodeDecodeError:
        return None


def append_utf16_string(blob, data_base, text):
    rel = len(blob) - data_base
    blob.extend(text.encode("utf-16le"))
    blob.extend(b"\x00\x00\x00\x00")
    return rel


def parse_name_table(blob):
    rows = struct.unpack_from("<I", blob, 0)[0]
    magic = blob.find(b"\xbb" * 8)
    if magic < 0:
        fail("name table magic not found")
    table_len = magic - 4
    if rows <= 0 or table_len % rows != 0:
        fail("name table row layout is not understood")
    return rows, table_len // rows, magic


def patch_name_table(raw_table, replacements):
    blob = bytearray(raw_table)
    rows, record_len, data_base = parse_name_table(blob)
    patched = []
    pending = dict(replacements)

    for row in range(rows):
        row_offset = 4 + row * record_len
        key_rel = struct.unpack_from("<Q", blob, row_offset)[0]
        key = read_utf16_string(blob, data_base + key_rel)
        if key not in pending:
            continue

        old_rel = struct.unpack_from("<Q", blob, row_offset + 32)[0]
        old_value = read_utf16_string(blob, data_base + old_rel)
        new_value = pending.pop(key)
        new_rel = append_utf16_string(blob, data_base, new_value)
        struct.pack_into("<Q", blob, row_offset + 32, new_rel)
        patched.append(
            {
                "row": row,
                "resource_id": key,
                "old_value": old_value,
                "new_value": new_value,
                "old_rel": old_rel,
                "new_rel": new_rel,
            }
        )

    if pending:
        fail("resource ids not found in name table: " + ", ".join(sorted(pending)))

    return bytes(blob), patched


def load_replacements(resource_map_path, prices_path):
    resource_map = json.loads(Path(resource_map_path).read_text(encoding="utf-8"))
    prices = json.loads(Path(prices_path).read_text(encoding="utf-8"))
    replacements = {}

    for price_name, price in prices.items():
        if price_name not in resource_map:
            continue
        item = resource_map[price_name]
        replacements[item["resource_id"]] = item["template"].format(price=price)

    if not replacements:
        fail("no replacements produced; check prices and resource_map")
    return replacements


def normalize_display_name(name):
    name = name.strip()
    name = re.sub(r"^[◆◇★☆✿♥♡♠♣♦●○◎※\s]+", "", name)
    name = re.sub(r"[◆◇★☆✿♥♡♠♣♦●○◎※\s]+$", "", name)
    return name.strip()


def extract_resource_map_from_name_table(raw_table):
    rows, record_len, data_base = parse_name_table(raw_table)
    generated = {}
    collisions = {}

    for row in range(rows):
        row_offset = 4 + row * record_len
        key_rel = struct.unpack_from("<Q", raw_table, row_offset)[0]
        value_rel = struct.unpack_from("<Q", raw_table, row_offset + 32)[0]
        key = read_utf16_string(raw_table, data_base + key_rel)
        value = read_utf16_string(raw_table, data_base + value_rel)
        if not key or not value:
            continue
        if not key.startswith("Metadata/Items/"):
            continue

        display_name = normalize_display_name(value)
        if not display_name:
            continue
        if display_name in generated and generated[display_name]["resource_id"] != key:
            collisions.setdefault(display_name, []).append(key)
            continue
        generated[display_name] = {
            "resource_id": key,
            "template": "%s {price}" % display_name,
            "source_value": value,
            "row": row,
        }

    return generated, collisions


def api_get_json(api_base, path, params=None):
    url = api_base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "poe2-price-patcher/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def format_price_label(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value >= 10000:
        text = "%.1fw" % (value / 10000.0)
        return text.replace(".0w", "w")
    if value < 1:
        return ("%.2fc" % value).rstrip("0").replace(".c", "c")
    if value == int(value):
        return "%dc" % int(value)
    return ("%.1fc" % value).replace(".0c", "c")


def pick_latest_price(rows, field):
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: r.get("datetime", ""))
    latest = rows[-1]
    label = format_price_label(latest.get(field))
    if label is None:
        return None
    return {
        "label": label,
        "raw": latest,
    }


def fetch_prices_from_api(api_base, hours, version, season, price_field, generated_map, explicit_names=None, chaos_price_label="1c", workers=8, bulk_limit=10000):
    fetched = {}
    skipped_unmapped = []
    no_price = []
    matched = []
    wanted = set(explicit_names or [])

    price_params = {"limit": bulk_limit}
    if version:
        price_params["version"] = version
    if season:
        price_params["season"] = season
    price_rows = api_get_json(api_base, "/api/db/price", price_params)
    price_rows_by_item = {}
    for row in price_rows:
        if not isinstance(row, dict):
            continue
        key = (row.get("item_name"), row.get("category_label"))
        if not key[0] or not key[1]:
            continue
        price_rows_by_item.setdefault(key, []).append(row)

    seen_unmapped = set()
    seen_price_names = set()
    for (item_name, category_label), rows in price_rows_by_item.items():
        if wanted and item_name not in wanted:
            continue
        seen_price_names.add(item_name)
        if item_name not in generated_map:
            unmapped_key = (item_name, category_label)
            if unmapped_key not in seen_unmapped:
                skipped_unmapped.append({"item_name": item_name, "category_label": category_label})
                seen_unmapped.add(unmapped_key)
            continue

        rows = price_rows_by_item.get((item_name, category_label), [])
        price = pick_latest_price(rows, price_field)
        if not price:
            if item_name == "混沌石" and chaos_price_label:
                fetched[item_name] = chaos_price_label
                matched.append({
                    "item_name": item_name,
                    "category_label": category_label,
                    "resource_id": generated_map[item_name]["resource_id"],
                    "price": chaos_price_label,
                    "datetime": None,
                    "field": "fixed",
                })
                continue
            no_price.append({"item_name": item_name, "category_label": category_label})
            continue

        fetched[item_name] = price["label"]
        matched.append(
            {
                "item_name": item_name,
                "category_label": category_label,
                "resource_id": generated_map[item_name]["resource_id"],
                "price": price["label"],
                "datetime": price["raw"].get("datetime"),
                "field": price_field,
            }
        )

    for item_name in sorted(wanted - seen_price_names):
        no_price.append({"item_name": item_name})

    return fetched, matched, skipped_unmapped, no_price, {
        "mode": "bulk",
        "price_rows": len(price_rows),
        "bulk_limit": bulk_limit,
        "requests": 1,
    }


def build_replacements_from_prices(prices, resource_maps):
    replacements = {}
    missing_map = []
    for price_name, price in prices.items():
        item = None
        for resource_map in resource_maps:
            item = resource_map.get(price_name)
            if item:
                break
        if not item:
            missing_map.append(price_name)
            continue
        replacements[item["resource_id"]] = item.get("template", "%s {price}" % price_name).format(price=price)
    return replacements, missing_map


def locate_name_table(index, bundle_module, bundles2):
    markers = [marker.encode("utf-16le") for marker in TARGET_MARKERS]
    for fr in index.files.values():
        if fr.file_size < 100000:
            continue
        bundle_path = bundles2 / (fr.bundle.file_name)
        if not bundle_path.exists():
            continue

        if fr.bundle.contents is None:
            bundle = bundle_module.Bundle()
            bundle.read(str(bundle_path))
            bundle.decompress()
            fr.bundle.contents = bundle

        raw = fr.get_file()
        if all(marker in raw for marker in markers):
            return fr, raw

    fail("could not locate item name table")


def parse_index_raw(index_raw):
    bundle_count = struct.unpack_from("<I", index_raw, 0)[0]
    offset = 4
    bundle_records = []
    for bundle_id in range(bundle_count):
        name_len = struct.unpack_from("<I", index_raw, offset)[0]
        name = index_raw[offset + 4 : offset + 4 + name_len].decode()
        size = struct.unpack_from("<I", index_raw, offset + 4 + name_len)[0]
        raw = index_raw[offset : offset + name_len + 8]
        bundle_records.append({"id": bundle_id, "name": name, "size": size, "raw": raw})
        offset += name_len + 8

    file_count_offset = offset
    file_count = struct.unpack_from("<I", index_raw, offset)[0]
    offset += 4
    file_records_offset = offset
    file_records_size = file_count * 20
    directory_section_offset = file_records_offset + file_records_size
    return {
        "bundle_records": bundle_records,
        "file_count_offset": file_count_offset,
        "file_count": file_count,
        "file_records_offset": file_records_offset,
        "file_records_size": file_records_size,
        "directory_section_offset": directory_section_offset,
    }


def rewrite_index_raw(index_raw, target_hash, price_bundle_name, price_bundle_size):
    parsed = parse_index_raw(index_raw)
    bundle_records = parsed["bundle_records"]
    existing = [b for b in bundle_records if b["name"] == price_bundle_name]
    if existing:
        price_bundle_id = existing[0]["id"]
        new_bundle_records = []
        for record in bundle_records:
            if record["id"] == price_bundle_id:
                name_bytes = price_bundle_name.encode()
                new_bundle_records.append(struct.pack("<I", len(name_bytes)) + name_bytes + struct.pack("<I", price_bundle_size))
            else:
                new_bundle_records.append(record["raw"])
    else:
        price_bundle_id = len(bundle_records)
        name_bytes = price_bundle_name.encode()
        new_bundle_records = [record["raw"] for record in bundle_records]
        new_bundle_records.append(struct.pack("<I", len(name_bytes)) + name_bytes + struct.pack("<I", price_bundle_size))

    file_records = bytearray(
        index_raw[
            parsed["file_records_offset"] : parsed["file_records_offset"] + parsed["file_records_size"]
        ]
    )
    found = False
    for i in range(parsed["file_count"]):
        off = i * 20
        file_hash = struct.unpack_from("<Q", file_records, off)[0]
        if file_hash == target_hash:
            struct.pack_into("<III", file_records, off + 8, price_bundle_id, 0, price_bundle_size)
            found = True
            break
    if not found:
        fail("target file hash not found in index: %s" % target_hash)

    rebuilt = bytearray()
    rebuilt += struct.pack("<I", len(new_bundle_records))
    for record in new_bundle_records:
        rebuilt += record
    rebuilt += struct.pack("<I", parsed["file_count"])
    rebuilt += file_records
    rebuilt += index_raw[parsed["directory_section_offset"] :]
    return bytes(rebuilt), price_bundle_id


def main():
    parser = argparse.ArgumentParser(description="Build a POE2 item display-name price patch.")
    parser.add_argument("--bundles2", required=True, help="Path to Bundles2 directory")
    parser.add_argument("--prices", default=None, help="JSON mapping source item name to price label")
    parser.add_argument("--fetch-prices", action="store_true", help="Fetch prices from poecurrency.top")
    parser.add_argument("--api-base", default="https://poecurrency.top")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--version", type=int, default=2, help="POE version parameter for poecurrency.top")
    parser.add_argument("--season", default=None)
    parser.add_argument("--price-field", default="sell1", choices=["sell1", "buy1", "sell2", "buy2"])
    parser.add_argument("--chaos-price-label", default="1c")
    parser.add_argument("--fetch-workers", type=int, default=8, help="Deprecated; bulk price fetch does not use workers")
    parser.add_argument("--bulk-price-limit", type=int, default=10000)
    parser.add_argument("--resource-map", default=None, help="JSON mapping source item name to resource id")
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument("--price-bundle-name", default="PricePatch")
    parser.add_argument("--bundle-encoder", type=int, default=9, choices=[9, 12])
    parser.add_argument("--oodle-dll", default=None, help="Path to oo2core DLL for Oodle compression")
    parser.add_argument("--oodle-level", type=int, default=4)
    args = parser.parse_args()
    if not args.prices and not args.fetch_prices:
        fail("provide --prices or --fetch-prices")

    platform.system = lambda: "Linux"
    os.environ["HOME"] = os.environ.get("HOME") or os.path.expanduser("~") or os.environ.get("USERPROFILE", str(Path.home()))
    from PyPoE.poe.file import bundle as bm

    install_pypoe_decompress_patch(bm)

    bundles2 = Path(args.bundles2)
    out = Path(args.out)
    out_bundles = out / "Bundles2"
    out_bundles.mkdir(parents=True, exist_ok=True)

    index = bm.Index()
    index.read(str(bundles2 / "_.index.bin"))
    name_record, name_table = locate_name_table(index, bm, bundles2)

    generated_map, map_collisions = extract_resource_map_from_name_table(name_table)
    explicit_map = {}
    if args.resource_map:
        explicit_map = json.loads(Path(args.resource_map).read_text(encoding="utf-8"))
    else:
        default_map = Path(__file__).with_name("resource_map.json")
        if default_map.exists():
            explicit_map = json.loads(default_map.read_text(encoding="utf-8"))

    fetched_matches = []
    fetch_stats = None
    skipped_unmapped = []
    no_price = []
    prices = {}
    if args.prices:
        prices.update(json.loads(Path(args.prices).read_text(encoding="utf-8")))
    if args.fetch_prices:
        explicit_names = set(explicit_map.keys()) if args.resource_map and explicit_map else None
        fetched, fetched_matches, skipped_unmapped, no_price, fetch_stats = fetch_prices_from_api(
            args.api_base,
            args.hours,
            args.version,
            args.season,
            args.price_field,
            {**generated_map, **explicit_map},
            explicit_names=explicit_names,
            chaos_price_label=args.chaos_price_label,
            workers=args.fetch_workers,
            bulk_limit=args.bulk_price_limit,
        )
        prices.update(fetched)

    replacements, missing_map = build_replacements_from_prices(prices, [explicit_map, generated_map])
    if not replacements:
        fail("no replacements produced")

    patched_table, patched_rows = patch_name_table(name_table, replacements)

    price_bundle_path = out_bundles / (args.price_bundle_name + ".bundle.bin")
    build_bundle(
        patched_table,
        price_bundle_path,
        encoder=args.bundle_encoder,
        oodle_dll=args.oodle_dll,
        level=args.oodle_level,
    )

    patched_index_raw, price_bundle_id = rewrite_index_raw(
        index.data,
        name_record.hash,
        args.price_bundle_name,
        len(patched_table),
    )
    build_bundle(
        patched_index_raw,
        out_bundles / "_.index.bin",
        encoder=args.bundle_encoder,
        oodle_dll=args.oodle_dll,
        level=args.oodle_level,
    )

    report = {
        "name_table": {
            "file_hash": name_record.hash,
            "source_bundle": name_record.bundle.name,
            "source_offset": name_record.file_offset,
            "source_size": name_record.file_size,
            "patched_size": len(patched_table),
        },
        "price_bundle": {
            "name": args.price_bundle_name,
            "bundle_id": price_bundle_id,
            "path": "Bundles2/%s.bundle.bin" % args.price_bundle_name,
        },
        "patched_rows": patched_rows,
        "price_source": {
            "fetch_prices": args.fetch_prices,
            "api_base": args.api_base,
            "hours": args.hours,
            "version": args.version,
            "season": args.season,
            "price_field": args.price_field,
            "chaos_price_label": args.chaos_price_label,
            "bulk_price_limit": args.bulk_price_limit,
            "fetch_stats": fetch_stats,
            "local_prices": args.prices,
        },
        "fetched_matches": fetched_matches,
        "missing_resource_map_names": missing_map,
        "unmapped_api_items": skipped_unmapped,
        "api_items_without_price": no_price,
        "resource_map_collisions": map_collisions,
    }
    (out / "patch-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "generated-resource-map.json").write_text(json.dumps(generated_map, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "unmapped-items.json").write_text(
        json.dumps(
            {
                "missing_resource_map_names": missing_map,
                "unmapped_api_items": skipped_unmapped,
                "api_items_without_price": no_price,
                "resource_map_collisions": map_collisions,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    zip_path = out / "poe2-price-name-patch.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.write(out_bundles / "_.index.bin", "Bundles2/_.index.bin")
        zf.write(price_bundle_path, "Bundles2/%s.bundle.bin" % args.price_bundle_name)
        zf.write(out / "patch-report.json", "patch-report.json")
        zf.write(out / "generated-resource-map.json", "generated-resource-map.json")
        zf.write(out / "unmapped-items.json", "unmapped-items.json")

    print("Wrote:", zip_path)
    print("Patched rows:", len(patched_rows))
    for row in patched_rows:
        print("%s: %s -> %s" % (row["resource_id"], row["old_value"], row["new_value"]))


if __name__ == "__main__":
    main()
