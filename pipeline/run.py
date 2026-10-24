#!/usr/bin/env python3
"""Scrape both listing markets and append successful snapshots to PostgreSQL."""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

APP = Path("/app")
DATA = Path("/data")
REPORTS = Path("/reports")


def csv_data_rows(path: Path) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return max(0, sum(1 for _ in csv.reader(stream)) - 1)


def run_scrape(label: str, command: list[str], output: Path) -> bool:
    print(f"\n=== دریافت داده {label} ===", flush=True)
    result = subprocess.run(command, check=False)
    if result.returncode:
        print(f"استخراج {label} ناموفق بود (کد {result.returncode}).", file=sys.stderr, flush=True)
        return False
    count = csv_data_rows(output)
    if count == 0:
        print(f"برای {label} آگهی تازه‌ای دریافت نشد؛ داده‌ای به پایگاه اضافه نمی‌شود.", flush=True)
        return True
    print(f"{count} ردیف برای {label} آماده‌ی تحلیل و ذخیره است.", flush=True)
    return True


def ingest(label: str, script: Path, csv_path: Path, reports: Path) -> bool:
    print(f"\n=== تحلیل و افزودن snapshotهای {label} ===", flush=True)
    result = subprocess.run([
        sys.executable, str(script), "--csv", str(csv_path), "--reports-dir", str(reports)
    ], check=False, env=os.environ.copy())
    if result.returncode:
        print(f"ذخیره‌ی {label} ناموفق بود (کد {result.returncode}).", file=sys.stderr, flush=True)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape and append car and real-estate market snapshots")
    parser.add_argument("--pages", type=int, default=1, help="maximum pages per car query and property category")
    parser.add_argument("--car-delay", type=float, default=1.5, help="pause between Bama requests, seconds")
    parser.add_argument("--real-estate-delay", type=float, default=2.0, help="pause between Sheypoor pages, seconds")
    parser.add_argument("--details", action="store_true", help="fetch public detail pages for car listings")
    args = parser.parse_args()
    if args.pages < 1 or args.car_delay < 0 or args.real_estate_delay < 0:
        parser.error("pages must be positive and delays must be non-negative")
    if not os.environ.get("DATABASE_URL"):
        parser.error("DATABASE_URL is required")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]
    car_csv = DATA / "cars" / f"run_{run_id}.csv"
    property_csv = DATA / "real_estate" / f"run_{run_id}.csv"
    car_csv.parent.mkdir(parents=True, exist_ok=True)
    property_csv.parent.mkdir(parents=True, exist_ok=True)

    car_command = [
        sys.executable, str(APP / "cars/bama_scraper.py"),
        "--queries-file", str(APP / "cars/queries.txt"),
        "--pages", str(args.pages), "--delay", str(args.car_delay),
        "--output", str(car_csv),
    ]
    if args.details:
        car_command.append("--details")
    property_command = [
        sys.executable, str(APP / "real_estate/sheypoor_scraper.py"),
        "--mode", "both", "--pages", str(args.pages),
        "--delay", str(args.real_estate_delay), "--output", str(property_csv),
    ]

    car_ok = run_scrape("خودرو", car_command, car_csv)
    property_ok = run_scrape("املاک", property_command, property_csv)
    failed = not car_ok or not property_ok

    # Keep every run's report artifacts in a fresh directory. This avoids
    # trying to overwrite files created by older containers with different UIDs.
    run_reports = REPORTS / run_id
    if car_ok and csv_data_rows(car_csv):
        failed |= not ingest("خودرو", APP / "cars/analytics/ingest.py", car_csv, run_reports / "cars")
    if property_ok and csv_data_rows(property_csv):
        failed |= not ingest("املاک", APP / "real_estate/analytics/ingest.py", property_csv, run_reports / "real_estate")

    if failed:
        print(f"اجرای {run_id} با خطا تمام شد؛ snapshotهای قبلی محفوظ‌اند.", file=sys.stderr)
        return 1
    print(f"\nاجرای {run_id} تمام شد. فایل‌های CSV این دور در /data ذخیره شدند؛ تاریخچه‌ی قبلی PostgreSQL حفظ شد.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
