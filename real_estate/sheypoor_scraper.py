#!/usr/bin/env python3
"""Polite public-card scraper for residential real-estate ads on Sheypoor."""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE = "https://www.sheypoor.com"
USER_AGENT = "Mozilla/5.0 (compatible; PublicPropertyResearch/1.0)"
CATEGORIES = {
    "sale": ["/s/iran/houses-apartments-for-sale"],
    "rent": ["/s/iran/house-apartment-for-rent"],
}
FIELDS = [
    "ad_id", "url", "title", "listing_type", "property_type", "city", "neighborhood",
    "area_sqm", "rooms", "year_built", "sale_price_toman", "deposit_toman",
    "monthly_rent_toman", "daily_rent_toman", "price_agreed", "posted_text", "scraped_at",
]
POSTED_RE = re.compile(r"(?:لحظاتی پیش|دقایقی پیش|\d+\s*(?:دقیقه|ساعت|روز|هفته|ماه) پیش|امروز|دیروز)")


def latin_digits(text: str) -> str:
    return text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def parse_amount(text: str | None) -> int | None:
    if not text:
        return None
    normalized = latin_digits(text).replace(",", "").replace("٬", "").replace(" ", "")
    match = re.search(r"\d+", normalized)
    return int(match.group(0)) if match else None


def page_url(base_url: str, page_num: int) -> str:
    parts = urlsplit(base_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if page_num > 1:
        query["page_num"] = str(page_num)
    else:
        query.pop("page_num", None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def get_label_amount(anchor, label_terms: tuple[str, ...]) -> int | None:
    for small in anchor.find_all("small"):
        label = small.get_text(" ", strip=True).replace(":", "").replace("：", "")
        if "اجاره روزانه" in label and "اجاره روزانه" not in label_terms:
            continue
        if any(term in label for term in label_terms):
            container = small.parent
            icon = container.select_one('[data-test-id="icon-toman"]')
            amount = icon.find_previous_sibling("span") if icon else None
            if amount:
                return parse_amount(amount.get_text(" ", strip=True))
            return parse_amount(container.get_text(" ", strip=True).replace(label, "", 1))
    return None


def get_unlabelled_price(anchor) -> int | None:
    for icon in anchor.select('[data-test-id="icon-toman"]'):
        container = icon.parent
        if container.find("small"):
            continue
        amount = icon.find_previous_sibling("span")
        if amount:
            return parse_amount(amount.get_text(" ", strip=True))
    return None


def attribute(anchor, icon_name: str) -> str | None:
    icon = anchor.select_one(f'[data-test-id="icon-{icon_name}"]')
    if not icon:
        return None
    value = icon.find_next_sibling("span")
    return value.get_text(" ", strip=True) if value else None


def parse_card(anchor, category: str, scraped_at: str) -> dict | None:
    href = anchor.get("href", "")
    if not href.startswith("/v/"):
        return None
    test_id = anchor.get("data-test-id", "")
    id_match = re.search(r"ad-item-(\d+)", test_id)
    if not id_match:
        id_match = re.search(r"-(\d+)\.html(?:$|\?)", href)
    ad_id = id_match.group(1) if id_match else href.rsplit("-", 1)[-1].split(".")[0]
    title_node = anchor.find("h2")
    title = title_node.get_text(" ", strip=True) if title_node else None
    text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))

    sale_price = get_label_amount(anchor, ("قیمت",))
    deposit = get_label_amount(anchor, ("رهن", "ودیعه"))
    monthly_rent = get_label_amount(anchor, ("اجاره",))
    daily_rent = get_label_amount(anchor, ("اجاره روزانه",))
    if sale_price is None and category == "sale":
        sale_price = get_unlabelled_price(anchor)
    if daily_rent is None and "اجاره روزانه" in text:
        # Daily-rent cards may display their amount without a visible label node.
        daily_rent = get_unlabelled_price(anchor)

    attrs = anchor.select_one('[data-test-id="rs-attributes"]')
    area_raw = attribute(anchor, "area")
    rooms_raw = attribute(anchor, "bed")
    year_raw = attribute(anchor, "year")
    location = None
    posted = None
    for small in anchor.find_all("small"):
        value = small.get_text(" ", strip=True).strip(" |،")
        if not value:
            continue
        if POSTED_RE.search(value):
            posted = value
        elif "|" in small.get_text(" ", strip=True) or (not any(x in value for x in ("رهن", "اجاره", "قیمت")) and len(value) < 100):
            if value not in ("Ad تابلو شده",):
                location = value
    if location:
        location = location.replace("|", "").strip()
        parts = [part.strip() for part in location.split("،", 1)]
        city = parts[0] or None
        neighborhood = parts[1] if len(parts) > 1 and parts[1] else None
    else:
        city = neighborhood = None

    price_agreed = "قیمت توافقی" in text
    listing_type = "rent" if category == "rent" else "sale"
    if daily_rent is not None:
        listing_type = "daily_rent"
    property_type = "land" if any(word in (title or "") for word in ("زمین", "باغ")) else "residential"
    return {
        "ad_id": ad_id,
        "url": urljoin(BASE, href),
        "title": title,
        "listing_type": listing_type,
        "property_type": property_type,
        "city": city,
        "neighborhood": neighborhood,
        "area_sqm": parse_amount(area_raw),
        "rooms": 0 if rooms_raw and "بدون اتاق" in rooms_raw else parse_amount(rooms_raw),
        "year_built": parse_amount(year_raw),
        "sale_price_toman": sale_price,
        "deposit_toman": deposit,
        "monthly_rent_toman": monthly_rent if listing_type == "rent" else None,
        "daily_rent_toman": daily_rent,
        "price_agreed": price_agreed,
        "posted_text": posted,
        "scraped_at": scraped_at,
    }


