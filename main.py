from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import swisseph as swe
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder
from datetime import datetime, timedelta
import pytz
import math

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

class AstroCartographyRequest(BaseModel):
    name: str = ""
    year: int
    month: int
    day: int
    hour: int
    minute: int
    lat: float
    lng: float
    tz_str: str = "AUTO"
    mode: str = "western"              # "western" | "vedic"
    include_parans: bool = True
    include_dasha_overlay: bool = True
    include_outer_planets: Optional[bool] = None  # None = mode default (western: on, vedic: off)

class TransitRequest(BaseModel):
    start: str                       # "YYYY-MM-DD" or ISO datetime (interpreted as UTC)
    end: str                         # "YYYY-MM-DD" or ISO datetime
    step_hours: Optional[float] = None  # sampling step; auto-picked from range if omitted
    natal_ascendant_sign_id: Optional[int] = None  # 1..12, to place transits in natal houses

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

# ── Astrocartography ─────────────────────────────────────────────────────────

# Equatorial (RA/Dec) flags — NO FLG_SIDEREAL so we always get tropical apparent
# geocentric coordinates. Ayanamsha is irrelevant to ACG line geometry.
_EQ_FLAGS = swe.FLG_MOSEPH | swe.FLG_SPEED | swe.FLG_EQUATORIAL

# The three modern (trans-Saturnian) planets. Standard in Western ACG; not part
# of classical Jyotish, so only added to Vedic mode on explicit request.
# All three are covered by the Moshier ephemeris — no extra data files needed.
OUTER_PLANETS = [
    (swe.URANUS,  "Uranus"),
    (swe.NEPTUNE, "Neptune"),
    (swe.PLUTO,   "Pluto"),
]

PLANET_COLORS_ACG = {
    "Sun": "#FFD700", "Moon": "#C0C0C0", "Mars": "#FF4500",
    "Mercury": "#7CFC00", "Jupiter": "#FFA500", "Venus": "#FF69B4",
    "Saturn": "#87CEEB", "Rahu": "#8B008B", "Ketu": "#A0522D",
    "Uranus": "#4FD1C5", "Neptune": "#5B8DEF", "Pluto": "#9B5DE5",
}

ANGLE_META_ACG = {
    "MC":  {"label": "Midheaven Line",  "meaning": "Career, public role, reputation, life direction"},
    "IC":  {"label": "IC Line",         "meaning": "Home, roots, private life, emotional foundation"},
    "ASC": {"label": "Rising Line",     "meaning": "Identity, body, how others perceive you in this place"},
    "DSC": {"label": "Setting Line",    "meaning": "Relationships, partnerships, what you attract here"},
}

WESTERN_INTERP = {
    "Sun":     {"keyword": "Identity & Recognition",    "theme": "Where the world sees and validates you. Career visibility, authority, and personal radiance."},
    "Moon":    {"keyword": "Comfort & Home",            "theme": "Emotional ease, nurturing environments, strong intuition. Where belonging feels natural."},
    "Mars":    {"keyword": "Energy & Drive",            "theme": "Motivation, courage, and physical vitality. Conflict is also possible. Best for sport, business, action."},
    "Mercury": {"keyword": "Communication & Intellect", "theme": "Sharp thinking, commerce, writing, and adaptability. Conversations and opportunities flow here."},
    "Jupiter": {"keyword": "Expansion & Luck",          "theme": "Growth, opportunity, abundance. Things come more easily. Higher learning, travel, good fortune."},
    "Venus":   {"keyword": "Love & Beauty",             "theme": "Relationships bloom, aesthetic sense heightens. Art, romance, social ease, and pleasure."},
    "Saturn":  {"keyword": "Discipline & Karma",        "theme": "Demanding but rewarding. Hard work builds lasting foundations. Long-term investment pays off."},
    "Rahu":    {"keyword": "Obsession & Ambition",      "theme": "Intensity, foreignness, rapid transformation. Disorienting but expansive. Karmic acceleration."},
    "Ketu":    {"keyword": "Release & Retreat",         "theme": "Spiritual depth, solitude, past-life resonance. Material attachments loosen. Good for inner work."},
    "Uranus":  {"keyword": "Freedom & Disruption",      "theme": "Sudden change, innovation, breaking from convention. Awakening and independence, sometimes upheaval. Where you reinvent yourself."},
    "Neptune": {"keyword": "Dreams & Dissolution",      "theme": "Imagination, spirituality, art, and escapism. Boundaries blur. Inspiring but can be foggy or deceptive. Strong for creatives and mystics."},
    "Pluto":   {"keyword": "Power & Transformation",    "theme": "Deep, irreversible change. Intensity, control, rebirth. Confronts you with power and shadow. Profound but demanding."},
}

