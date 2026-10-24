#!/usr/bin/env python3
"""Polite public-listing scraper for Bama.ir used-car ads (v2).

Collects public vehicle listing fields only. It deliberately does not collect
phone numbers, seller names, or other contact details.

What is new in v2
-----------------
* The card text is parsed with one anchored regex instead of loose searches, so
  title / location / price / mileage no longer bleed into each other.
* The model string is split into structured columns:
      model (family) · trim · gearbox · gearbox_type · gears · engine_liters ·
      is_turbo · fuel_type · drive
  and combined into `variant_key`, a stable key you can group by. Two ads share
  a variant_key only if every one of those attributes matches, so
  "Shahin 402 CVT", "Shahin 402 GL" and "Shahin 402 Plus" never get averaged.
* Years are normalised: Bama mixes Jalali (1402) and Gregorian (2026) years;
  both `year_jalali` and `year_gregorian` are always filled.
* Zero-km cars get mileage_km = 0 and is_zero_km = True (not NaN).
* The URL slug (e.g. "peugeot-207-tu3mt-1405") is used as a second source,
  e.g. "mt"/"at" in the slug fills `gearbox` when the card does not say it.
* `raw_card_text` is saved, so the parser can be improved later and re-run
  offline with --reparse, without hitting the site again.
* Fair-price estimation only compares ads of the SAME variant (same year, then
  year ±1), never different trims; implausible prices are excluded.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://bama.ir"
USER_AGENT = "Mozilla/5.0 (compatible; BamaPublicListingsResearch/1.0; +https://bama.ir)"

# --------------------------------------------------------------------------
# Text normalisation
# --------------------------------------------------------------------------
_DIGIT_MAP = {0x06F0 + i: str(i) for i in range(10)}          # ۰-۹
_DIGIT_MAP.update({0x0660 + i: str(i) for i in range(10)})    # ٠-٩
_CHAR_MAP = str.maketrans({
    "\u064a": "\u06cc", "\u0649": "\u06cc",   # Arabic yeh  -> Persian yeh
    "\u0643": "\u06a9",                       # Arabic kaf  -> Persian kaf
    "\u06c0": "\u0647", "\u0629": "\u0647",   # heh variants
    "\u0640": "",                             # kashida
    "\u200c": " ",                            # ZWNJ -> space (consistent keys)
    "\u200e": "", "\u200f": "", "\xa0": " ",
    "\u066c": ",", "\u066b": ".",             # Arabic thousands / decimal sep
})


def normalize_digits(value: str) -> str:
    return value.translate(_DIGIT_MAP)


def normalize_fa(value: str) -> str:
    """Unify digits, Arabic/Persian letter variants, ZWNJ and whitespace."""
    value = normalize_digits(value).translate(_CHAR_MAP)
    return re.sub(r"\s+", " ", value).strip()


def number(value: str | None) -> int | None:
    if not value:
        return None
    value = normalize_fa(value).replace(",", "").replace(" ", "")
    try:
        return int(float(value))
    except ValueError:
        return None


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


# --------------------------------------------------------------------------
# Domain knowledge (extend these freely)
# --------------------------------------------------------------------------
# Families whose name is more than one word; matched on a token prefix.
# Add your own with --families-file families.json  (a JSON list of strings).
KNOWN_FAMILIES: list[str] = ["گرند ویتارا", "لند کروزر", "ایکس تریل"]

# Different spellings of the same family / make -> one canonical spelling.
FAMILY_ALIASES = {"کوئیک": "کوییک", "تیبا2": "تیبا 2"}
MAKE_ALIASES = {"بی ام و": "ب ام و", "مرسدس‌بنز": "مرسدس بنز"}

# Badges that appear on the card and must not leak into `location`.
BADGES = ("احراز هویت شده", "قیمت توافقی", "کارشناسی شده", "فروش فوری")

MIN_PRICE_TOMAN_DEFAULT = 100_000_000   # below this the price is a placeholder
MAX_PLAUSIBLE_KM = 1_500_000


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class Listing:
    ad_id: str
    url: str
    title: str | None = None            # clean "make، model year" (no price/city)
    query: str | None = None            # which search line found this ad (query mode)
    match_note: str | None = None       # exact / gearbox_unstated / cvt_assumed ...
    make: str | None = None
    model: str | None = None            # family, e.g. "شاهین 402", "207", "X3"
    trim: str | None = None             # e.g. "GL", "پلاس", "TU3", "تیپ 2"
    variant_key: str | None = None      # <- group by this for aggregation
    gearbox: str | None = None          # manual / automatic (None = not stated)
    gearbox_type: str | None = None     # CVT / DCT / AMT / TIPTRONIC
    gears: int | None = None
    engine_liters: float | None = None
    is_turbo: bool = False
    fuel_type: str | None = None        # hybrid / phev / electric / diesel / bifuel
    drive: str | None = None            # AWD / FWD / RWD
    year_jalali: int | None = None
    year_gregorian: int | None = None
    year_original: int | None = None
    year_calendar: str | None = None    # jalali | gregorian (as shown on Bama)
    mileage_km: int | None = None
    is_zero_km: bool = False
    price_toman: int | None = None
    price_is_agreed: bool = False
    price_valid: bool = False
    city: str | None = None
    district: str | None = None
    badges: str | None = None
    posted_text: str | None = None
    body_condition: str | None = None
    exterior_color: str | None = None
    interior_color: str | None = None
    detail_gearbox: str | None = None
    detail_fuel: str | None = None
    detail_title: str | None = None
    model_raw: str | None = None        # text between "make،" and the year
    slug_name: str | None = None        # e.g. "peugeot-207-tu3mt"
    flags: str | None = None            # data-quality flags, ';'-separated
    scraped_at: str = ""
    raw_card_text: str | None = None
    fair_price_toman: int | None = None
    discount_pct: float | None = None
    comparable_count: int = 0
    comparison_basis: str | None = None
    underpriced: bool | None = None


# --------------------------------------------------------------------------
# Card parsing
# --------------------------------------------------------------------------
CARD_RE = re.compile(
    r"^(?:\d+\s+(?=[^،]+،))?"                            # photo-count badge
    r"(?:(?P<make>[^،.]{1,40}?)\s*،\s*)?"                # make،
    r"(?P<model>.+?)\s+"                                 # model + trim + specs
    r"(?P<year>(?:13|14|19|20)\d{2})\s*\.?\s*"           # year (only when followed by usage)
    r"(?P<usage>صفر کیلومتر|کارکرد\s*(?P<km>\d[\d,.]*)\s*کیلومتر)"
    r"\s*(?P<tail>.*)$"
)
TIME_AGO_RE = re.compile(r"(?:لحظاتی|دقایقی|ساعاتی)\s*پیش|\d+\s*(?:دقیقه|ساعت|روز|هفته|ماه)\s*پیش|دیروز|امروز|\d{4}/\d{1,2}/\d{1,2}")
PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)\s*تومان")
PRICE_TRAILING_RE = re.compile(r"(\d{1,3}(?:,\d{3})+)\s*$")


def split_year(year: int) -> tuple[str | None, int | None, int | None]:
    """Return (calendar, jalali, gregorian). Gregorian->Jalali is approximate (±1)."""
    if 1300 <= year <= 1500:
        return "jalali", year, year + 621
    if 1900 <= year <= 2100:
        return "gregorian", year - 621, year
    return None, None, None


def parse_tail(tail: str) -> dict:
    """tail = everything after the mileage: 'time-ago location [badges] price'."""
    out: dict = {"price": None, "agreed": False, "posted": None, "badges": [], "location": None}
    if "قیمت توافقی" in tail:
        out["agreed"] = True
    match = None
    for match in PRICE_RE.finditer(tail):
        pass
    if match is None:
        match = PRICE_TRAILING_RE.search(tail)
    if match:
        out["price"] = number(match.group(1))
        tail = tail[:match.start()] + " " + tail[match.end():]
    time_match = TIME_AGO_RE.search(tail)
    if time_match:
        out["posted"] = time_match.group(0)
        tail = tail[:time_match.start()] + " " + tail[time_match.end():]
    for badge in BADGES:
        if badge in tail:
            out["badges"].append(badge)
            tail = tail.replace(badge, " ")
    tail = re.sub(r"\s+", " ", tail).strip(" .،")
    out["location"] = tail or None
    return out


def parse_card_text(text: str) -> dict | None:
    match = CARD_RE.match(normalize_fa(text))
    if not match:
        return None
    calendar, jalali, gregorian = split_year(int(match.group("year")))
    zero = match.group("usage").startswith("صفر")
    km = number(match.group("km")) if match.group("km") else None
    tail = parse_tail(match.group("tail"))
    make = (match.group("make") or "").strip() or None
    return {
        "make": MAKE_ALIASES.get(make, make) if make else None,
        "model_raw": match.group("model").strip(),
        "year_original": int(match.group("year")),
        "calendar": calendar, "jalali": jalali, "gregorian": gregorian,
        "zero": zero or km == 0, "km": 0 if zero else km, **tail,
    }


def listing_from_text(raw_text: str, ad_id: str, url: str, fetched_at: str) -> Listing:
    item = Listing(ad_id=ad_id, url=url, scraped_at=fetched_at, raw_card_text=normalize_fa(raw_text) or None)
    parsed = parse_card_text(raw_text)
    if not parsed:
        item.flags = "parse_failed"          # keep the row; --reparse can fix it later
        return item
    location = parsed["location"]
    city, _, district = (location or "").partition("،")
    item.make = parsed["make"]
    item.model_raw = parsed["model_raw"]
    item.title = f"{item.make}، {item.model_raw} {parsed['year_original']}" if item.make else \
        f"{item.model_raw} {parsed['year_original']}"
    item.year_original, item.year_calendar = parsed["year_original"], parsed["calendar"]
    item.year_jalali, item.year_gregorian = parsed["jalali"], parsed["gregorian"]
    item.mileage_km, item.is_zero_km = parsed["km"], parsed["zero"]
    item.price_toman, item.price_is_agreed = parsed["price"], parsed["agreed"]
    item.posted_text = parsed["posted"]
    item.badges = ";".join(parsed["badges"]) or None
    item.city, item.district = city.strip() or None, district.strip() or None
    return item


# --------------------------------------------------------------------------
# Variant extraction (the part that separates CVT / GL / Plus ...)
# --------------------------------------------------------------------------
def _w(pattern: str) -> str:
    return r"(?<!\w)(?:" + pattern + r")(?!\w)"


RE_LITERS_T = re.compile(r"(?<![\w.])(\d\.\d) ?t(?!\w)", re.I)               # 2.0T
RE_LITERS = re.compile(r"(?<![\w.])(\d(?:\.\d)?) ?(?:لیتر|l)(?!\w)", re.I)    # 1.5 لیتر
RE_CC = re.compile(_w(r"(\d{3,4}) ?(?:سی ?سی|cc)"), re.I)
RE_TURBO = re.compile(_w(r"توربو(?: شارژ)?|turbo"), re.I)
RE_PHEV = re.compile(_w(r"پلاگین(?: این)?(?: هیبرید)?|plug-?in(?: hybrid)?|phev"), re.I)
RE_HYBRID = re.compile(_w(r"هیبرید|هایبرید|hybrid|mhev|e?-?hev"), re.I)
RE_EV = re.compile(_w(r"برقی|الکتریکی?|electric|bev|ev"), re.I)
RE_DIESEL = re.compile(_w(r"دیزلی?|diesel|tdi|crdi|dci"), re.I)
RE_BIFUEL = re.compile(_w(r"دوگانه ?سوز|cng|lpg"), re.I)
RE_DRIVE = re.compile(_w(r"awd|4wd|4x4|fwd|rwd|2wd|چهار ?چرخ ?محرک"), re.I)
RE_GEAR_TYPE = re.compile(_w(r"cvt|dct|dsg|amt|tiptronic|سی وی تی|تیپ ?ترونیک"), re.I)
RE_AUTO = re.compile(_w(r"اتوماتیک|اتومات|automatic|at"), re.I)
RE_MANUAL = re.compile(_w(r"دستی|دنده ?ای|manual|mt"), re.I)
RE_GEARS = re.compile(_w(r"(\d{1,2}) ?سرعته"), re.I)

GEAR_TYPE_MAP = {"cvt": "CVT", "سی وی تی": "CVT", "dct": "DCT", "dsg": "DCT", "amt": "AMT",
                 "tiptronic": "TIPTRONIC", "تیپ ترونیک": "TIPTRONIC", "تیپترونیک": "TIPTRONIC"}
DRIVE_MAP = {"awd": "AWD", "4wd": "AWD", "4x4": "AWD", "fwd": "FWD", "rwd": "RWD", "2wd": "2WD"}


def extract_attributes(text: str) -> tuple[dict, str]:
    """Pull technical attributes out of a model string.

    Returns (attributes, leftover) where leftover is what remains once all the
    recognised attribute words are removed - i.e. family + trim.
    """
    s = f" {normalize_fa(text)} "
    attrs: dict = {}

    def cut(rx: re.Pattern):
        nonlocal s
        found = rx.search(s)
        if found:
            s = s[:found.start()] + " " + s[found.end():]
        return found

    if m := cut(RE_LITERS_T):
        attrs["engine_liters"], attrs["is_turbo"] = float(m.group(1)), True
    if m := cut(RE_LITERS):
        attrs.setdefault("engine_liters", float(m.group(1)))
    if m := cut(RE_CC):
        attrs.setdefault("engine_liters", round(int(m.group(1)) / 1000, 1))
    if cut(RE_TURBO):
        attrs["is_turbo"] = True

    if cut(RE_PHEV):
        attrs["fuel_type"] = "phev"
    elif cut(RE_HYBRID):
        attrs["fuel_type"] = "hybrid"
    elif cut(RE_EV):
        attrs["fuel_type"] = "electric"
    elif cut(RE_DIESEL):
        attrs["fuel_type"] = "diesel"
    elif cut(RE_BIFUEL):
        attrs["fuel_type"] = "bifuel"

    if m := cut(RE_DRIVE):
        key = m.group(0).lower().replace(" ", "")
        attrs["drive"] = DRIVE_MAP.get(key, "AWD")

    if m := cut(RE_GEAR_TYPE):
        attrs["gearbox_type"] = GEAR_TYPE_MAP.get(m.group(0).lower().replace("  ", " "), m.group(0).upper())
        attrs["gearbox"] = "automatic"
    auto, manual = cut(RE_AUTO), cut(RE_MANUAL)
    if "gearbox" not in attrs:
        if auto:
            attrs["gearbox"] = "automatic"
        elif manual:
            attrs["gearbox"] = "manual"
    if m := cut(RE_GEARS):
        attrs["gears"] = int(m.group(1))

    return attrs, re.sub(r"\s+", " ", s).strip(" .،,-/")


def _canon_token(token: str) -> str:
    return token.upper() if re.fullmatch(r"[A-Za-z0-9\-]+", token) else token


def split_family_trim(leftover: str, families: list[str]) -> tuple[str, str]:
    tokens = [t.strip(".,،/-") for t in leftover.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return "", ""
    lowered = [t.casefold() for t in tokens]
    for family in sorted(families, key=lambda f: -len(f.split())):
        parts = [p.casefold() for p in family.split()]
        if lowered[:len(parts)] == parts:
            return family, " ".join(_canon_token(t) for t in tokens[len(parts):])
    take = 1
    # Persian name followed by a bare number is one family: "شاهین 402", "تندر 90", "آریزو 6".
    # A Latin first token ("X3", "RX", "HR-V") or a number ("206") is the family on its own.
    if len(tokens) > 1 and re.fullmatch(r"[\u0600-\u06FF]+", tokens[0]) and re.fullmatch(r"\d{1,4}", tokens[1]):
        take = 2
    family = " ".join(_canon_token(t) for t in tokens[:take])
    trim = " ".join(_canon_token(t) for t in tokens[take:])
    return family, trim


SLUG_GEAR_RE = re.compile(r"(?:[a-z]*\d+)?(mt|at|cvt|dct|amt)")


def parse_slug(ad_id: str) -> tuple[str | None, str | None]:
    """'2paf1uil-peugeot-207-tu3mt-1405' -> ('peugeot-207-tu3mt', 'manual')."""
    tokens = ad_id.split("-")
    if len(tokens) < 3:
        return None, None
    body = tokens[1:-1] if re.fullmatch(r"\d{4}", tokens[-1]) else tokens[1:]
    gearbox = None
    for token in body[1:]:
        hit = SLUG_GEAR_RE.fullmatch(token)
        if hit:
            gearbox = "manual" if hit.group(1) == "mt" else "automatic"
    return "-".join(body) or None, gearbox


def finalize(item: Listing, families: list[str]) -> None:
    """Fill every derived column. Safe to call again after detail pages are loaded."""
    item.slug_name, slug_gearbox = parse_slug(item.ad_id)
    if not item.model_raw:
        return
    attrs, leftover = extract_attributes(item.model_raw)
    for extra in (item.detail_gearbox, item.detail_fuel):      # detail page fills gaps only
        if extra:
            more, _ = extract_attributes(extra)
            for key, value in more.items():
                attrs.setdefault(key, value)
    if "gearbox" not in attrs and slug_gearbox:
        attrs["gearbox"] = slug_gearbox
    item.gearbox = attrs.get("gearbox")
    item.gearbox_type = attrs.get("gearbox_type")
    item.gears = attrs.get("gears")
    item.engine_liters = attrs.get("engine_liters")
    item.is_turbo = bool(attrs.get("is_turbo"))
    item.fuel_type = attrs.get("fuel_type")
    item.drive = attrs.get("drive")

    family, trim = split_family_trim(leftover, families)
    family = FAMILY_ALIASES.get(family, family) or item.make or ""
    item.model, item.trim = family or None, trim or None

    parts = [item.make, item.model, item.trim, item.gearbox_type or item.gearbox]
    if item.engine_liters:
        parts.append(f"{item.engine_liters:g}L")
    if item.is_turbo:
        parts.append("turbo")
    parts += [item.fuel_type, item.drive]
    item.variant_key = " | ".join(dict.fromkeys(p for p in parts if p)) or None


# --------------------------------------------------------------------------
# Data quality + price estimation
# --------------------------------------------------------------------------
def mark_quality(listings: Iterable[Listing], min_price: int) -> None:
    for item in listings:
        flags = {f for f in (item.flags or "").split(";") if f}
        flags -= {"price_too_low", "mileage_implausible", "no_year"}
        if item.price_toman and item.price_toman < min_price:
            flags.add("price_too_low")
        if item.mileage_km and item.mileage_km > MAX_PLAUSIBLE_KM:
            flags.add("mileage_implausible")
        if item.model_raw and item.year_jalali is None:
            flags.add("no_year")
        item.flags = ";".join(sorted(flags)) or None
        item.price_valid = bool(item.price_toman and item.price_toman >= min_price
                                and not item.price_is_agreed and "mileage_implausible" not in flags)


def _mileage_close(a: Listing, b: Listing) -> bool:
    if a.mileage_km is None or b.mileage_km is None:
        return True
    return abs(b.mileage_km - a.mileage_km) <= max(30_000, a.mileage_km * 0.30)


def estimate_prices(listings: list[Listing], discount_threshold: float, min_comparables: int = 3) -> None:
    """Asking-price baseline from ads of the SAME variant (same trim, gearbox, engine...).

    Tries the same year first, then year ±1. Zero-km cars are only compared with zero-km cars.
    """
    pool = [x for x in listings if x.price_valid and x.variant_key and x.year_jalali]
    by_variant: dict[str, list[Listing]] = defaultdict(list)
    for item in pool:
        by_variant[item.variant_key].append(item)
    for item in pool:
        comps: list[int] = []
        for spread, basis in ((0, "variant+year"), (1, "variant+year±1")):
            comps = [o.price_toman for o in by_variant[item.variant_key]
                     if o.ad_id != item.ad_id
                     and abs(o.year_jalali - item.year_jalali) <= spread
                     and o.is_zero_km == item.is_zero_km
                     and _mileage_close(item, o)]
            if len(comps) >= min_comparables:
                item.comparison_basis = basis
                break
        item.comparable_count = len(comps)
        if len(comps) < min_comparables:
            continue
        fair = int(median(comps))
        item.fair_price_toman = fair
        item.discount_pct = round((fair - item.price_toman) / fair * 100, 1)
        item.underpriced = item.price_toman <= fair * (1 - discount_threshold)


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------
def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.5"})
    retry = Retry(total=3, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",), respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


PAGE_PARAM = "page"      # change with --page-param if Bama uses another name


def listing_url(page_url: str, page_num: int) -> str:
    parts = urlsplit(page_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if page_num > 1:
        query[PAGE_PARAM] = str(page_num)
    else:
        query.pop(PAGE_PARAM, None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


DETAIL_LABELS = {
    "body_condition": ("وضعیت بدنه",),
    "exterior_color": ("رنگ بدنه",),
    "interior_color": ("رنگ داخل", "رنگ داخلی"),
    "detail_gearbox": ("گیربکس",),
    "detail_fuel": ("نوع سوخت",),
}
_STOP_LABELS = {label for labels in DETAIL_LABELS.values() for label in labels} | {
    "شماره تماس", "توضیحات", "پیش از بازدید"}


def extract_detail(session: requests.Session, listing: Listing, timeout: int) -> None:
    """Read label/value pairs from the public detail page (no contact fields)."""
    response = session.get(listing.url, timeout=timeout)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    chunks = [normalize_fa(s) for s in soup.stripped_strings]
    for index, chunk in enumerate(chunks):
        for field, labels in DETAIL_LABELS.items():
            if getattr(listing, field):
                continue
            for label in labels:
                value = None
                if chunk == label and index + 1 < len(chunks) and chunks[index + 1] not in _STOP_LABELS:
                    value = chunks[index + 1]
                elif chunk.startswith(label + " "):
                    value = chunk[len(label) + 1:]
                if value and len(value) <= 40:        # long text = description, not a spec
                    setattr(listing, field, value)
                    break
    h1 = soup.find("h1")
    if h1:
        listing.detail_title = clean_text(h1.get_text(" ", strip=True))


def _card_map(soup: BeautifulSoup) -> dict[str, tuple[str, str]]:
    """ad_id -> (card text, href). One ad can have several anchors; keep the longest text."""
    cards: dict[str, tuple[str, str]] = {}
    for anchor in soup.select('a[href*="/car/detail-"]'):
        href = anchor.get("href", "")
        found = re.search(r"/car/detail-([^/?#]+)", href)
        if not found:
            continue
        text = clean_text(anchor.get_text(" ", strip=True))
        if found.group(1) not in cards or len(text) > len(cards[found.group(1)][0]):
            cards[found.group(1)] = (text, href)
    return cards


def fetch_cards(session: requests.Session, url: str, pages: int, delay: float,
                timeout: int) -> tuple[list[tuple[str, str, str, str]], str]:
    """Walk result pages. Returns ([(ad_id, text, href, fetched_at)], stop_reason)."""
    seen: set[str] = set()
    out: list[tuple[str, str, str, str]] = []
    reason = f"به سقف --pages={pages} رسید"
    for page in range(1, pages + 1):
        response = session.get(listing_url(url, page), timeout=timeout)
        if response.status_code == 404:
            return out, "HTTP 404" if page == 1 else f"صفحه {page} وجود نداشت (404)"
        response.raise_for_status()
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cards = _card_map(BeautifulSoup(response.text, "html.parser"))
        if not cards:
            reason = "بدون نتیجه" if page == 1 else f"صفحه {page} آگهی نداشت"
            break
        fresh = {ad: card for ad, card in cards.items() if ad not in seen}
        if not fresh:
            reason = (f"صفحه {page} آگهی تازه نداشت (آخر لیست، یا پارامتر page کار نمی‌کند)")
            break
        for ad_id, (text, href) in fresh.items():
            seen.add(ad_id)
            out.append((ad_id, text, href, fetched_at))
        if page < pages:
            time.sleep(delay)
    return out, reason


def scrape(session: requests.Session, url: str, pages: int, delay: float,
           timeout: int, details: bool) -> list[Listing]:
    cards, reason = fetch_cards(session, url, pages, delay, timeout)
    print(f"توقف: {reason}", file=sys.stderr)
    results: list[Listing] = []
    failed = 0
    for ad_id, text, href, fetched_at in cards:
        item = listing_from_text(text, ad_id, urljoin(BASE_URL, href), fetched_at)
        if item.flags == "parse_failed":
            failed += 1
            if failed <= 3:
                print(f"هشدار: متن کارت قابل تجزیه نبود → {text[:90]!r}", file=sys.stderr)
        results.append(item)
        if details:
            try:
                extract_detail(session, item, timeout)
            except requests.RequestException as exc:
                print(f"جزئیات آگهی {ad_id} دریافت نشد: {exc}", file=sys.stderr)
            time.sleep(delay)
    if failed:
        print(f"{failed} کارت تجزیه نشد (با flag=parse_failed ذخیره شدند؛ بعد از اصلاح parser با --reparse درستشان کن).",
              file=sys.stderr)
    return results


# --------------------------------------------------------------------------
# Query mode: give a list of cars, get only those cars
# --------------------------------------------------------------------------
@dataclass
class CatalogEntry:
    aliases: tuple[str, ...]          # spellings people type (spaces ignored)
    make: str
    family: str                       # must equal the parsed `model` of the listing
    slug: str                         # Bama path: https://bama.ir/car/<slug>
    verified: bool = False            # True = slug seen in a real Bama URL
    trim_slugs: dict | None = None    # optional narrower page, e.g. "تیپ 5" -> "type5"


# Extend freely, or bypass per line with:  شاهین g | saipa-shahin   (or a full URL)
CATALOG: list[CatalogEntry] = [
    CatalogEntry(("پژو پارس", "پارس"), "پژو", "پارس", "peugeot-pars"),
    CatalogEntry(("پژو 206", "206"), "پژو", "206", "peugeot-206ir", verified=True,
                 trim_slugs={f"تیپ {n}": f"type{n}" for n in range(1, 7)}),
    CatalogEntry(("پژو 207", "207"), "پژو", "207", "peugeot-207"),
    CatalogEntry(("شاهین",), "سایپا", "شاهین", "saipa-shahin"),
    CatalogEntry(("تارا",), "ایران خودرو", "تارا", "irankhodro-tara"),
]
TRIM_TYPOS = {"پاناروما": "پانوراما", "پانورما": "پانوراما", "پانوروما": "پانوراما"}


@dataclass
class QuerySpec:
    text: str
    entry: CatalogEntry | None
    override: str | None
    year: int | None                  # Jalali model year
    trim_tokens: list[str]
    gearbox: str | None
    gearbox_type: str | None
    family_override: str | None = None


def parse_query(text: str, catalog: list[CatalogEntry]) -> QuerySpec:
    """'شاهین ۴۰۲ g' -> family شاهین, year 1402, trim G.

    Numbers are read as model YEARS the way Iranians write them:
    97 -> 1397, 402 -> 1402, 404 -> 1404 (or write the full year, e.g. 1402).
    A model number that belongs to the car itself (206, 207) is consumed first.
    """
    raw, _, override = text.partition("|")
    q = f" {normalize_fa(raw)} "
    flat = q.replace(" ", "")
    best: tuple[CatalogEntry, str] | None = None
    for entry in catalog:
        for alias in entry.aliases:
            spaceless = normalize_fa(alias).replace(" ", "")
            if spaceless in flat and (best is None or len(spaceless) > len(best[1])):
                best = (entry, spaceless)
    entry = None
    if best:
        entry, spaceless = best
        q = re.sub(r"\s*".join(map(re.escape, spaceless)), " ", q, count=1)

    year = None
    if m := re.search(r"(?<!\d)(1[34]\d{2})(?!\d)", q):
        year = int(m.group(1))
        q = q[:m.start()] + " " + q[m.end():]
    elif m := re.search(r"(?<!\d)(40\d|[89]\d)(?!\d)", q):
        digits = m.group(1)
        year = 1000 + int(digits) if len(digits) == 3 else 1300 + int(digits)
        q = q[:m.start()] + " " + q[m.end():]

    attrs, leftover = extract_attributes(q)
    tokens = [t.strip(".,،/-") for t in leftover.split()]
    tokens = [_canon_token(TRIM_TYPOS.get(t, t)) for t in tokens if t.strip(".,،/-")]
    family_override = tokens.pop(0) if entry is None and override.strip() and tokens else None
    return QuerySpec(text=text.strip(), entry=entry, override=override.strip() or None, year=year,
                     trim_tokens=tokens, gearbox=attrs.get("gearbox"), gearbox_type=attrs.get("gearbox_type"),
                     family_override=family_override)


def build_urls(spec: QuerySpec) -> list[str]:
    """Candidate pages, most specific first. Only HTTP 404 moves to the next one."""
    if spec.override and spec.override.startswith("http"):
        return [spec.override]
    base = spec.override or (spec.entry.slug if spec.entry else None)
    if not base:
        return []
    suffix = f"-y{spec.year}" if spec.year else ""
    urls = []
    if spec.entry and not spec.override and spec.entry.trim_slugs and spec.trim_tokens:
        trim_slug = spec.entry.trim_slugs.get(" ".join(spec.trim_tokens))
        if trim_slug:
            urls.append(f"{BASE_URL}/car/{base}-{trim_slug}{suffix}")
    urls.append(f"{BASE_URL}/car/{base}{suffix}")
    return urls


def _token_eq(a: str, b: str) -> bool:
    if a.casefold() == b.casefold():
        return True
    if len(a) >= 4 and len(b) >= 4 and not re.search(r"[A-Za-z0-9]", a + b):
        return SequenceMatcher(None, a, b).ratio() >= 0.8       # tolerate typos
    return False


def _family_key(value: str | None) -> str:
    return re.sub(r"^(\d+)i$", r"\1", (value or "").casefold())       # 207i == 207


def match_listing(spec: QuerySpec, item: Listing) -> str | None:
    """Return a short note when the ad IS the requested car, else None."""
    if not item.model or not spec.entry and not spec.override:
        return None
    expected_family = spec.entry.family if spec.entry else spec.family_override
    if expected_family and not any(
        _family_key(value) == _family_key(expected_family) for value in (item.model, item.make)
    ):
        return None
    if spec.year and item.year_jalali != spec.year:
        return None
    if spec.trim_tokens:
        # A query may name only the trim (for example "پانوراما") while Bama's
        # card appends an engine code ("پانوراما TU5P"). Require every query
        # token in order, but allow unmentioned detail tokens in the listing.
        have = (item.trim or "").split()
        next_index = 0
        for wanted in spec.trim_tokens:
            while next_index < len(have) and not _token_eq(wanted, have[next_index]):
                next_index += 1
            if next_index == len(have):
                return None
            next_index += 1
    note = "exact"
    if spec.gearbox == "automatic":
        if item.gearbox != "automatic":
            return None
        if spec.gearbox_type and item.gearbox_type != spec.gearbox_type:
            if item.gearbox_type:
                return None
            note = f"{spec.gearbox_type.lower()}_assumed"
    elif spec.gearbox == "manual":
        if item.gearbox == "automatic":
            return None
        if item.gearbox is None:
            note = "gearbox_unstated"
    return note


def search_queries(session: requests.Session, specs: list[QuerySpec], pages: int, delay: float,
                   timeout: int, details: bool, families: list[str]) -> tuple[list[Listing], list[tuple]]:
    results: dict[str, Listing] = {}
    summary: list[tuple] = []
    for spec in specs:
        urls = build_urls(spec)
        if not urls:
            summary.append((spec.text, "-", 0, 0, "مدل در CATALOG نیست؛ بعد از | slug یا URL بده"))
            continue
        cards, reason, used = [], "", urls[0]
        try:
            for used in urls:
                cards, reason = fetch_cards(session, used, pages, delay, timeout)
                if reason != "HTTP 404":
                    break
                time.sleep(delay)
        except requests.RequestException as exc:
            summary.append((spec.text, used, 0, 0, f"خطای شبکه: {exc}"))
            continue
        kept = 0
        samples: list[str] = []
        for ad_id, text, href, fetched_at in cards:
            if ad_id in results:
                continue
            item = listing_from_text(text, ad_id, urljoin(BASE_URL, href), fetched_at)
            finalize(item, families)
            note = match_listing(spec, item)
            if note is None:
                if len(samples) < 4:
                    samples.append(f"{item.title or text[:50]}  →  {item.variant_key}")
                continue
            item.query, item.match_note = spec.text, note
            if details:
                try:
                    extract_detail(session, item, timeout)
                    finalize(item, families)
                except requests.RequestException as exc:
                    print(f"جزئیات آگهی {ad_id} دریافت نشد: {exc}", file=sys.stderr)
                time.sleep(delay)
            results[ad_id] = item
            kept += 1
        if reason == "HTTP 404":
            reason = "slug پیدا نشد (404)؛ در مرورگر فیلتر کن و URL را بعد از | بگذار"
        if kept == 0 and samples:
            reason += "\n        ردشده‌های نمونه:\n          " + "\n          ".join(samples)
        summary.append((spec.text, used, len(cards), kept, reason))
        time.sleep(delay)
    return list(results.values()), summary


def load_queries(path: str | None, inline: list[str]) -> list[str]:
    lines = list(inline)
    if path:
        lines += Path(path).read_text(encoding="utf-8-sig").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def describe_plan(specs: list[QuerySpec]) -> None:
    for spec in specs:
        gear = "/".join(x for x in (spec.gearbox, spec.gearbox_type) if x) or "-"
        fam = spec.entry.family if spec.entry else "?"
        print(f"• {spec.text}\n    مدل={fam} | سال={spec.year or '-'} | تیپ={' '.join(spec.trim_tokens) or '-'} | گیربکس={gear}")
        urls = build_urls(spec)
        for url in urls or ["(slug ندارد — بعد از | slug یا URL بده)"]:
            mark = "" if spec.override or (spec.entry and spec.entry.verified) else "  [slug تأییدنشده]"
            print(f"    {url}{mark}")


# --------------------------------------------------------------------------
# Offline re-parse of an existing CSV (no network)
# --------------------------------------------------------------------------
DETAIL_FIELDS = ("body_condition", "exterior_color", "interior_color",
                 "detail_gearbox", "detail_fuel", "detail_title")


def reparse_csv(path: Path) -> list[Listing]:
    """Rebuild listings from a CSV made by this or the previous (v1) scraper.

    v2 files carry `raw_card_text`; v1 files carry the card text in `title`
    (everything before the word 'تومان'), which the parser also understands.
    """
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    listings: list[Listing] = []
    for row in rows:
        text = row.get("raw_card_text") or row.get("title") or ""
        item = listing_from_text(text, row["ad_id"], row.get("url", ""), row.get("scraped_at", ""))
        if item.flags == "parse_failed":
            print(f"هشدار: ردیف {row['ad_id']} دوباره تجزیه نشد.", file=sys.stderr)
        for field in DETAIL_FIELDS:
            if row.get(field):
                setattr(item, field, row[field])
        listings.append(item)
    return listings


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def save_json(listings: Iterable[Listing], path: Path) -> None:
    path.write_text(json.dumps([asdict(item) for item in listings], ensure_ascii=False, indent=2),
                    encoding="utf-8")


def save_csv(listings: Iterable[Listing], path: Path) -> None:
    columns = [f.name for f in fields(Listing)]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for item in listings:
            writer.writerow(asdict(item))


def main() -> int:
    parser = argparse.ArgumentParser(description="دریافت آگهی‌های عمومی خودرو از باما")
    parser.add_argument("--url", default=f"{BASE_URL}/car", help="نشانی صفحه خودرو یا صفحه نتایج فیلترشده")
    parser.add_argument("--pages", type=int, default=1, help="حداکثر تعداد صفحات (پارامتر page به URL افزوده می‌شود)")
    parser.add_argument("--delay", type=float, default=1.5, help="مکث بین درخواست‌ها، بر حسب ثانیه")
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--details", action="store_true", help="دریافت جزئیات عمومی هر آگهی؛ درخواست بیشتر می‌فرستد")
    parser.add_argument("--format", choices=("json", "csv"), default="csv")
    parser.add_argument("--output", default=None, help="مسیر خروجی؛ پیش‌فرض bama_listings.csv یا .json")
    parser.add_argument("--underpriced-threshold", type=float, default=0.15,
                        help="حد تخفیف نسبت به میانه آگهی‌های هم‌واریانت برای علامت زیرقیمت، مثل 0.15")
    parser.add_argument("--min-comparables", type=int, default=3, help="حداقل تعداد آگهی هم‌واریانت برای برآورد قیمت")
    parser.add_argument("--min-price", type=int, default=MIN_PRICE_TOMAN_DEFAULT,
                        help="قیمت‌های کمتر از این (تومان) نمادین فرض می‌شوند و در مقایسه نمی‌آیند")
    parser.add_argument("--families-file", default=None,
                        help="فایل JSON شامل لیست نام مدل‌های چندکلمه‌ای، مثل [\"لند کروزر\"]")
    parser.add_argument("--queries-file", default=None,
                        help="فایل متنی، هر خط یک ماشین، مثل «شاهین ۴۰۲ g»؛ فقط همین‌ها جستجو و نگه داشته می‌شن")
    parser.add_argument("--query", action="append", default=[], help="یک ماشین (چند بار قابل تکرار)")
    parser.add_argument("--page-param", default="page", help="نام پارامتر صفحه‌بندی در URL (پیش‌فرض page)")
    parser.add_argument("--dry-run", action="store_true", help="فقط نشان بده هر خط چطور فهمیده شد و چه URLی باز می‌شود")
    parser.add_argument("--reparse", default=None, metavar="CSV",
                        help="بدون اینترنت، یک CSV قبلی را با parser جدید دوباره پردازش کن")
    args = parser.parse_args()
    if args.pages < 1 or args.delay < 0 or not 0 <= args.underpriced_threshold < 1 or args.min_comparables < 1:
        parser.error("pages باید مثبت، delay نامنفی، threshold بین صفر و یک و min-comparables مثبت باشد")

    global PAGE_PARAM
    PAGE_PARAM = args.page_param
    families = list(KNOWN_FAMILIES)
    if args.families_file:
        families += [normalize_fa(f) for f in json.loads(Path(args.families_file).read_text(encoding="utf-8"))]

    query_lines = load_queries(args.queries_file, args.query)
    summary: list[tuple] = []
    if query_lines:
        specs = [parse_query(line, CATALOG) for line in query_lines]
        if args.dry_run:
            describe_plan(specs)
            return 0
        try:
            listings, summary = search_queries(make_session(), specs, args.pages, args.delay,
                                               args.timeout, args.details, families)
        except requests.RequestException as exc:
            print(f"خطا در دریافت صفحه: {exc}", file=sys.stderr)
            return 1
    elif args.reparse:
        listings = reparse_csv(Path(args.reparse))
    else:
        session = make_session()
        try:
            listings = scrape(session, args.url, args.pages, args.delay, args.timeout, args.details)
        except requests.RequestException as exc:
            print(f"خطا در دریافت صفحه: {exc}", file=sys.stderr)
            return 1
    for item in listings:
        finalize(item, families)
    mark_quality(listings, args.min_price)
    estimate_prices(listings, args.underpriced_threshold, args.min_comparables)

    output = Path(args.output or f"bama_listings.{args.format}")
    (save_json if args.format == "json" else save_csv)(listings, output)
    print(f"{len(listings)} آگهی در {output.resolve()} ذخیره شد.")
    analyzed = sum(item.fair_price_toman is not None for item in listings)
    flagged = sum(item.underpriced is True for item in listings)
    print(f"مقایسه قابل محاسبه: {analyzed} | زیرقیمت برآوردی: {flagged}")
    if summary:
        print("\nخلاصه‌ی جستجو (دیده‌شده → نگه‌داشته‌شده):")
        for text, url, seen, kept, reason in summary:
            print(f"  {kept:>3} از {seen:<4} {text}\n        {url}\n        توقف: {reason}")
    counts = Counter(item.variant_key for item in listings if item.variant_key)
    print(f"تعداد واریانت‌های متمایز: {len(counts)}")
    for key, count in counts.most_common(10):
        print(f"  {count:>4}  {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())