#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo


def api_get_json(api_base, path, params=None):
    url = api_base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "poe2-price-patcher-monitor/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m-%d %H:%M:%S",
        "%m-%d %H:%M",
    ]
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            if "%Y" not in fmt:
                parsed = parsed.replace(year=now.year)
            return parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(ZoneInfo("Asia/Shanghai"))
    except ValueError:
        return None


def github_output(**values):
    output = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{key}={value}" for key, value in values.items()]
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="Detect latest poecurrency.top price version.")
    parser.add_argument("--api-base", default="https://poecurrency.top")
    parser.add_argument("--item-name", action="append", default=None)
    parser.add_argument("--category-label", default="通货仓库")
    parser.add_argument("--hours", type=int, default=1)
    parser.add_argument("--version", type=int, default=None)
    parser.add_argument("--season", default=None)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    item_names = args.item_name or [
        "神圣石",
        "崇高石",
        "卡兰德的魔镜",
        "混沌石",
        "剥离石",
        "瓦尔宝珠",
    ]

    hour_windows = []
    for value in [args.hours, 2, 6, 24]:
        if value not in hour_windows:
            hour_windows.append(value)

    def fetch_one(item_name, hours):
        params = {
            "item_name": item_name,
            "category_label": args.category_label,
            "hours": hours,
        }
        if args.version:
            params["version"] = args.version
        if args.season:
            params["season"] = args.season
        rows = api_get_json(args.api_base, "/api/db/price", params)
        return item_name, rows

    candidates = []
    errors = []
    for attempt in range(3):
        rows = []
        for hours in hour_windows:
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
                futures = [executor.submit(fetch_one, item_name, hours) for item_name in item_names]
                for future in as_completed(futures):
                    try:
                        _, item_rows = future.result()
                        rows.extend(item_rows)
                    except Exception as exc:
                        errors.append(str(exc))
            candidates = [parse_datetime(row.get("datetime")) for row in rows if isinstance(row, dict)]
            candidates = [dt for dt in candidates if dt]
            if candidates:
                break
        if candidates:
            break
        time.sleep(5 * (attempt + 1))

    if not candidates:
        detail = "; ".join(errors[:3])
        raise SystemExit("ERROR: no price datetime found" + (": " + detail if detail else ""))

    latest = max(candidates)
    version_label = latest.strftime("%m-%d %H:%M")
    tag = "price-" + latest.strftime("%Y%m%d-%H%M")
    github_output(version=version_label, tag=tag, iso=latest.isoformat())
    print(f"latest_price_time={version_label}", file=sys.stderr)


if __name__ == "__main__":
    main()