VEDIC_INTERP = {
    "Sun":     {"keyword": "Surya — Soul & Authority",       "theme": "Government, leadership, father figures. Where your inner dharma is recognized. Visibility comes with responsibility."},
    "Moon":    {"keyword": "Chandra — Mind & Mother",        "theme": "Emotional nourishment, mental peace, connection to the feminine. Where the mind settles. Strong for domestic and creative life."},
    "Mars":    {"keyword": "Mangal — Courage & Property",    "theme": "Valor, real estate, siblings. High energy and competitiveness. Excellent for soldiers, athletes, surgeons, and entrepreneurs."},
    "Mercury": {"keyword": "Budha — Intellect & Trade",      "theme": "Sharp intellect, commercial acumen, adaptability. Thriving in trade, media, and mathematics. Languages come easily."},
    "Jupiter": {"keyword": "Guru — Wisdom & Dharma",         "theme": "Your most auspicious lines in Jyotish. Teachers, wealth, children, spirituality. Blessings flow with less effort here."},
    "Venus":   {"keyword": "Shukra — Luxury & Relationships","theme": "Comfort, beauty, fine arts, romantic partnerships. Wealth through creative pursuits. Sensory richness of place."},
    "Saturn":  {"keyword": "Shani — Karma & Discipline",     "theme": "Shani demands patience but grants enduring reward. Places of hard karmic work that build real foundations over time."},
    "Rahu":    {"keyword": "Rahu — Foreign Lands & Desire",  "theme": "Classic indicator of success abroad. Material ambition, disruption, unconventional paths. Intense and expansive."},
    "Ketu":    {"keyword": "Ketu — Liberation & Past Karma", "theme": "Moksha energy. Spiritual seeking, renunciation, psychic depth. Past-life connections. Best for retreats and inner work."},
    # Not classical grahas — included only when outer planets are explicitly requested in Vedic mode.
    "Uranus":  {"keyword": "Uranus — Sudden Upheaval",       "theme": "Non-traditional in Jyotish. Read as abrupt change, rebellion, and awakening. Places of radical reinvention."},
    "Neptune": {"keyword": "Neptune — Maya & Mysticism",     "theme": "Non-traditional in Jyotish. Read as illusion (maya), spirituality, and imagination. Dissolving of boundaries."},
    "Pluto":   {"keyword": "Pluto — Death & Rebirth",        "theme": "Non-traditional in Jyotish. Read as deep transformation, hidden power, and regeneration. Intense and irreversible."},
}

def _gast_deg(jd: float) -> float:
    """Greenwich Apparent Sidereal Time in degrees (includes nutation in RA)."""
    return swe.sidtime(jd) * 15.0

def _equatorial(jd: float, planet_id: int) -> tuple:
    """Apparent geocentric RA (degrees) and Dec (degrees)."""
    xx, _ = swe.calc_ut(jd, planet_id, _EQ_FLAGS)
    return xx[0], xx[1]

def _norm180(lon: float) -> float:
    """Normalize longitude to -180..+180 for GeoJSON map display."""
    lon = lon % 360
    return lon - 360 if lon > 180 else lon

def _mc_ic_pts(ra: float, gast: float) -> dict:
    mc = _norm180(ra - gast)
    ic = _norm180(ra - gast + 180)
    return {
        "MC": [[mc, -85.0], [mc,  85.0]],
        "IC": [[ic, -85.0], [ic,  85.0]],
    }

