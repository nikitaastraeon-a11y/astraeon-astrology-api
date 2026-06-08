from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import swisseph as swe
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder
from datetime import datetime, timedelta
import pytz

app = FastAPI(title="Astraeon Vedic API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Moshier ephemeris — built into pyswisseph, no external files needed
swe.set_ephe_path(None)

_geolocator = Nominatim(user_agent="astraeon-vedic-api-v1", timeout=10)
_tf = TimezoneFinder()

# ── Constants ────────────────────────────────────────────────────────────────

PLANETS = [
    (swe.SUN,       "Sun"),
    (swe.MOON,      "Moon"),
    (swe.MARS,      "Mars"),
    (swe.MERCURY,   "Mercury"),
    (swe.JUPITER,   "Jupiter"),
    (swe.VENUS,     "Venus"),
    (swe.SATURN,    "Saturn"),
    (swe.MEAN_NODE, "Rahu"),   # Ketu derived as Rahu + 180°
]

SIGNS = [
    "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
    "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces",
]

NAKSHATRAS = [
    "Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira", "Ardra",
    "Punarvasu", "Pushya", "Ashlesha", "Magha", "Purva Phalguni", "Uttara Phalguni",
    "Hasta", "Chitra", "Swati", "Vishakha", "Anuradha", "Jyeshtha",
    "Mula", "Purva Ashadha", "Uttara Ashadha", "Shravana", "Dhanishtha",
    "Shatabhisha", "Purva Bhadrapada", "Uttara Bhadrapada", "Revati",
]

# Each nakshatra repeats this lord sequence across all 27
NAKSHATRA_LORDS = (
    ["Ketu", "Venus", "Sun", "Moon", "Mars", "Rahu", "Jupiter", "Saturn", "Mercury"] * 3
)

DASHA_YEARS = {
    "Ketu": 7, "Venus": 20, "Sun": 6, "Moon": 10, "Mars": 7,
    "Rahu": 18, "Jupiter": 16, "Saturn": 19, "Mercury": 17,
}
DASHA_ORDER = ["Ketu", "Venus", "Sun", "Moon", "Mars", "Rahu", "Jupiter", "Saturn", "Mercury"]

# Navamsa starting sign per rasi (0-indexed): Fire=Aries(0), Earth=Cap(9), Air=Lib(6), Water=Can(3)
NAVAMSA_START = [0, 9, 6, 3, 0, 9, 6, 3, 0, 9, 6, 3]

NAK_SIZE  = 360.0 / 27   # 13.333...°
PADA_SIZE = NAK_SIZE / 4  # 3.333...°

# ── Pydantic Models ──────────────────────────────────────────────────────────

class GeocodeRequest(BaseModel):
    city: str

class BirthData(BaseModel):
    name: str = ""
    year: int
    month: int
    day: int
    hour: int
    minute: int
    lat: float
    lng: float
    tz_str: str = "AUTO"

# ── Core Helpers ─────────────────────────────────────────────────────────────

def _detect_tz(lat: float, lng: float) -> str:
    return _tf.timezone_at(lat=lat, lng=lng) or "UTC"

def _to_jd(year: int, month: int, day: int, hour: int, minute: int, tz_str: str) -> float:
    tz = pytz.timezone(tz_str)
    local_dt = tz.localize(datetime(year, month, day, hour, minute))
    utc = local_dt.utctimetuple()
    hour_decimal = utc.tm_hour + utc.tm_min / 60.0
    return swe.julday(utc.tm_year, utc.tm_mon, utc.tm_mday, hour_decimal)

def _nakshatra(lon: float) -> dict:
    idx   = int(lon / NAK_SIZE) % 27
    pada  = int((lon % NAK_SIZE) / PADA_SIZE) + 1
    return {"name": NAKSHATRAS[idx], "lord": NAKSHATRA_LORDS[idx], "pada": pada, "index": idx}

def _navamsa_sign(lon: float) -> tuple[str, int]:
    """Return (sign_name, 1-based sign_id) for navamsa of a sidereal longitude."""
    sign_idx     = int(lon / 30) % 12
    pos_in_sign  = lon % 30
    nav_idx      = int(pos_in_sign / (30.0 / 9))
    result_idx   = (NAVAMSA_START[sign_idx] + nav_idx) % 12
    return SIGNS[result_idx], result_idx + 1

def _compute_chart(jd: float, lat: float, lng: float) -> dict:
    """Core Vedic chart calculation — all planets, ascendant, whole-sign houses."""
    swe.set_sid_mode(swe.SIDM_LAHIRI)
    flags       = swe.FLG_MOSEPH | swe.FLG_SIDEREAL | swe.FLG_SPEED
    ayanamsha   = swe.get_ayanamsa_ut(jd)

    # Tropical ascendant → sidereal
    cusps, ascmc = swe.houses(jd, lat, lng, b'W')
    sid_asc      = (ascmc[0] - ayanamsha) % 360
    asc_sign_idx = int(sid_asc / 30)

    # Planets
    planets   = []
    moon_lon  = None
    rahu_lon  = None

    for planet_id, name in PLANETS:
        xx, _ = swe.calc_ut(jd, planet_id, flags)
        lon   = xx[0] % 360
        speed = xx[3]

        sign_idx = int(lon / 30)
        nak      = _nakshatra(lon)
        house    = ((sign_idx - asc_sign_idx) % 12) + 1

        planets.append({
            "name":           name,
            "longitude":      round(lon, 4),
            "sign":           SIGNS[sign_idx],
            "sign_id":        sign_idx + 1,
            "degree_in_sign": round(lon % 30, 4),
            "nakshatra":      nak["name"],
            "nakshatra_lord": nak["lord"],
            "pada":           nak["pada"],
            "house":          house,
            "is_retrograde":  speed < 0,
        })

        if name == "Moon":  moon_lon = lon
        if name == "Rahu":  rahu_lon = lon

    # Ketu = Rahu + 180°
    if rahu_lon is not None:
        k_lon    = (rahu_lon + 180) % 360
        k_sign   = int(k_lon / 30)
        k_nak    = _nakshatra(k_lon)
        planets.append({
            "name":           "Ketu",
            "longitude":      round(k_lon, 4),
            "sign":           SIGNS[k_sign],
            "sign_id":        k_sign + 1,
            "degree_in_sign": round(k_lon % 30, 4),
            "nakshatra":      k_nak["name"],
            "nakshatra_lord": k_nak["lord"],
            "pada":           k_nak["pada"],
            "house":          ((k_sign - asc_sign_idx) % 12) + 1,
            "is_retrograde":  False,
        })

    # Whole-sign houses
    houses = [
        {"house": i + 1, "sign": SIGNS[(asc_sign_idx + i) % 12], "sign_id": ((asc_sign_idx + i) % 12) + 1}
        for i in range(12)
    ]

    asc_nak = _nakshatra(sid_asc)
    return {
        "ascendant": {
            "longitude":      round(sid_asc, 4),
            "sign":           SIGNS[asc_sign_idx],
            "sign_id":        asc_sign_idx + 1,
            "degree_in_sign": round(sid_asc % 30, 4),
            "nakshatra":      asc_nak["name"],
            "nakshatra_lord": asc_nak["lord"],
        },
        "planets":        planets,
        "houses":         houses,
        "moon_longitude": moon_lon,
        "ayanamsha":      round(ayanamsha, 4),
    }

def _compute_dasha(moon_lon: float, birth_dt: datetime) -> list:
    """Vimshottari Dasha from Moon nakshatra."""
    nak_idx           = int(moon_lon / NAK_SIZE) % 27
    fraction_elapsed  = (moon_lon % NAK_SIZE) / NAK_SIZE
    lord              = NAKSHATRA_LORDS[nak_idx]
    lord_idx          = DASHA_ORDER.index(lord)

    years_remaining   = (1 - fraction_elapsed) * DASHA_YEARS[lord]
    dashas            = []
    cursor            = birth_dt

    # First dasha (partial)
    end = cursor + timedelta(days=years_remaining * 365.25)
    dashas.append({
        "lord":  lord,
        "years": round(years_remaining, 2),
        "start": cursor.strftime("%Y-%m-%d"),
        "end":   end.strftime("%Y-%m-%d"),
    })
    cursor = end

    for i in range(1, 9):
        next_lord = DASHA_ORDER[(lord_idx + i) % 9]
        yrs       = DASHA_YEARS[next_lord]
        end       = cursor + timedelta(days=yrs * 365.25)
        dashas.append({
            "lord":  next_lord,
            "years": yrs,
            "start": cursor.strftime("%Y-%m-%d"),
            "end":   end.strftime("%Y-%m-%d"),
        })
        cursor = end

    return dashas

def _compute_navamsa(chart: dict) -> dict:
    """Navamsa (D-9) chart from a computed chart dict."""
    asc_nav_sign, asc_nav_id = _navamsa_sign(chart["ascendant"]["longitude"])
    asc_nav_idx              = asc_nav_id - 1

    nav_planets = []
    for p in chart["planets"]:
        sign, sign_id = _navamsa_sign(p["longitude"])
        house         = ((sign_id - 1 - asc_nav_idx) % 12) + 1
        nav_planets.append({
            "name":          p["name"],
            "sign":          sign,
            "sign_id":       sign_id,
            "house":         house,
            "is_retrograde": p["is_retrograde"],
        })

    nav_houses = [
        {"house": i + 1, "sign": SIGNS[(asc_nav_idx + i) % 12], "sign_id": ((asc_nav_idx + i) % 12) + 1}
        for i in range(12)
    ]

    return {
        "ascendant": {"sign": asc_nav_sign, "sign_id": asc_nav_id},
        "planets":   nav_planets,
        "houses":    nav_houses,
    }

# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "astraeon-vedic-api", "version": "1.0.0"}

@app.post("/geocode")
def geocode(req: GeocodeRequest):
    """City name → lat, lng, timezone. Used for birth form autocomplete."""
    try:
        location = _geolocator.geocode(req.city, exactly_one=True)
        if not location:
            raise HTTPException(status_code=404, detail=f"City '{req.city}' not found")
        tz_str = _tf.timezone_at(lat=location.latitude, lng=location.longitude) or "UTC"
        return {
            "city":     location.address,
            "lat":      round(location.latitude, 4),
            "lng":      round(location.longitude, 4),
            "timezone": tz_str,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/chart")
def chart(data: BirthData):
    """Rasi (D-1) chart — all 9 planets + Ketu, ascendant, whole-sign houses."""
    try:
        tz  = data.tz_str if data.tz_str != "AUTO" else _detect_tz(data.lat, data.lng)
        jd  = _to_jd(data.year, data.month, data.day, data.hour, data.minute, tz)
        res = _compute_chart(jd, data.lat, data.lng)
        res["name"] = data.name
        res["birth"] = {
            "year": data.year, "month": data.month, "day": data.day,
            "hour": data.hour, "minute": data.minute, "timezone": tz,
        }
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dasha")
def dasha(data: BirthData):
    """Vimshottari Mahadasha timeline from Moon nakshatra at birth."""
    try:
        tz     = data.tz_str if data.tz_str != "AUTO" else _detect_tz(data.lat, data.lng)
        jd     = _to_jd(data.year, data.month, data.day, data.hour, data.minute, tz)
        chart  = _compute_chart(jd, data.lat, data.lng)
        birth_dt = datetime(data.year, data.month, data.day, data.hour, data.minute)
        return {
            "dashas":         _compute_dasha(chart["moon_longitude"], birth_dt),
            "moon_longitude": chart["moon_longitude"],
            "moon_nakshatra": _nakshatra(chart["moon_longitude"]),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/navamsa")
def navamsa(data: BirthData):
    """Navamsa (D-9) chart."""
    try:
        tz    = data.tz_str if data.tz_str != "AUTO" else _detect_tz(data.lat, data.lng)
        jd    = _to_jd(data.year, data.month, data.day, data.hour, data.minute, tz)
        chart = _compute_chart(jd, data.lat, data.lng)
        return _compute_navamsa(chart)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/kundli")
def kundli(data: BirthData):
    """Full Kundli in one call — Rasi chart + Navamsa + Vimshottari Dasha."""
    try:
        tz    = data.tz_str if data.tz_str != "AUTO" else _detect_tz(data.lat, data.lng)
        jd    = _to_jd(data.year, data.month, data.day, data.hour, data.minute, tz)
        c     = _compute_chart(jd, data.lat, data.lng)
        birth_dt = datetime(data.year, data.month, data.day, data.hour, data.minute)
        return {
            "name":    data.name,
            "chart":   {**c, "birth": {"year": data.year, "month": data.month, "day": data.day,
                                        "hour": data.hour, "minute": data.minute, "timezone": tz}},
            "navamsa": _compute_navamsa(c),
            "dasha":   _compute_dasha(c["moon_longitude"], birth_dt),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
