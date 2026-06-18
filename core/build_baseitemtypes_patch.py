#!/usr/bin/env python3
import argparse
import json
import os
import platform
import struct
import zipfile
from pathlib import Path

import build_patch as core


BASEITEMTYPES_HASH = 12566211446770480856
BASEITEMTYPES_PATH = "data/balance/simplified chinese/baseitemtypes.datc64"
BASEITEMTYPES_NAME_OFFSET = 32


def main():
    parser = argparse.ArgumentParser(description="Build a POE2 base item display-name price patch.")
    parser.add_argument("--bundles2", required=True, help="Path to Bundles2 directory")
    parser.add_argument("--prices", required=True, help="JSON mapping source item name to price label")
    parser.add_argument("--resource-map", default=str(Path(__file__).with_name("resource_map.json")))
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument("--price-bundle-name", default="PricePatch")
    parser.add_argument("--bundle-encoder", type=int, default=12, choices=[9, 12])
    parser.add_argument("--oodle-dll", default=None, help="Path to oo2core DLL for Hydra/Oodle compression")
    parser.add_argument("--oodle-level", type=int, default=4)
    args = parser.parse_args()

    if args.bundle_encoder == 12 and not args.oodle_dll:
        core.fail("Hydra output requires --oodle-dll")

    platform.system = lambda: "Linux"
    os.environ["HOME"] = os.environ.get("HOME") or os.path.expanduser("~") or os.environ.get("USERPROFILE", str(Path.home()))
    from PyPoE.poe.file import bundle as bm

    core.install_pypoe_decompress_patch(bm)

    bundles2 = Path(args.bundles2)
    out = Path(args.out)
    out_bundles = out / "Bundles2"
    out_bundles.mkdir(parents=True, exist_ok=True)

    index = bm.Index()
    index.read(str(bundles2 / "_.index.bin"))
    try:
        name_record = index.files[BASEITEMTYPES_HASH]
    except KeyError:
        core.fail("baseitemtypes hash not found in index")

    bundle_path = bundles2 / name_record.bundle.file_name
    if not bundle_path.exists():
        core.fail("required source bundle is missing: %s" % name_record.bundle.file_name)

    bundle = bm.Bundle()
    bundle.read(str(bundle_path))
    bundle.decompress()
    name_record.bundle.contents = bundle
    name_table = name_record.get_file()

    rows, record_len, data_base = core.parse_name_table(name_table)
    if BASEITEMTYPES_NAME_OFFSET + 8 > record_len:
        core.fail("baseitemtypes row layout is not understood: %s" % record_len)

    prices = json.loads(Path(args.prices).read_text(encoding="utf-8"))
    resource_map = json.loads(Path(args.resource_map).read_text(encoding="utf-8"))
    replacements, missing_map = core.build_replacements_from_prices(prices, [resource_map])
    if not replacements:
        core.fail("no replacements produced")

    blob = bytearray(name_table)
    patched_rows = []
    pending = dict(replacements)
    for row in range(rows):
        row_offset = 4 + row * record_len
        key_rel = struct.unpack_from("<Q", blob, row_offset)[0]
        key = core.read_utf16_string(blob, data_base + key_rel)
        if key not in pending:
            continue

        old_rel = struct.unpack_from("<Q", blob, row_offset + BASEITEMTYPES_NAME_OFFSET)[0]
        old_value = core.read_utf16_string(blob, data_base + old_rel)
        new_value = pending.pop(key)
        new_rel = core.append_utf16_string(blob, data_base, new_value)
        struct.pack_into("<Q", blob, row_offset + BASEITEMTYPES_NAME_OFFSET, new_rel)
        patched_rows.append(
            {
                "row": row,
                "resource_id": key,
                "old_value": old_value,
                "new_value": new_value,
                "old_rel": old_rel,
                "new_rel": new_rel,
                "value_offset": BASEITEMTYPES_NAME_OFFSET,
            }
        )

    if pending:
        core.fail("resource ids not found in baseitemtypes: " + ", ".join(sorted(pending)))

    patched_table = bytes(blob)
    price_bundle_path = out_bundles / (args.price_bundle_name + ".bundle.bin")
    core.build_bundle(
        patched_table,
        price_bundle_path,
        encoder=args.bundle_encoder,
        oodle_dll=args.oodle_dll,
        level=args.oodle_level,
    )

    patched_index_raw, price_bundle_id = core.rewrite_index_raw(
        index.data,
        BASEITEMTYPES_HASH,
        args.price_bundle_name,
        len(patched_table),
    )
    core.build_bundle(
        patched_index_raw,
        out_bundles / "_.index.bin",
        encoder=args.bundle_encoder,
        oodle_dll=args.oodle_dll,
        level=args.oodle_level,
    )

    report = {
        "target": {
            "path": BASEITEMTYPES_PATH,
            "file_hash": BASEITEMTYPES_HASH,
            "source_bundle": name_record.bundle.file_name,
            "source_offset": name_record.file_offset,
            "source_size": name_record.file_size,
            "patched_size": len(patched_table),
        },
        "price_bundle": {
            "name": args.price_bundle_name,
            "bundle_id": price_bundle_id,
            "path": "Bundles2/%s.bundle.bin" % args.price_bundle_name,
            "encoder": args.bundle_encoder,
        },
        "patched_rows": patched_rows,
        "missing_resource_map_names": missing_map,
    }
    (out / "patch-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    zip_path = out / "poe2-price-name-patch.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.write(out_bundles / "_.index.bin", "Bundles2/_.index.bin")
        zf.write(price_bundle_path, "Bundles2/%s.bundle.bin" % args.price_bundle_name)
        zf.write(out / "patch-report.json", "patch-report.json")

    print("Wrote:", zip_path)
    print("Patched rows:", len(patched_rows))
    for row in patched_rows:
        print("%s: %s -> %s" % (row["resource_id"], row["old_value"], row["new_value"]))


if __name__ == "__main__":
    main()