def _asc_dsc_pts(ra: float, dec: float, gast: float, step: float = 0.4) -> dict:
    """Sweep latitudes -66..+66 and compute ASC/DSC longitudes in mundo."""
    dec_r = math.radians(dec)
    asc, dsc = [], []
    lat = -66.0
    while lat <= 66.01:
        lat_r = math.radians(lat)
        d = -math.tan(lat_r) * math.tan(dec_r)
        if abs(d) <= 1.0:
            H0 = math.degrees(math.acos(d))
            asc.append([_norm180(ra - H0 - gast), round(lat, 2)])
            dsc.append([_norm180(ra + H0 - gast), round(lat, 2)])
        lat += step
    return {"ASC": asc, "DSC": dsc}

def _split_antimeridian(pts: list) -> list:
    """Split a curve into segments at antimeridian crossings (lon jump > 180°)."""
    if len(pts) < 2:
        return [pts] if pts else []
    segs, cur = [], [pts[0]]
    for i in range(1, len(pts)):
        if abs(pts[i][0] - pts[i - 1][0]) > 180:
            if len(cur) > 1:
                segs.append(cur)
            cur = [pts[i]]
        else:
            cur.append(pts[i])
    if len(cur) > 1:
        segs.append(cur)
    return segs

def _make_features(planet: str, all_pts: dict, mode: str, is_active: bool) -> list:
    interp = (VEDIC_INTERP if mode == "vedic" else WESTERN_INTERP).get(planet, {})
    color  = PLANET_COLORS_ACG.get(planet, "#FFFFFF")
    features = []
    for angle, pts in all_pts.items():
        ameta = ANGLE_META_ACG[angle]
        props = {
            "planet": planet, "angle": angle,
            "angle_label":   ameta["label"],
            "angle_meaning": ameta["meaning"],
            "keyword":       interp.get("keyword", ""),
            "theme":         interp.get("theme", ""),
            "color":         color,
            "is_dasha_active": is_active,
        }
        segs = [pts] if angle in ("MC", "IC") else _split_antimeridian(pts)
        for seg in segs:
            if len(seg) < 2:
                continue
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": seg},
                "properties": props,
            })
    return features

def _lon_at_lat_direct(ra: float, dec: float, gast: float, angle: str, lat: float) -> Optional[float]:
    """Compute the ACG longitude for a planet/angle at a given latitude. O(1)."""
    if angle == "MC":
        return _norm180(ra - gast)
    if angle == "IC":
        return _norm180(ra - gast + 180)
    dec_r = math.radians(dec)
    lat_r = math.radians(lat)
    d = -math.tan(lat_r) * math.tan(dec_r)
    if abs(d) > 1.0:
        return None
    H0 = math.degrees(math.acos(d))
    return _norm180(ra - H0 - gast) if angle == "ASC" else _norm180(ra + H0 - gast)

def _compute_parans(bodies: dict, gast: float, step: float = 0.25) -> list:
    """Find in-mundo parans: latitudes where two different-planet lines are
    simultaneously angular. MC/IC against each other are skipped (parallel verticals).
    Uses direct O(1) formula per latitude — no interpolation needed."""
    names  = list(bodies.keys())
    angles = ["MC", "IC", "ASC", "DSC"]
    lats   = [round(-66.0 + i * step, 2) for i in range(int(132 / step) + 1)]
    parans = []

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            p1, (ra1, dec1) = names[i], bodies[names[i]]
            p2, (ra2, dec2) = names[j], bodies[names[j]]
            for a1 in angles:
                for a2 in angles:
                    if a1 in ("MC", "IC") and a2 in ("MC", "IC"):
                        continue  # two vertical lines never cross
                    prev_diff, prev_lat = None, None
                    for lat in lats:
                        lon1 = _lon_at_lat_direct(ra1, dec1, gast, a1, lat)
                        lon2 = _lon_at_lat_direct(ra2, dec2, gast, a2, lat)
                        if lon1 is None or lon2 is None:
                            prev_diff = None
                            continue
                        diff = lon1 - lon2
                        if diff >  180: diff -= 360
                        if diff < -180: diff += 360
                        if prev_diff is not None and prev_diff * diff < 0 and prev_lat is not None:
                            paran_lat = round((lat + prev_lat) / 2, 1)
                            hem = "N" if paran_lat >= 0 else "S"
                            parans.append({
                                "latitude": paran_lat,
                                "planet1": p1, "angle1": a1,
                                "planet2": p2, "angle2": a2,
                                "detail": (
                                    f"{p1} {ANGLE_META_ACG[a1]['label']} / "
                                    f"{p2} {ANGLE_META_ACG[a2]['label']} "
                                    f"at {abs(paran_lat):.1f}°{hem}"
                                ),
                            })
                        prev_diff, prev_lat = diff, lat

    parans.sort(key=lambda x: x["latitude"])
    return parans