def validate_url(url: str) -> None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host != "sheypoor.com" or not parts.path.startswith("/s/"):
        raise ValueError("فقط صفحه‌های عمومی دسته‌بندی شیپور با مسیر /s/ پشتیبانی می‌شوند.")
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    if any(key != "page_num" for key in params):
        raise ValueError("برای رعایت robots.txt فقط پارامتر صفحه‌بندی page_num مجاز است.")


def robots_parser(session: requests.Session, timeout: int) -> RobotFileParser:
    response = session.get(f"{BASE}/robots.txt", timeout=timeout)
    response.raise_for_status()
    parser = RobotFileParser()
    parser.set_url(f"{BASE}/robots.txt")
    parser.parse(response.text.splitlines())
    return parser


def fetch_category(session: requests.Session, robots: RobotFileParser, base_url: str,
                   category: str, pages: int, timeout: int, delay: float) -> list[dict]:
    validate_url(base_url)
    rows, seen = [], set()
    for page_num in range(1, pages + 1):
        url = page_url(base_url, page_num)
        if not robots.can_fetch("*", url):
            print(f"طبق robots.txt این صفحه واکشی نمی‌شود: {url}", file=sys.stderr)
            break
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        anchors = soup.select('a[href^="/v/"]')
        fresh = []
        for anchor in anchors:
            row = parse_card(anchor, category, scraped_at)
            if row and row["ad_id"] not in seen:
                seen.add(row["ad_id"])
                fresh.append(row)
        rows.extend(fresh)
        if not fresh:
            break
        if page_num < pages:
            time.sleep(delay)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="استخراج فیلدهای عمومی آگهی ملک از کارت‌های شیپور")
    parser.add_argument("--mode", choices=("both", "sale", "rent"), default="both")
    parser.add_argument("--url", action="append", help="صفحه دسته‌بندی عمومی شیپور؛ برای شهر خاص مسیر /s/... را بدهید")
    parser.add_argument("--pages", type=int, default=1, help="حداکثر صفحه در هر دسته؛ page_num طبق robots.txt")
    parser.add_argument("--delay", type=float, default=2.0, help="مکث بین صفحات، ثانیه")
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--output", default="data/real_estate/sheypoor_properties.csv")
    parser.add_argument("--format", choices=("csv", "json"), default="csv")
    args = parser.parse_args()
    if args.pages < 1 or args.delay < 0:
        parser.error("pages باید مثبت و delay نامنفی باشد")

    categories = []
    if args.url:
        for url in args.url:
            validate_url(url)
            category = "rent" if "rent" in urlsplit(url).path.lower() else "sale"
            categories.append((url, category))
    else:
        for name in ("sale", "rent") if args.mode == "both" else (args.mode,):
            categories.extend((urljoin(BASE, path), name) for path in CATEGORIES[name])

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.5"})
    records, seen = [], set()
    try:
        robots = robots_parser(session, args.timeout)
        for index, (url, category) in enumerate(categories):
            if index:
                time.sleep(args.delay)
            for row in fetch_category(session, robots, url, category, args.pages, args.timeout, args.delay):
                if row["ad_id"] not in seen:
                    records.append(row)
                    seen.add(row["ad_id"])
    except requests.RequestException as exc:
        print(f"خطا در دریافت صفحه: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        parser.error(str(exc))

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "json":
        import json
        output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        with output.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(records)
    print(f"تعداد آگهی‌های یکتای استخراج‌شده: {len(records)}")
    print(f"فروش: {sum(row['listing_type'] == 'sale' for row in records)} | اجاره: {sum(row['listing_type'] == 'rent' for row in records)} | اجاره روزانه: {sum(row['listing_type'] == 'daily_rent' for row in records)}")
    print(f"ذخیره شد: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
