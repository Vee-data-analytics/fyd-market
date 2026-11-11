# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests",
#     "beautifulsoup4",
#     "lxml",
# ]
# ///
"""Free Your Dreams - daily market scraper.

Writes out/market.json with auction / retail cars, car price points
(hammer + retail) and sectional-title homes near Cosmo City.

    uv run scrape.py            # real run (network)
    uv run scrape.py --dry      # parse saved fixtures only, run checks
    uv run scrape.py --only aucor,mfc   # subset of sources

Every source is isolated: an exception is recorded in
market.json["sources"][name]["error"] and the run continues.
"""
from __future__ import annotations

import argparse
import hashlib
import html as htmlmod
import json
import re
import sys
import time
import traceback
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
FIX = ROOT / "fixtures"

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
TIMEOUT = 30
RETRIES = 2
MIN_DELAY = 1.0            # seconds between requests to one host (robots Crawl-delay wins if larger)
MAX_REQUESTS = 300         # hard cap per run

YEAR_MIN, YEAR_MAX = 2016, 2025
PRICE_MIN, PRICE_MAX = 40_000, 80_000          # for bids / estimates / asking prices
RETAIL_GUIDE_MAX = 130_000   # MFC lots only carry a *retail guide*; bank cars usually
                             # hammer well below retail, so allow guides up to this
WBC_RETAIL_MAX = 120_000     # retail comparables go a bit above the bid band on purpose

ALLOWED_MAKES = ["Toyota", "Honda", "Nissan", "Mazda", "Suzuki", "Mitsubishi", "Subaru",
                 "Isuzu", "Daihatsu", "Lexus", "Hyundai", "Kia", "Volkswagen"]

HOME_PRICE_MAX = 700_000
HOME_BEDS_MIN = 2
HOME_MAX = 150
PP_DETAIL_MAX = 40          # Private Property detail pages fetched for levy/rates/size

# Private Property suburb ids (from their residential-shapes sitemap)
PP_SUBURBS = {
    "Honeydew Grove": "west-rand/roodepoort/honeydew-grove/7780",
    "Honeydew Ridge": "west-rand/roodepoort/honeydew-ridge/2990",
    "Honeydew": "west-rand/roodepoort/honeydew/915",
    "Wilgeheuwel": "west-rand/roodepoort/wilgeheuwel/286",
    "Weltevreden Park": "west-rand/roodepoort/weltevreden-park/280",
    "Radiokop": "west-rand/roodepoort/radiokop/281",
    "Northgate": "johannesburg/randburg/northgate/1492",
    "North Riding": "johannesburg/randburg/north-riding/771",
    "Northwold": "johannesburg/randburg/northwold/379",
    "Olivedale": "johannesburg/randburg/olivedale/1143",
    "Sundowner": "johannesburg/randburg/sundowner/770",
    "Randpark Ridge": "johannesburg/randburg/randpark-ridge/259",
}
# Property24 suburbs: exactly the ones in your own Property24 search (38 suburbs,
# houses/flats/townhouses, R700k max, 2+ beds). The advanced-search page itself is
# off-limits to bots (robots.txt), so each suburb is read from its own page.
P24_SUBURBS = {
    "Randpark Ridge": "randpark-ridge/randburg/gauteng/4471",
    "Weltevreden Park": "weltevreden-park/roodepoort/gauteng/1593",
    "Laser Park": "laser-park/roodepoort/gauteng/12860",
    "North Riding AH": "north-riding-ah/randburg/gauteng/4456",
    "Honeypark": "honeypark/roodepoort/gauteng/14262",
    "Eagle Canyon Golf Estate": "eagle-canyon-golf-estate/roodepoort/gauteng/10926",
    "Sundowner": "sundowner/randburg/gauteng/4298",
    "Northwold": "northwold/randburg/gauteng/4282",
    "Boskruin": "boskruin/randburg/gauteng/4417",
    "Northgate": "northgate/randburg/gauteng/4457",
    "Sharonlea": "sharonlea/randburg/gauteng/4296",
    "Boundary Park": "boundary-park/randburg/gauteng/33215",
    "Rietfontein AH": "rietfontein-ah/krugersdorp/gauteng/976",
    "Nooitgedacht": "nooitgedacht/krugersdorp/gauteng/1019",
    "Lion Pride": "lion-pride/randburg/gauteng/33455",
    "Mostyn Park": "mostyn-park/roodepoort/gauteng/5196",
    "Cosmo Business Park": "cosmo-business-park/roodepoort/gauteng/17765",
    "Craigavon": "craigavon/sandton/gauteng/3913",
    "Glen Nerine": "glen-nerine/sandton/gauteng/5168",
    "Salfred": "salfred/sandton/gauteng/5175",
    "Brendavere": "brendavere/sandton/gauteng/17131",
    "Noordhang": "noordhang/randburg/gauteng/4278",
    "Hoogland": "hoogland/randburg/gauteng/12166",
    "North Riding": "north-riding/randburg/gauteng/4455",
    "Fourways": "fourways/sandton/gauteng/5811",
    "Olivedale": "olivedale/randburg/gauteng/4294",
    "Chartwell": "chartwell/randburg/gauteng/4424",
    "Farmall AH": "farmall-ah/randburg/gauteng/4429",
    "Alsef AH": "alsef-ah/roodepoort/gauteng/658",
    "Honeydew Grove": "honeydew-grove/roodepoort/gauteng/12989",
    "Ambot": "ambot/roodepoort/gauteng/17152",
    "Honeydew Manor": "honeydew-manor/roodepoort/gauteng/10959",
    "Honeydew Ridge": "honeydew-ridge/roodepoort/gauteng/819",
    "Harveston AH": "harveston-ah/roodepoort/gauteng/32969",
    "Radiokop": "radiokop/roodepoort/gauteng/756",
    "Honeydew": "honeydew/roodepoort/gauteng/716",
    "Kimbult AH": "kimbult-ah/roodepoort/gauteng/724",
    "Wilgeheuwel": "wilgeheuwel/roodepoort/gauteng/837",
}
TARGET_SUBURBS = sorted(set(PP_SUBURBS) | set(P24_SUBURBS))
# Never list these, whatever a site pads its results with.
EXCLUDED_SUBURBS = re.compile(r"bloubos|cosmo city", re.I)

# --------------------------------------------------------------------------
# HTTP helper: polite, robots-aware, retrying, capped
# --------------------------------------------------------------------------
class Blocked(Exception):
    pass


class Http:
    def __init__(self, max_requests: int = MAX_REQUESTS):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-ZA,en;q=0.9"})
        self.robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.last: dict[str, float] = {}
        self.count = 0
        self.max = max_requests

    def _robots(self, url: str):
        p = urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        if host not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(host + "/robots.txt", timeout=TIMEOUT)
                self.count += 1
                if r.status_code == 200 and "html" not in r.headers.get("content-type", ""):
                    rp.parse(r.text.splitlines())
                else:
                    rp = None          # no robots.txt -> everything allowed
            except requests.RequestException:
                rp = None
            self.robots[host] = rp
            self.last[p.netloc] = time.monotonic()
        return self.robots[host]

    def delay_for(self, url: str) -> float:
        rp = self._robots(url)
        cd = None
        if rp is not None:
            try:
                cd = rp.crawl_delay("*")
            except Exception:
                cd = None
        return max(MIN_DELAY, float(cd or 0))

    def allowed(self, url: str) -> bool:
        rp = self._robots(url)
        return True if rp is None else rp.can_fetch("*", url)

    def request(self, method: str, url: str, **kw) -> requests.Response:
        if not self.allowed(url):
            raise Blocked(f"robots.txt disallows {url}")
        host = urlparse(url).netloc
        wait = self.delay_for(url) - (time.monotonic() - self.last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        err = None
        for attempt in range(RETRIES + 1):
            if self.count >= self.max:
                raise Blocked(f"request cap {self.max} reached")
            self.count += 1
            try:
                r = self.s.request(method, url, timeout=TIMEOUT, **kw)
                self.last[host] = time.monotonic()
                if r.status_code in (429, 500, 502, 503, 504):
                    err = f"HTTP {r.status_code}"
                    time.sleep(3 * (attempt + 1))
                    continue
                return r
            except requests.RequestException as e:
                self.last[host] = time.monotonic()
                err = f"{type(e).__name__}: {e}"
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"{method} {url} failed after {RETRIES + 1} tries: {err}")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