# ── Transits (gochar) ──────────────────────────────────────────────────────────

# Bodies scanned for transit events (Ketu handled as Rahu + 180°).
TRANSIT_BODIES = PLANETS  # Sun..Rahu; Ketu derived
PHASE_NAMES = {0: "New Moon", 1: "First Quarter", 2: "Full Moon", 3: "Last Quarter"}

def _jd_from_dt(dt: datetime) -> float:
    return swe.julday(dt.year, dt.month, dt.day, dt.hour + dt.minute / 60.0 + dt.second / 3600.0)

def _dt_from_jd(jd: float) -> datetime:
    y, m, d, h = swe.revjul(jd)
    hh = int(h)
    mm = int((h - hh) * 60)
    return datetime(y, m, d, hh, mm)

def _sidereal_lon(jd: float, planet_id: int) -> tuple[float, float]:
    """Return (sidereal_longitude, speed) for a body at jd."""
    swe.set_sid_mode(swe.SIDM_LAHIRI)
    flags = swe.FLG_MOSEPH | swe.FLG_SIDEREAL | swe.FLG_SPEED
    xx, _ = swe.calc_ut(jd, planet_id, flags)
    return xx[0] % 360, xx[3]

def _all_lons(jd: float) -> dict:
    """{name: (lon, speed)} for all bodies incl. Ketu at jd."""
    out = {}
    rahu_lon = rahu_spd = None
    for pid, name in TRANSIT_BODIES:
        lon, spd = _sidereal_lon(jd, pid)
        out[name] = (lon, spd)
        if name == "Rahu":
            rahu_lon, rahu_spd = lon, spd
    if rahu_lon is not None:
        out["Ketu"] = ((rahu_lon + 180) % 360, rahu_spd)
    return out

def _bisect(a: float, b: float, key, target, tol_days: float = 1.0 / 1440):
    """Binary-search jd in [a, b] for the boundary where key(jd) leaves `target`.
    Assumes key(a) == target and key(b) != target. Returns jd of the crossing."""
    while b - a > tol_days:
        mid = (a + b) / 2
        if key(mid) == target:
            a = mid
        else:
            b = mid
    return b

def _scan_transits(start: datetime, end: datetime, step_hours: float,
                   natal_asc_id: Optional[int]) -> list:
    step_jd = step_hours / 24.0
    jd0, jd_end = _jd_from_dt(start), _jd_from_dt(end)
    events = []

    prev = _all_lons(jd0)
    jd = jd0
    while jd < jd_end:
        nxt_jd = min(jd + step_jd, jd_end)
        curr = _all_lons(nxt_jd)

        for name in curr:
            p_lon, p_spd = prev[name]
            c_lon, c_spd = curr[name]

            # 1) Sign ingress
            p_sign, c_sign = int(p_lon / 30), int(c_lon / 30)
            if p_sign != c_sign:
                cross = _bisect(jd, nxt_jd, lambda j: int(_planet_lon(j, name) / 30), p_sign)
                new_sign = int(_planet_lon(cross, name) / 30)
                events.append({
                    "type": "ingress", "planet": name,
                    "date": _dt_from_jd(cross).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "detail": f"{name} enters {SIGNS[new_sign]}",
                    "sign": SIGNS[new_sign], "sign_id": new_sign + 1,
                })

            # 2) Retrograde / direct station (nodes are always retrograde — skip)
            if name not in ("Rahu", "Ketu") and (p_spd < 0) != (c_spd < 0):
                cross = _bisect(jd, nxt_jd, lambda j: _planet_speed(j, name) < 0, p_spd < 0)
                going_retro = _planet_speed(cross, name) < 0
                events.append({
                    "type": "retrograde" if going_retro else "direct", "planet": name,
                    "date": _dt_from_jd(cross).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "detail": f"{name} turns {'retrograde' if going_retro else 'direct'}",
                })

        # 3) Lunar phases (Sun–Moon elongation quadrant crossings)
        p_q = int(((prev["Moon"][0] - prev["Sun"][0]) % 360) / 90)
        c_q = int(((curr["Moon"][0] - curr["Sun"][0]) % 360) / 90)
        if p_q != c_q:
            cross = _bisect(jd, nxt_jd, lambda j: int(_elongation(j) / 90), p_q)
            new_q = int(_elongation(cross) / 90)
            events.append({
                "type": "moon_phase", "planet": "Moon",
                "date": _dt_from_jd(cross).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "detail": PHASE_NAMES[new_q], "phase": PHASE_NAMES[new_q],
            })

        prev = curr
        jd = nxt_jd

    events.sort(key=lambda e: e["date"])
    return events

def _planet_lon(jd: float, name: str) -> float:
    return _all_lons(jd)[name][0]

def _planet_speed(jd: float, name: str) -> float:
    return _all_lons(jd)[name][1]

def _elongation(jd: float) -> float:
    d = _all_lons(jd)
    return (d["Moon"][0] - d["Sun"][0]) % 360

def _snapshot(jd: float, natal_asc_id: Optional[int]) -> list:
    """Current position of every body — sign, degree, nakshatra, retrograde, natal house."""
    out = []
    for name, (lon, spd) in _all_lons(jd).items():
        sign_idx = int(lon / 30)
        nak = _nakshatra(lon)
        retro = (spd < 0) and name not in ("Ketu",)
        entry = {
            "name": name,
            "longitude": round(lon, 4),
            "sign": SIGNS[sign_idx],
            "sign_id": sign_idx + 1,
            "degree_in_sign": round(lon % 30, 2),
            "nakshatra": nak["name"],
            "is_retrograde": retro or name == "Rahu" or name == "Ketu",
        }
        if natal_asc_id:
            entry["natal_house"] = ((sign_idx - (natal_asc_id - 1)) % 12) + 1
        out.append(entry)
    return out

# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "astraeon-vedic-api", "version": "1.0.0"}

def _parse_dt(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s.replace("Z", ""))
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%d")