# --------------------------------------------------------------------------
# Normalisers
# --------------------------------------------------------------------------
def sid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def money(text) -> float | None:
    """'R 699 000' / 'R1,199,900' / '52500.00' -> float."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text else None
    t = htmlmod.unescape(str(text)).replace("\xa0", " ")
    m = re.search(r"\d[\d ,]*(?:\.\d+)?", t)
    if not m:
        return None
    try:
        v = float(m.group(0).replace(" ", "").replace(",", ""))
    except ValueError:
        return None
    return v or None


def to_int(text) -> int | None:
    v = money(text)
    return int(v) if v is not None else None


def clean_km(v) -> int | None:
    """Catalogues use 1 / 111 / 11111 / 1111111111 as 'unknown' placeholders."""
    k = to_int(v)
    if k is None or k < 500 or k > 1_500_000 or re.fullmatch(r"1+", str(k)):
        return None
    return k


MAKE_ALIASES = [
    (r"VOLKSWAGEN|\bVW\b", "Volkswagen"),
    (r"MERCEDES[- ]?BENZ|M/BENZ|MERCEDES|\bMB\b", "Mercedes-Benz"),
    (r"LAND ROVER|RANGE ROVER", "Land Rover"),
    (r"ALFA ROMEO", "Alfa Romeo"),
    (r"GREAT WALL|\bGWM\b", "GWM"),
    (r"ROLLS[- ]ROYCE", "Rolls-Royce"),
    (r"ASTON MARTIN", "Aston Martin"),
]
SINGLE_MAKES = ["TOYOTA", "HONDA", "NISSAN", "MAZDA", "SUZUKI", "MITSUBISHI", "SUBARU", "ISUZU",
                "DAIHATSU", "LEXUS", "HYUNDAI", "KIA", "BMW", "AUDI", "PORSCHE", "FORD", "CHEVROLET",
                "OPEL", "RENAULT", "PEUGEOT", "CITROEN", "CHERY", "HAVAL", "BAIC", "JAC", "BYD", "JETOUR",
                "OMODA", "MAHINDRA", "TATA", "PROTON", "FIAT", "JEEP", "DODGE", "VOLVO", "MINI", "JAGUAR",
                "MASERATI", "FERRARI", "LAMBORGHINI", "DATSUN", "CHANA", "FOTON", "JMC", "SHACMAN",
                "HINO", "UD", "SCANIA", "MAN", "IVECO", "DAF", "FAW", "SINOTRUK", "LDV", "GAC", "PROTON",
                "CUPRA", "SEAT", "SKODA", "SSANGYONG", "TESLA", "DFSK", "KAIYI", "MG", "INFINITI",
                "POLESTAR", "SMART", "BENTLEY", "HARLEY", "YAMAHA", "KAWASAKI", "DUCATI", "TRIUMPH"]


def norm_make(text: str) -> str | None:
    up = (text or "").upper()
    for pat, name in MAKE_ALIASES:
        if re.search(pat, up):
            return name
    for mk in SINGLE_MAKES:
        if re.search(rf"\b{re.escape(mk)}\b", up):
            if mk == "KIA":
                return "Kia"
            return mk.title() if len(mk) > 3 else mk
    return None


MULTI_WORD_MODELS = ["COROLLA QUEST", "COROLLA CROSS", "URBAN CRUISER", "LAND CRUISER", "HILUX VIGO",
                     "POLO VIVO", "GRAND I10", "GRAND i10", "QUANTUM HIACE", "RUMION", "STARLET",
                     "T-CROSS", "T-ROC", "UP!", "D-MAX", "MU-X", "X-TRAIL", "CX-3", "CX-5", "CX-30",
                     "BT-50", "ECLIPSE CROSS", "CR-V", "HR-V", "WR-V", "BR-V", "GRAND VITARA", "S-PRESSO",
                     "CELERIO", "SWIFT DZIRE", "XV", "FORESTER", "OUTBACK", "IMPREZA", "PAJERO SPORT",
                     "GOLF GTI", "AMAROK", "NP200", "NP300", "NAVARA", "TRITON"]
BAKKIE_HINTS = r"\b(HILUX|NAVARA|NP200|NP300|HARDBODY|D-MAX|KB\s?\d*|TRITON|AMAROK|BT-50|RANGER|BAKKIE|P/U|D/C|S/C|E/CAB|X/CAB|SUPER CAB|DOUBLE CAB|SINGLE CAB|CHASSIS CAB|D/S|DROPSIDE)\b"


def parse_vehicle_title(title: str) -> dict:
    """'2019 MAZDA 3 1.6 ORIGINAL 5DR M/T (NON-RUNNER)' -> year/make/model/variant/..."""
    t = re.sub(r"\s+", " ", htmlmod.unescape(title or "")).strip()
    up = t.upper()
    out = {"year": None, "make": None, "model": None, "variant": None,
           "transmission": None, "fuel": None, "bodyType": None}
    m = re.search(r"\b(19[89]\d|20[0-3]\d)\b", t)
    if m:
        out["year"] = int(m.group(1))
    make = norm_make(t)
    out["make"] = make
    if make:
        # text after the make token
        pat = {"Volkswagen": r"VOLKSWAGEN|\bVW\b", "Mercedes-Benz": r"MERCEDES[- ]?BENZ|M/BENZ|MERCEDES"}.get(
            make, re.escape(make.upper()))
        mm = re.search(pat, up)
        rest = t[mm.end():].strip(" -") if mm else t
        rest = re.sub(r"\(.*?\)", "", rest).strip()
        rest_up = rest.upper()
        model = None
        for mw in MULTI_WORD_MODELS:
            if rest_up.startswith(mw.upper()):
                model = rest[:len(mw)]
                break
        if not model and rest:
            model = rest.split()[0]
        if model:
            out["model"] = model.title() if not re.search(r"\d|-", model) else model.upper()
            out["variant"] = rest[len(model):].strip(" -") or None
    if re.search(r"\b(A/T|AUTO|AUTOMATIC|DSG|CVT|TIP|TIPTRONIC|DCT|AMT|STEPTRONIC|EDC)\b", up):
        out["transmission"] = "Automatic"
    elif re.search(r"\b(M/T|MANUAL)\b", up):
        out["transmission"] = "Manual"
    if re.search(r"\b(TDI|D-4D|GD-6|GD6|DCI|CRDI|CDI|D4D|TD|DIESEL|\dD\b|D-MAX|DTEC|I-DTEC|SKYACTIV-D|TDCI|KB2\d\dD|DDIS)\b", up) \
            or re.search(r"\d\.\dD\b", up):
        out["fuel"] = "Diesel"
    elif re.search(r"\b(HYBRID|HEV|PHEV)\b", up):
        out["fuel"] = "Hybrid"
    elif re.search(r"\b(EV|ELECTRIC)\b", up):
        out["fuel"] = "Electric"
    if re.search(BAKKIE_HINTS, up):
        out["bodyType"] = "Bakkie"
    elif re.search(r"\b(QUANTUM|HIACE|SESFIKILE|PANEL VAN|P/V|F/C)\b", up):
        out["bodyType"] = "Van/Minibus"
    return out


CONDITION_PATTERNS = [
    (r"NON[- ]?RUNNER|NOT RUNNING|DOES NOT START|DOESN.T START", "non-runner"),
    (r"ACCIDENT|DAMAGE|DAAMAGE|WRITE[- ]?OFF|CODE 3|SALVAGE", "accident damaged"),
    (r"FLOOD", "flood damaged"),
    (r"HAIL", "hail damage"),
    (r"NO KEYS?|WITHOUT KEYS?", "no keys"),
    (r"\b(\d|ONE|TWO|1X|2X)\s*KEYS?\b|NO\. OF KEYS\W+(\d|1X|2X)|SPARE KEY", "keys"),
    (r"NO (NATIS|PAPERS|REGISTRATION)|SOLD WITH NO NATIS|WITHOUT PAPERS", "no papers"),
    (r"NATIS|REGISTRATION PAPERS|\bPAPERS\b", "papers"),
    (r"ENGINE LIGHT|ENGINE (NOISE|KNOCK|FAULT|DAMAGE)", "engine issue"),
    (r"GEARBOX (FAULT|ISSUE|DAMAGE)", "gearbox issue"),
    (r"WINDSCREEN CRACKED|CRACKED WINDSCREEN", "cracked windscreen"),
    (r"REPOSSESS|\bREPO\b", "repossessed"),
    (r"REG 1ST MONTH", "reg 1st month"),
    (r"SEVERE ABUSE|SEVER ABUSE", "signs of severe abuse"),
]


def condition_notes(*texts) -> str | None:
    up = " ".join(htmlmod.unescape(t or "") for t in texts).upper()
    notes = []
    for pat, label in CONDITION_PATTERNS:
        if re.search(pat, up) and label not in notes:
            if label == "keys" and "no keys" in notes:
                continue
            if label == "papers" and "no papers" in notes:
                continue
            notes.append(label)
    return ", ".join(notes) or None


REGION_HINTS = [
    (r"GAUTENG|JOHANNESBURG|\bJHB\b|JOBURG|PRETORIA|TSHWANE|CENTURION|KOSMOSDAL|MIDRAND|ROODEPOORT|RANDBURG|"
     r"KEMPTON|GERMISTON|BOKSBURG|BENONI|SANDTON|SOWETO|ALBERTON|LANGLAAGTE|ROBERTVILLE|LEA GLEN|EAST RAND|WEST RAND|"
     r"BURCHMORES|SPRINGS|VEREENIGING|KRUGERSDORP|MFC AUCTION HOUSE GAUTENG", "Gauteng"),
    (r"KWAZULU|DURBAN|\bKZN\b|PINETOWN|WESTVILLE|AVOCA|PIETERMARITZBURG|RICHARDS BAY", "KwaZulu-Natal"),
    (r"WESTERN CAPE|CAPE TOWN|\bCPT\b|EPPING|BELLVILLE|STELLENBOSCH|\(CAPE\)|WH AUCTIONEERS", "Western Cape"),
    (r"EASTERN CAPE|PORT ELIZABETH|GQEBERHA|\bPE\b|EAST LONDON|KUGOMPO|MTHATHA", "Eastern Cape"),
    (r"FREE STATE|BLOEMFONTEIN|\bBFN\b|WELKOM", "Free State"),
    (r"LIMPOPO|POLOKWANE", "Limpopo"),
    (r"MPUMALANGA|NELSPRUIT|MBOMBELA|WITBANK|EMALAHLENI|MIDDELBURG|SECUNDA", "Mpumalanga"),
    (r"NORTH WEST|RUSTENBURG|MAHIKENG|KLERKSDORP|POTCHEFSTROOM", "North West"),
    (r"NORTHERN CAPE|KIMBERLEY|UPINGTON", "Northern Cape"),
]


def region_of(*texts) -> str | None:
    up = " ".join(t or "" for t in texts).upper()
    for pat, name in REGION_HINTS:
        if re.search(pat, up):
            return name
    return None


def text_of(html: str) -> str:
    t = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", html or "", flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t\r\f\v]+", " ", htmlmod.unescape(t)).strip()


def make_car(source, url, title, *, location=None, region=None, auctionDate=None, auctionName=None,
             priceKind="none", price=None, image=None, km=None, fuel=None, transmission=None,
             notes=None, make=None, model=None, variant=None, year=None, bodyType=None, extra=None) -> dict:
    p = parse_vehicle_title(title)
    car = {
        "id": sid(source, url),
        "source": source,
        "url": url,
        "title": re.sub(r"\s+", " ", title or "").strip(),
        "make": make or p["make"],
        "model": model or p["model"],
        "variant": variant if variant is not None else p["variant"],
        "year": year or p["year"],
        "km": km,
        "fuel": fuel or p["fuel"],
        "transmission": transmission or p["transmission"],
        "location": location,
        "region": region or region_of(location, auctionName) or "Unknown",
        "auctionDate": auctionDate,
        "auctionName": auctionName,
        "priceKind": priceKind if price else "none",
        "price": price or None,
        "image": image,
        "condition": notes,
        "bodyType": bodyType or p["bodyType"],
    }
    if extra:
        car.update(extra)
    return car


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------
def car_reject_reason(c: dict) -> str | None:
    if not c.get("year") or not c.get("make"):
        return "not a vehicle / unparseable"
    if c["make"] not in ALLOWED_MAKES:
        return "make"
    if not (YEAR_MIN <= c["year"] <= YEAR_MAX):
        return "year"
    price, kind = c.get("price"), c.get("priceKind")
    if price:
        if kind == "retail":
            if not (PRICE_MIN <= price <= RETAIL_GUIDE_MAX):
                return "price"
        elif not (PRICE_MIN <= price <= PRICE_MAX):
            return "price"
    return None


def filter_cars(cars: list[dict]) -> tuple[list[dict], dict]:
    kept, why = [], {}
    for c in cars:
        r = car_reject_reason(c)
        if r:
            why[r] = why.get(r, 0) + 1
        else:
            kept.append(c)
    return kept, why


def keep_pricepoint(p: dict) -> bool:
    return (p.get("make") in ALLOWED_MAKES and p.get("year") and YEAR_MIN <= p["year"] <= YEAR_MAX
            and p.get("price"))


def home_reject_reason(h: dict) -> str | None:
    if not h.get("price") or h["price"] > HOME_PRICE_MAX:
        return "price"
    if h.get("beds") is None or h["beds"] < HOME_BEDS_MIN:
        return "beds"
    if h.get("type") not in ("sectional", "freehold", "new development"):  # houses, flats, townhouses (as your search)
        return "type"
    if EXCLUDED_SUBURBS.search((h.get("suburb") or "") + " " + (h.get("title") or "")):
        return "excluded suburb"
    return None


def home_type(title: str) -> str | None:
    t = (title or "").lower()
    if re.search(r"apartment|flat|townhouse|cluster|duplex|simplex|penthouse|studio|sectional|unit", t):
        return "sectional"
    if re.search(r"house|home", t):
        return "freehold"
    if re.search(r"land|plot|stand|farm|commercial|industrial|office", t):
        return "other"
    return None


# --------------------------------------------------------------------------
# Source: Auction Operation (AO)
# --------------------------------------------------------------------------
AO_APP = "https://www.app.auctionoperation.co.za"


def parse_ao_upcoming(html: str) -> list[dict]:
    """Auction cards on app.auctionoperation.co.za/home/auctions/upcoming."""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.select('a[href*="/auction/lots/layout"]'):
        m = re.search(r"auctionId=([A-Za-z0-9=]+)", a["href"])
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        card = a
        for _ in range(8):  # climb to the card that holds the title/date text
            if card.parent is None:
                break
            card = card.parent
            txt = card.get_text(" ", strip=True)
            if "Starting:" in txt and "Lots" in txt:
                break
        txt = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
        name = re.search(r"([A-Z0-9][^·]*?) Starting:", txt)
        start = re.search(r"Starting: (\w{3} \d{1,2}, \d{4} at \d{1,2}:\d{2} [AP]M)", txt)
        loc = re.search(r"[AP]M · (.*?) Register", txt)
        lots = re.search(r"(\d+) Lots", txt)
        try:
            iso = datetime.strptime(start.group(1), "%b %d, %Y at %I:%M %p").isoformat() if start else None
        except ValueError:
            iso = None
        title = name.group(1).strip() if name else None
        if title:
            title = re.sub(r"^(?:\w{3} \d{1,2} \w{3} )+", "", title).strip()
        out.append({
            "auctionId": m.group(1),
            "name": title,
            "start": iso,
            "location": loc.group(1).split(",")[0].strip() if loc else None,
            "lots": int(lots.group(1)) if lots else None,
            "url": AO_APP + "/auction/lots/layout?page=0&auctionId=" + m.group(1),
        })
    return out


def scrape_auction_operation(http: Http, ctx: dict) -> dict:
    r = http.get(AO_APP + "/home/auctions/upcoming?menu-item=upcoming&status=upcoming&page=0")
    r.raise_for_status()
    auctions = [a for a in parse_ao_upcoming(r.text)
                if a["name"] and re.search(r"vehicle|auto|car|wesbank|repo|fleet|salvage|runner", a["name"], re.I)]
    # The lot grid is filled client-side by POST /auction/lots/search/by/title, which
    # app.auctionoperation.co.za/robots.txt disallows ("Disallow: /auction/lots/search/").
    lot_api = AO_APP + "/auction/lots/search/by/title"
    note = None if http.allowed(lot_api) else (
        "lot list API /auction/lots/search/ is disallowed by robots.txt - only auction-level data collected")
    for a in auctions:
        a["region"] = region_of(a["location"], a["name"]) or "Gauteng"
    return {"cars": [], "raw": 0, "auctions": auctions, "note": note}


# --------------------------------------------------------------------------
# Source: Aucor (live.aucor.com, AuctionMobility platform)
# --------------------------------------------------------------------------
AUCOR_LIVE = "https://live.aucor.com"
AUCOR_VEHICLE_RE = re.compile(r"vehicle|bank|repo|de-?fleet|wesbank|motor|\bcars?\b(?! accessor)", re.I)
AUCOR_EXCLUDE_RE = re.compile(r"open box|accessor|mining|truck auction(?!.*vehicle)|warehouse|yard", re.I)


def parse_aucor_auction_list(html: str) -> list[dict]:
    """auction-summary objects embedded in a live.aucor.com page (viewVars JSON)."""
    out, seen = [], set()
    dec = json.JSONDecoder()
    for m in re.finditer(r'\{"row_id":"(4-[A-Z0-9]+)","type":"auction-summary"', html):
        if m.group(1) in seen:
            continue
        try:
            obj, _ = dec.raw_decode(html, m.start())
        except json.JSONDecodeError:
            continue
        seen.add(m.group(1))
        out.append(obj)
    return out


def aucor_is_vehicle_auction(a: dict) -> bool:
    t = a.get("title") or ""
    return bool(AUCOR_VEHICLE_RE.search(t)) and not AUCOR_EXCLUDE_RE.search(t)


def _aucor_bid(lot: dict) -> float | None:
    for k in ("timed_auction_bid", "highest_live_bid"):
        v = lot.get(k)
        if isinstance(v, dict) and v.get("amount"):
            return money(v["amount"])
    return None


def parse_aucor_lots(data: dict, auction: dict | None = None) -> tuple[list[dict], list[dict], int]:
    """/ajax/lots/<id> JSON -> (cars on offer, hammer price points, raw lot count)."""
    cars, points = [], []
    lots = data.get("result_page") or []
    for lot in lots:
        a = lot.get("auction") or auction or {}
        title = lot.get("title") or ""
        desc = text_of(lot.get("truncated_description") or "")
        p = parse_vehicle_title(title)
        url = AUCOR_LIVE + (lot.get("_detail_url") or f"/lots/view/{lot.get('row_id')}")
        km = None
        mk = re.search(r"Mileage\W+([\d ,]{3,})", desc, re.I)
        if mk:
            km = clean_km(mk.group(1))
        loc = (auction or {}).get("location_name") or a.get("location_name")
        auction_title = a.get("title") or (auction or {}).get("title")
        region = region_of(auction_title, loc)
        status = lot.get("status")
        bid = _aucor_bid(lot)
        sold = money(lot.get("sold_price"))
        if status == "sold" and (sold or bid) and p["year"] and p["make"]:
            points.append({
                "source": "aucor", "make": p["make"], "model": p["model"], "year": p["year"], "km": km,
                "price": sold or bid, "kind": "hammer",
                "date": (a.get("effective_end_time") or a.get("time_start_live_auction") or a.get("time_start") or "")[:10] or None,
                "title": title, "url": url, "region": region,
                "condition": condition_notes(title, lot.get("condition")),
            })
            continue
        if status not in (None, "active", "upcoming", "open"):
            continue  # expired / withdrawn / subject_to: not buyable any more
        start = money(lot.get("starting_price"))
        est = money(lot.get("estimate_low"))
        if bid:
            kind, price = "current bid", bid
        elif start:
            kind, price = "starting bid", start
        elif est:
            kind, price = "estimate", est
        else:
            kind, price = "none", None
        cars.append(make_car(
            "aucor", url, title, location=loc, region=region,
            auctionDate=a.get("time_start_live_auction") or a.get("time_start"),
            auctionName=auction_title, priceKind=kind, price=price,
            image=lot.get("cover_thumbnail"), km=km,
            notes=condition_notes(title, lot.get("condition"), desc),
            extra={"lotNumber": lot.get("lot_number")},
        ))
    return cars, points, len(lots)


def scrape_aucor(http: Http, ctx: dict, max_pages_per_auction: int = 8, max_auctions: int = 6) -> dict:
    hdr = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
    auctions: dict[str, dict] = {}
    for path in ("/", "/auctions/past"):
        r = http.get(AUCOR_LIVE + path)
        r.raise_for_status()
        for a in parse_aucor_auction_list(r.text):
            auctions.setdefault(a["row_id"], a)
    veh = [a for a in auctions.values() if aucor_is_vehicle_auction(a)]
    now = datetime.now(timezone.utc).isoformat()
    upcoming = [a for a in veh if (a.get("effective_end_time") or a.get("time_start") or "") >= now[:10]]
    past = sorted([a for a in veh if a not in upcoming and (a.get("sold_lot_count") or 0) > 0],
                  key=lambda a: a.get("time_start") or "", reverse=True)
    chosen = upcoming[:max_auctions] + past[:2]
    cars, points, raw = [], [], 0
    for a in chosen:
        page = 1
        while page <= max_pages_per_auction:
            r = http.get(f"{AUCOR_LIVE}/ajax/lots/{a['row_id']}?page={page}", headers=hdr)
            r.raise_for_status()
            data = r.json()
            c, p, n = parse_aucor_lots(data, a)
            cars += c
            points += p
            raw += n
            if not (data.get("query_info") or {}).get("next_page") or n == 0:
                break
            page += 1
    summary = [{"id": a["row_id"], "title": a.get("title"), "start": a.get("time_start"),
                "lots": a.get("lot_count"), "sold": a.get("sold_lot_count"),
                "url": AUCOR_LIVE + (a.get("_detail_url") or "")} for a in chosen]
    return {"cars": cars, "points": points, "raw": raw, "auctions": summary}


# --------------------------------------------------------------------------
# Source: MFC (Nedbank) auction catalogue JSON
# --------------------------------------------------------------------------
MFC_API = "https://onlineforms.nedsecure.co.za/mfc-dealer/nedbank/mfc-servicing/auctions"
MFC_PAGE = "https://www.mfc.co.za/home/auction-calendar.html"


def parse_mfc(data: dict) -> tuple[list[dict], list[dict], int]:
    rows = data.get("data") or []
    cars, points = [], []
    for x in rows:
        make = norm_make(x.get("varMake") or "") or (x.get("varMake") or "").title()
        title = f"{x.get('varYear', '')} {x.get('varMake', '')} {x.get('varModel', '')}".strip()
        p = parse_vehicle_title(title)
        year = to_int(x.get("varYear"))
        km = clean_km(x.get("intMileage"))
        retail = money(x.get("decRetail"))
        try:
            adate = datetime.strptime(x.get("sdtAuction", ""), "%d %b %Y").date().isoformat()
        except ValueError:
            adate = None
        house = x.get("varAuctionLocation")
        site = x.get("varWebsite")
        url = ("https://" + site.lstrip("/")) if site and not site.startswith("http") else (site or MFC_PAGE)
        key = sid("mfc", house, adate, title, x.get("intMileage"), x.get("decRetail"))
        region = region_of(house, x.get("varRegion"), x.get("varAddress"))
        car = make_car("mfc", url, title, location=x.get("varAddress"), region=region,
                       auctionDate=adate, auctionName=house, priceKind="retail", price=retail,
                       km=km, make=make, year=year,
                       notes=condition_notes(x.get("varModel")),
                       extra={"catalogueRef": key, "phone": x.get("varTelephoneNumber"),
                              "gps": x.get("varGPSCoordinates")})
        car["id"] = sid("mfc", key)
        cars.append(car)
        if retail and year:
            points.append({"source": "mfc", "make": make, "model": p["model"], "year": year, "km": km,
                           "price": retail, "kind": "retail", "date": adate, "title": title,
                           "url": MFC_PAGE, "region": region,
                           "note": "MFC catalogue retail guide value (not a sale price)"})
    return cars, points, len(rows)


def scrape_mfc(http: Http, ctx: dict) -> dict:
    # The API intermittently answers R00 with an empty list (seen with extra
    # Origin/Accept headers); plain GET + one retry is reliable.
    data = {}
    for attempt in range(2):
        r = http.get(MFC_API, headers={"Referer": MFC_PAGE})
        r.raise_for_status()
        data = r.json()
        if data.get("data"):
            break
        time.sleep(5)
    if not data.get("data"):
        raise RuntimeError(f"MFC API returned no rows (responseCode={data.get('responseCode')})")
    cars, points, raw = parse_mfc(data)
    return {"cars": cars, "points": points, "raw": raw}


# --------------------------------------------------------------------------
# Source: WeBuyCars (elastic search API used by the site)
# --------------------------------------------------------------------------
WBC_API = "https://appgateway.webuycars.co.za/website-elastic-backend/api/search"
WBC_SITE = "https://www.webuycars.co.za"


def wbc_body(offset: int, size: int, filter_type: str, price_lo=0, price_hi=0) -> dict:
    return {"to": offset, "size": size, "type": None, "filter_type": filter_type, "subcategory": None,
            "hubSummary": False, "q": "", "Make": ALLOWED_MAKES,
            "Priced_Amount_Gte": price_lo, "Priced_Amount_Lte": price_hi,
            "Year_Gte": YEAR_MIN, "Year_Lte": YEAR_MAX, "Kilometers_Gte": 0, "Kilometers_Lte": 0,
            "Fuel_Consumption_Gte": 0, "Fuel_Consumption_Lte": 0,
            "MonthlyInstallment_Amount_Gte": 0, "MonthlyInstallment_Amount_Lte": 0}


def wbc_url(x: dict) -> str:
    from urllib.parse import quote
    return f"{WBC_SITE}/buy-a-car/{quote(x.get('Make') or '')}/{quote(x.get('Model') or '')}/{x.get('StockNumber')}"


def wbc_image(x: dict) -> str | None:
    imgs = x.get("Images") or {}
    ext = imgs.get("external") or []
    return ext[0] if ext else imgs.get("internal")


def parse_wbc_retail(data: dict) -> list[dict]:
    pts = []
    for x in data.get("data") or []:
        price = money(x.get("Price") or x.get("BuyNowPrice"))
        if not price:
            continue
        pts.append({"source": "webuycars", "make": norm_make(x.get("Make") or "") or x.get("Make"),
                    "model": x.get("Model"), "year": x.get("Year"), "km": clean_km(x.get("Mileage")),
                    "price": price, "kind": "retail", "date": (x.get("LastPriceUpdateDate") or "")[:10] or None,
                    "title": x.get("Description"), "url": wbc_url(x), "region": x.get("Province"),
                    "variant": x.get("Variant"), "bodyType": x.get("BodyType")})
    return pts


def real_date(v):
    """WBC uses 0001-01-01 as 'not scheduled yet'."""
    return v if v and not str(v).startswith(("0001", "1900")) else None


def parse_wbc_auction(data: dict) -> list[dict]:
    cars = []
    for x in data.get("data") or []:
        auc = x.get("auction") or {}
        start = money(auc.get("startingPrice") or x.get("startingPrice") or x.get("OpeningBid"))
        notes = condition_notes(x.get("Condition"), x.get("PricingGrade"))
        extra_notes = []
        if x.get("lotCategory") == "Repossession":
            extra_notes.append("repossessed")
        if x.get("HasSpareKey"):
            extra_notes.append("spare key")
        if x.get("RoadworthyRelevantIssues"):
            extra_notes.append(f"{x['RoadworthyRelevantIssues']} roadworthy issues")
        if x.get("Condition"):
            extra_notes.append(x["Condition"].lower())
        allnotes = ", ".join(dict.fromkeys([n for n in (notes or "").split(", ") if n] + extra_notes)) or None
        cars.append(make_car(
            "webuycars_auction", wbc_url(x), x.get("Description") or "",
            location=x.get("DealerKey"), region=x.get("Province"),
            auctionDate=real_date(auc.get("expirationDateTime")) or real_date(x.get("auctionEndDate"))
            or real_date(x.get("auctionStartDateTime")),
            auctionName=f"WeBuyCars online auction ({x.get('lotCategory') or 'lot'})",
            priceKind="starting bid", price=start, image=wbc_image(x),
            km=clean_km(x.get("Mileage")), fuel=x.get("FuelType"), transmission=x.get("Gearbox"),
            notes=allnotes, make=norm_make(x.get("Make") or "") or x.get("Make"), model=x.get("Model"),
            variant=x.get("Variant"), year=x.get("Year"),
            bodyType="Bakkie" if (x.get("BodyType") or "").lower() in ("bakkie", "pick-up", "pickup",
                                                                        "double cab", "single cab") else x.get("BodyType"),
            extra={"bids": x.get("totalNumberOfBids"), "reserveMet": x.get("reservePriceMet"),
                   "auctionEnds": real_date(auc.get("expirationDateTime"))},
        ))
    return cars


def _wbc_pages(http: Http, filter_type: str, price_lo=0, price_hi=0, size=100, max_pages=5) -> list[dict]:
    hdr = {"Content-Type": "application/json", "Origin": WBC_SITE, "Referer": WBC_SITE + "/",
           "Accept": "application/json"}
    rows, offset = [], 0
    for _ in range(max_pages):
        r = http.post(WBC_API, headers=hdr, json=wbc_body(offset, size, filter_type, price_lo, price_hi))
        r.raise_for_status()
        d = r.json()
        batch = d.get("data") or []
        rows += batch
        total = (d.get("total") or {}).get("value", 0)
        offset += size
        if not batch or offset >= total:
            break
    return rows


def scrape_webuycars(http: Http, ctx: dict) -> dict:
    rows = _wbc_pages(http, "", PRICE_MIN, WBC_RETAIL_MAX)
    return {"cars": [], "points": parse_wbc_retail({"data": rows}), "raw": len(rows)}


def scrape_webuycars_auction(http: Http, ctx: dict) -> dict:
    rows = _wbc_pages(http, "firstDay")
    return {"cars": parse_wbc_auction({"data": rows}), "raw": len(rows)}


# --------------------------------------------------------------------------
# Source: Private Property
# --------------------------------------------------------------------------
PP = "https://www.privateproperty.co.za"


def parse_pp_results(html: str, suburb_hint: str | None = None) -> tuple[list[dict], bool]:
    soup = BeautifulSoup(html, "lxml")
    homes = []
    for a in soup.select("a.listing-result[href]"):
        href = a["href"]
        url = href if href.startswith("http") else PP + href
        price = money(a.select_one(".listing-result__price").get_text(" ") if a.select_one(".listing-result__price") else None)
        tnode = a.select_one(".listing-result__title")
        title = (a.get("title") or (tnode.get_text(" ", strip=True) if tnode else "")).strip()
        sub = a.select_one(".listing-result__desktop-suburb") or a.select_one(".listing-result__mobile-suburb")
        suburb = sub.get_text(strip=True) if sub else suburb_hint
        feats = {}
        for f in a.select(".listing-result__feature"):
            feats[(f.get("title") or "").lower()] = f.get_text(" ", strip=True)
        img = a.select_one("img.listing-result__image")
        lid = re.search(r"/(T\d+)$", href)
        size = None
        for k, v in feats.items():
            if "floor" in k or "size" in k:
                size = money(v)
        homes.append({
            "id": sid("privateproperty", url), "source": "privateproperty", "url": url,
            "title": title, "suburb": suburb, "price": price,
            "beds": to_int(feats.get("bedrooms")), "baths": money(feats.get("bathrooms")),
            "sizeM2": size, "levy": None, "rates": None, "type": home_type(title),
            "image": img.get("src") if img else None, "listedDate": None,
            "listingNo": lid.group(1) if lid else None,
        })
    has_next = bool(soup.select_one('a[href*="?page="]'))
    return homes, has_next


def parse_pp_detail(html: str) -> dict:
    t = re.sub(r"\s*\|\s*", "|", re.sub(r"<[^>]+>", "|", re.sub(r"<(script|style|svg)\b.*?</\1>", "", html, flags=re.S)))
    t = htmlmod.unescape(re.sub(r"\s+", " ", t))
    t = re.sub(r"(\| ?)+", "|", t)

    def field(label):
        m = re.search(r"\|" + label + r" ?\|([^|]+)\|", t, re.I)
        return m.group(1).strip() if m else None

    out = {"levy": money(field("Levies")), "rates": money(field("Rates and taxes")),
           "sizeM2": money(field("Floor size")), "propertyType": field("Property type")}
    ld = field("Listing date")
    try:
        out["listedDate"] = datetime.strptime(ld, "%d %b %Y").date().isoformat() if ld else None
    except ValueError:
        out["listedDate"] = None
    return out


def scrape_privateproperty(http: Http, ctx: dict) -> dict:
    homes, raw = [], 0
    for name, path in PP_SUBURBS.items():
        for page in (1, 2):
            url = f"{PP}/for-sale/gauteng/{path}?bd={HOME_BEDS_MIN}&tp={HOME_PRICE_MAX}" + (f"&page={page}" if page > 1 else "")
            r = http.get(url)
            if r.status_code == 404:
                break
            r.raise_for_status()
            hs, has_next = parse_pp_results(r.text, name)
            raw += len(hs)
            for h in hs:
                h["suburb"] = h["suburb"] or name
            homes += hs
            if not has_next or len(hs) < 20:
                break
    # dedupe and enrich the sectional ones with detail data (levy, rates, size, date)
    uniq = {h["url"]: h for h in homes}
    homes = list(uniq.values())
    fetched = 0
    for h in homes:
        if fetched >= PP_DETAIL_MAX:
            break
        if home_reject_reason(h):
            continue
        try:
            r = http.get(h["url"])
            fetched += 1
            if r.ok:
                d = parse_pp_detail(r.text)
                for k in ("levy", "rates", "sizeM2", "listedDate"):
                    if d.get(k) is not None:
                        h[k] = d[k]
                if d.get("propertyType"):
                    h["type"] = home_type(d["propertyType"]) or h["type"]
        except Exception as e:  # one bad detail page must not kill the source
            ctx.setdefault("warnings", []).append(f"pp detail {h['url']}: {e}")
    return {"homes": homes, "raw": raw}


# --------------------------------------------------------------------------
# Source: Property24
# --------------------------------------------------------------------------
P24 = "https://www.property24.com"


def parse_p24_results(html: str, suburb_hint: str | None = None) -> tuple[list[dict], bool]:
    soup = BeautifulSoup(html, "lxml")
    # Normal pages mention reCAPTCHA (contact form), and zero-result pages have no
    # results container, so only treat it as a bot wall when it isn't a P24 listing page.
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Property24" not in title and re.search(r"captcha|verify you are human|challenge|access denied", html, re.I):
        raise Blocked(f"Property24 served a bot check (title={title[:60]!r})")
    homes = []
    for tile in soup.select("div.p24_regularTile[data-listing-number]"):
        a = tile.select_one("a.p24_content[href]") or tile.select_one("a[href]")
        if not a:
            continue
        url = P24 + a["href"] if a["href"].startswith("/") else a["href"]
        pr = tile.select_one('[itemprop="price"]')
        price = money(pr.get("content") if pr and pr.get("content") else (pr.get_text() if pr else None))
        title = (tile.select_one(".p24_title").get_text(strip=True) if tile.select_one(".p24_title") else "")
        loc = tile.select_one(".p24_location")
        feats = {}
        for f in tile.select(".p24_featureDetails"):
            feats[(f.get("title") or "").lower()] = f.get_text(" ", strip=True)
        img = tile.select_one("img[itemprop=image]") or tile.select_one("img")
        homes.append({
            "id": sid("property24", url), "source": "property24", "url": url, "title": title,
            "suburb": loc.get_text(strip=True) if loc else suburb_hint, "price": price,
            "beds": to_int(feats.get("bedrooms")), "baths": money(feats.get("bathrooms")),
            "sizeM2": money(feats.get("floor size") or feats.get("erf size")) if feats.get("floor size") else None,
            "levy": None, "rates": None, "type": home_type(title),
            "image": (img.get("src") or img.get("lazy-src")) if img else None, "listedDate": None,
            "listingNo": tile.get("data-listing-number"),
        })
    has_next = bool(soup.select_one(".pagination a[data-pagenumber='2'], a[href*='/p2']"))
    return homes, has_next


def scrape_property24(http: Http, ctx: dict) -> dict:
    homes, raw = [], 0
    sp = f"?sp=pt%3d{HOME_PRICE_MAX}%26bd%3d{HOME_BEDS_MIN}"
    for name, path in P24_SUBURBS.items():
        for page in (1, 2):
            url = f"{P24}/for-sale/{path}" + (f"/p{page}" if page > 1 else "") + sp
            r = http.get(url)
            r.raise_for_status()
            if f"/{path.split('/')[-1]}" not in r.url:
                break  # redirected elsewhere (bad id / no results)
            hs, has_next = parse_p24_results(r.text, name)
            raw += len(hs)
            # P24 pads thin result pages with nearby suburbs - keep only target suburbs
            hs = [h for h in hs if h["suburb"] and any(s.lower() in h["suburb"].lower() for s in TARGET_SUBURBS)]
            homes += hs
            if not has_next or len(hs) < 20:
                break
    return {"homes": list({h["url"]: h for h in homes}.values()), "raw": raw}


# --------------------------------------------------------------------------
# Source: IGrow developments
# --------------------------------------------------------------------------
IGROW = "https://igrow.co.za/properties/"


def parse_igrow_list(html: str) -> list[dict]:
    """Current developments (cards before the 'past-developments' grid)."""
    cut = html.find("past-developments")
    out, seen = [], set()
    for m in re.finditer(r'href="(https://igrow\.co\.za/properties/([a-z0-9-]+)/)"[^>]*>\s*<span class="elementor-button-content-wrapper">', html):
        if cut != -1 and m.start() > cut:
            break
        if m.group(2) in seen:
            continue
        seen.add(m.group(2))
        seg = html[max(0, m.start() - 6000):m.start()]
        heads = [htmlmod.unescape(h).strip() for h in re.findall(r'class="elementor-heading-title[^"]*">([^<]*)<', seg)]
        if len(heads) < 4:
            continue
        name, loc, label, value = heads[-4:]
        out.append({"slug": m.group(2), "url": m.group(1), "name": name, "location": loc,
                    "priceFrom": money(value) if "price" in label.lower() else None,
                    "shortfall": value if "shortfall" in label.lower() else None})
    return out


def parse_igrow_detail(html: str, dev: dict) -> list[dict]:
    t = text_of(html)
    t = re.sub(r"\s+", " ", t)
    homes = []
    pat = re.compile(r"(\d) Bed \|? ?(\d) Bath \|? ?From R\s?([\d,]+) Typical Size: ?(\d+) ?m²(.{0,400}?)(?=\d Bed \|? ?\d Bath \|? ?From|$)")
    for m in pat.finditer(t):
        tail = m.group(5)
        levy = re.search(r"levy[^:]*:\s*R\s?([\d,]+)", tail, re.I)
        rates = re.search(r"rates:\s*R\s?([\d,]+)", tail, re.I)
        beds, baths, price, size = int(m.group(1)), int(m.group(2)), money(m.group(3)), money(m.group(4))
        url = dev["url"] + f"#{beds}bed-{baths}bath-{int(size)}m2"
        homes.append({
            "id": sid("igrow", url), "source": "igrow", "url": url,
            "title": f"{dev['name']} - {beds} bed {baths} bath ({int(size)} m²)",
            "suburb": dev["location"].split("|")[0].split(",")[0].strip(),
            "price": price, "beds": beds, "baths": baths, "sizeM2": size,
            "levy": money(levy.group(1)) if levy else None, "rates": money(rates.group(1)) if rates else None,
            "type": "new development", "image": None, "listedDate": None,
        })
    return homes


def scrape_igrow(http: Http, ctx: dict) -> dict:
    r = http.get(IGROW)
    if r.status_code in (403, 503) or "cf-chl" in r.text[:5000]:
        raise Blocked(f"IGrow blocked (HTTP {r.status_code}) - skipped")
    r.raise_for_status()
    devs = parse_igrow_list(r.text)
    near = [d for d in devs if re.search("|".join(TARGET_SUBURBS) + r"|randburg|roodepoort", d["location"], re.I)]
    homes = []
    for d in near[:4]:
        rd = http.get(d["url"])
        if rd.ok:
            homes += parse_igrow_detail(rd.text, d)
    return {"homes": homes, "raw": len(devs), "developments": devs}


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
CAR_SOURCES = {
    "auction_operation": scrape_auction_operation,
    "aucor": scrape_aucor,
    "mfc": scrape_mfc,
    "webuycars_auction": scrape_webuycars_auction,
    "webuycars": scrape_webuycars,
}
HOME_SOURCES = {
    "privateproperty": scrape_privateproperty,
    "property24": scrape_property24,
    "igrow": scrape_igrow,
}

HISTORY = OUT / "pricepoints_history.json"


def merge_history(points: list[dict], path: Path = HISTORY, write: bool = True) -> list[dict]:
    """Hammer prices vanish once an auction is archived, so keep every one we have seen."""
    hist = []
    if path.exists():
        try:
            hist = json.loads(path.read_text())
        except Exception:
            hist = []
    key = lambda p: (p["source"], p.get("url"), p.get("title"), p.get("price"), p.get("kind"))
    merged = {key(p): p for p in hist if p.get("kind") == "hammer"}
    for p in points:
        if p.get("kind") == "hammer":
            merged[key(p)] = p
    hammer = list(merged.values())
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(hammer, indent=1, ensure_ascii=False))
    return hammer + [p for p in points if p.get("kind") != "hammer"]


def dedupe_homes(homes: list[dict]) -> list[dict]:
    seen, out = set(), []
    for h in sorted(homes, key=lambda h: ({"privateproperty": 0, "property24": 1}.get(h["source"], 2),)):
        k = ((h.get("suburb") or "").lower(), h.get("price"), h.get("beds"), round(h.get("sizeM2") or 0))
        if h.get("sizeM2") and k in seen:
            continue
        seen.add(k)
        out.append(h)
    return out


def cap_homes(homes: list[dict], n: int = HOME_MAX) -> list[dict]:
    """Round-robin across suburbs (each suburb sorted by price) so the cap keeps every
    suburb and the whole price band instead of only the cheapest units."""
    by: dict[str, list] = {}
    for h in sorted(homes, key=lambda h: h["price"] or 0):
        by.setdefault((h.get("suburb") or "").lower(), []).append(h)
    out, queues = [], list(by.values())
    while queues and len(out) < n:
        for q in list(queues):
            if q and len(out) < n:
                out.append(q.pop(0))
            if not q:
                queues.remove(q)
    return sorted(out, key=lambda h: h["price"] or 0)


def run(only: set[str] | None = None, out_path: Path = OUT / "market.json") -> dict:
    t0 = time.time()
    http = Http()
    ctx: dict = {}
    sources, cars, homes, points = {}, [], [], []
    extra = {}
    for name, fn in {**CAR_SOURCES, **HOME_SOURCES}.items():
        if only and name not in only:
            continue
        st = time.time()
        print(f"[{name}] ...", file=sys.stderr, flush=True)
        try:
            res = fn(http, ctx)
            if name in CAR_SOURCES:
                kept, why = filter_cars(res.get("cars", []))
                cars += kept
                pts = [p for p in res.get("points", []) if keep_pricepoint(p)]
                points += pts
                info = {"ok": True, "count": len(kept), "error": res.get("note"),
                        "raw": res.get("raw", 0), "rejected": why, "pricePoints": len(pts)}
            else:
                hs = res.get("homes", [])
                kept = [h for h in hs if not home_reject_reason(h)]
                homes += kept
                info = {"ok": True, "count": len(kept), "error": res.get("note"), "raw": res.get("raw", 0)}
            if res.get("auctions"):
                info["auctions"] = res["auctions"]
            if res.get("developments"):
                info["developments"] = res["developments"]
        except Exception as e:
            info = {"ok": False, "count": 0, "error": f"{type(e).__name__}: {e}"}
            traceback.print_exc(file=sys.stderr)
        info["seconds"] = round(time.time() - st, 1)
        sources[name] = info
        print(f"[{name}] {info['ok']} kept={info['count']} raw={info.get('raw')} err={info['error']}", file=sys.stderr)
    homes = cap_homes(dedupe_homes(homes))
    cars.sort(key=lambda c: (c["region"] != "Gauteng", c["auctionDate"] or "9999", c["price"] or 9e9))
    carprices = merge_history(points, write=out_path.parent == OUT)
    result = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "cars": cars,
        "homes": homes,
        "carPrices": carprices,
        "meta": {"requests": http.count, "seconds": round(time.time() - t0, 1),
                 "filters": {"years": [YEAR_MIN, YEAR_MAX], "price": [PRICE_MIN, PRICE_MAX],
                             "retailGuideMax": RETAIL_GUIDE_MAX, "makes": ALLOWED_MAKES,
                             "homePriceMax": HOME_PRICE_MAX, "homeBedsMin": HOME_BEDS_MIN},
                 "warnings": ctx.get("warnings", [])[:20]},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1, ensure_ascii=False))
    tmp.replace(out_path)
    return result


# --------------------------------------------------------------------------
# Offline check (--dry): parse fixtures/, assert the parsers still work
# --------------------------------------------------------------------------
def dry() -> int:
    def rd(n):
        return (FIX / n).read_text(encoding="utf-8", errors="replace")

    fails = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    print("Auction Operation")
    ao = parse_ao_upcoming(rd("ao_upcoming.html"))
    check(len(ao) >= 3 and all(a["auctionId"] for a in ao), f"{len(ao)} auctions parsed")
    check(any(a["lots"] for a in ao) and any(a["start"] for a in ao), "lot counts + start dates present")

    print("Aucor")
    al = parse_aucor_auction_list(rd("aucor_past_auctions.html"))
    check(len(al) >= 5, f"{len(al)} auction summaries embedded")
    check(any(aucor_is_vehicle_auction(a) for a in al), "vehicle auctions recognised")
    c1, p1, n1 = parse_aucor_lots(json.loads(rd("aucor_lots_upcoming.json")))
    check(n1 > 0 and len(c1) > 0, f"upcoming lots -> {len(c1)} cars / {n1} raw")
    c2, p2, n2 = parse_aucor_lots(json.loads(rd("aucor_lots_past.json")))
    check(len(p2) > 0 and all(p["kind"] == "hammer" and p["price"] for p in p2), f"past lots -> {len(p2)} hammer prices")
    check(any(c["condition"] and "non-runner" in c["condition"] for c in c1), "non-runner flag parsed")

    print("MFC")
    mc, mp, mn = parse_mfc(json.loads(rd("mfc_auctions.json")))
    check(mn > 0 and len(mc) == mn, f"{mn} catalogue rows -> cars")
    check(all(c["region"] != "Unknown" for c in mc), "every MFC row has a region")
    check(all(c["km"] is None or c["km"] >= 500 for c in mc), "placeholder mileages nulled")

    print("WeBuyCars")
    wr = parse_wbc_retail(json.loads(rd("wbc_retail.json")))
    check(len(wr) > 0 and all(p["price"] for p in wr), f"{len(wr)} retail price points")
    wa = parse_wbc_auction(json.loads(rd("wbc_auction.json")))
    check(len(wa) > 0 and all(c["region"] for c in wa), f"{len(wa)} auction cars")

    print("Private Property")
    ph, _ = parse_pp_results(rd("pp_results.html"), "North Riding")
    check(len(ph) >= 10 and all(h["price"] and h["beds"] for h in ph), f"{len(ph)} result cards")
    pd = parse_pp_detail(rd("pp_detail.html"))
    check(pd["levy"] and pd["rates"] and pd["sizeM2"] and pd["listedDate"], f"detail fields {pd}")

    print("Property24")
    p24, _ = parse_p24_results(rd("p24_results.html"), "North Riding")
    check(len(p24) >= 10 and all(h["price"] for h in p24), f"{len(p24)} tiles")

    print("IGrow")
    ig = parse_igrow_list(rd("igrow_list.html"))
    check(len(ig) >= 5, f"{len(ig)} current developments")
    igd = parse_igrow_detail(rd("igrow_detail.html"), {"url": IGROW + "tirong-views/", "name": "Tirong Views",
                                                         "location": "Northgate | Randburg"})
    check(len(igd) >= 1 and igd[0]["price"], f"{len(igd)} unit types on detail page")

    print("Normalisers / filters")
    p = parse_vehicle_title("2019 VW POLO VIVO 1.4 TRENDLINE 5DR M/T (NON-RUNNER)")
    check(p["make"] == "Volkswagen" and p["model"] == "Polo Vivo" and p["year"] == 2019
          and p["transmission"] == "Manual", f"title parse {p}")
    check(parse_vehicle_title("2020 ISUZU 250 D-MAX KB250 D/C 4X2 A/T")["bodyType"] == "Bakkie", "bakkie tag")
    check(norm_make("2017 M/BENZ ACTROS") == "Mercedes-Benz", "M/BENZ alias")
    check(car_reject_reason(make_car("x", "u", "2018 BMW 320I", priceKind="starting bid", price=60000)) == "make", "BMW rejected")
    check(car_reject_reason(make_car("x", "u", "2015 TOYOTA ETIOS", priceKind="none")) == "year", "2015 rejected")
    check(car_reject_reason(make_car("x", "u", "2018 TOYOTA ETIOS", priceKind="current bid", price=95000)) == "price", "R95k bid rejected")
    check(car_reject_reason(make_car("x", "u", "2018 TOYOTA ETIOS", priceKind="none")) is None, "unknown price kept")
    check(clean_km("111111") is None and clean_km("87035") == 87035, "km cleaner")

    # end-to-end on fixtures -> out/market.dry.json
    cars = filter_cars(c1 + mc + wa)[0]
    homes = [h for h in ph + p24 + igd if not home_reject_reason(h)]
    res = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sources": {},
           "cars": cars, "homes": dedupe_homes(homes),
           "carPrices": [x for x in p2 + mp + wr if keep_pricepoint(x)]}
    (OUT / "market.dry.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print(f"\ndry run: {len(res['cars'])} cars, {len(res['homes'])} homes, {len(res['carPrices'])} price points"
          f" -> out/market.dry.json")
    print("FAILED: " + "; ".join(fails) if fails else "all checks passed")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry", action="store_true", help="parse fixtures only (no network)")
    ap.add_argument("--only", help="comma list of sources, e.g. aucor,mfc")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    if a.dry:
        sys.exit(dry())
    res = run(set(a.only.split(",")) if a.only else None)
    print(json.dumps({k: {kk: v.get(kk) for kk in ("ok", "count", "raw", "error")} for k, v in res["sources"].items()},
                     indent=1))
    print(f"cars={len(res['cars'])} homes={len(res['homes'])} carPrices={len(res['carPrices'])} "
          f"requests={res['meta']['requests']} seconds={res['meta']['seconds']}")


if __name__ == "__main__":
    main()