@app.post("/transits")
def transits(req: TransitRequest):
    """Gochar (sky transits) over a date range: current positions + a timeline of
    events (sign ingresses, retrograde/direct stations, lunar phases). Dates are UTC.
    Pass natal_ascendant_sign_id to place each transiting planet in a natal house."""
    try:
        start = _parse_dt(req.start)
        end   = _parse_dt(req.end)
        if end <= start:
            raise HTTPException(status_code=400, detail="`end` must be after `start`.")

        span_days = (end - start).total_seconds() / 86400.0
        if span_days > 800:
            raise HTTPException(status_code=400, detail="Range too large (max ~2 years).")

        # Auto step: fine for short ranges (catch the Moon), coarse for long ones.
        step = req.step_hours or (1.0 if span_days <= 2 else 6.0 if span_days <= 45 else 24.0)

        events = _scan_transits(start, end, step, req.natal_ascendant_sign_id)
        return {
            "range": {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "end":   end.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "step_hours": step,
            "positions": _snapshot(_jd_from_dt(start), req.natal_ascendant_sign_id),
            "events": events,
            "count": len(events),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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

@app.post("/astrocartography")
def astrocartography(data: AstroCartographyRequest):
    """Astrocartography (ACG) lines computed fully in mundo from true geocentric
    RA/Dec + Greenwich Apparent Sidereal Time — the most astronomically accurate method.

    Returns GeoJSON Features for all 40 angular lines (MC, IC, ASC, DSC × 9 grahas + Ketu).
    Supports Western (tropical interpretation) and Vedic (Jyotish graha meanings + dasha overlay).
    MC/IC lines are exact meridians (2-point vertical). ASC/DSC are swept horizon curves.
    Antimeridian crossings are pre-split for direct use in any GeoJSON map renderer.
    Parans (in mundo) are latitude bands where two planet lines simultaneously coincide."""
    try:
        tz   = data.tz_str if data.tz_str != "AUTO" else _detect_tz(data.lat, data.lng)
        jd   = _to_jd(data.year, data.month, data.day, data.hour, data.minute, tz)
        gast = _gast_deg(jd)

        # ── Choose body set by mode ──────────────────────────────────────────
        # Western ACG conventionally includes Uranus/Neptune/Pluto; classical
        # Jyotish does not. Default follows the mode; caller can override.
        include_outer = (
            data.include_outer_planets
            if data.include_outer_planets is not None
            else (data.mode != "vedic")
        )
        body_defs = list(PLANETS) + (OUTER_PLANETS if include_outer else [])

        # ── Planetary equatorial coordinates (tropical RA/Dec) ───────────────
        # FLG_SIDEREAL is NOT set so we always get tropical apparent geocentric
        # positions, regardless of any prior set_sid_mode call.
        bodies: dict[str, tuple[float, float]] = {}
        for pid, name in body_defs:
            bodies[name] = _equatorial(jd, pid)
        ra_r, dec_r = bodies["Rahu"]
        bodies["Ketu"] = ((ra_r + 180) % 360, -dec_r)   # exact antipodal point

        # ── Dasha overlay — find which planets are active today ──────────────
        dasha_overlay: dict = {}
        active_planets: set = set()
        if data.include_dasha_overlay:
            chart    = _compute_chart(jd, data.lat, data.lng)
            birth_dt = datetime(data.year, data.month, data.day, data.hour, data.minute)
            dashas   = _compute_dasha(chart["moon_longitude"], birth_dt)
            today    = datetime.now()
            cur_maha = next(
                (d for d in dashas
                 if datetime.strptime(d["start"], "%Y-%m-%d") <= today
                    <= datetime.strptime(d["end"], "%Y-%m-%d")),
                None,
            )
            if cur_maha:
                maha_lord  = cur_maha["lord"]
                maha_start = datetime.strptime(cur_maha["start"], "%Y-%m-%d")
                maha_end   = datetime.strptime(cur_maha["end"], "%Y-%m-%d")
                total_days = max((maha_end - maha_start).days, 1)
                maha_idx   = DASHA_ORDER.index(maha_lord)
                cursor     = maha_start
                antar_lord = None
                for i in range(9):
                    lord = DASHA_ORDER[(maha_idx + i) % 9]
                    days = int(total_days * DASHA_YEARS[lord] / 120.0)
                    end  = cursor + timedelta(days=days)
                    if cursor <= today <= end:
                        antar_lord = lord
                        break
                    cursor = end
                active_planets = {maha_lord} | ({antar_lord} if antar_lord else set())
                dasha_overlay  = {
                    "mahadasha": maha_lord,
                    "antardasha": antar_lord,
                    "active_planets": list(active_planets),
                }

        # ── Build GeoJSON features ───────────────────────────────────────────
        features = []
        for name, (ra, dec) in bodies.items():
            is_active = name in active_planets
            all_pts   = {**_mc_ic_pts(ra, gast), **_asc_dsc_pts(ra, dec, gast)}
            features.extend(_make_features(name, all_pts, data.mode, is_active))

        # ── Parans (in mundo) ────────────────────────────────────────────────
        parans = _compute_parans(bodies, gast) if data.include_parans else []

        return {
            "type": "FeatureCollection",
            "mode": data.mode,
            "name": data.name,
            "birth": {
                "year": data.year, "month": data.month, "day": data.day,
                "hour": data.hour, "minute": data.minute, "timezone": tz,
            },
            "gast":              round(gast, 6),
            "includes_outer_planets": include_outer,
            "dasha_overlay":     dasha_overlay,
            "features":          features,
            "parans":            parans,
            "interpretations":   VEDIC_INTERP if data.mode == "vedic" else WESTERN_INTERP,
            "angle_meta":        ANGLE_META_ACG,
            "planet_colors":     PLANET_COLORS_ACG,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
