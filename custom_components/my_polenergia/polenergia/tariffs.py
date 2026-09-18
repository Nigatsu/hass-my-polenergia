"""Polish G-tariff helpers: zone counts, zone slugs, label and direction parsing.

Polenergia has only ever been observed returning single-zone (G11) data, so every
function here is written to degrade to "one zone, import only" rather than guess.
The zone slugs (``z1``/``z2``/``z3``) become statistic-ID suffixes, so they must
stay stable once released.
"""

import re
import unicodedata

# Direction of energy flow. Import is the default for any row that says nothing.
DIRECTION_IMPORT = "import"
DIRECTION_EXPORT = "export"

# Zone slugs. ``None`` means "no zone dimension" (single-zone tariff / total).
ZONE_1 = "z1"
ZONE_2 = "z2"
ZONE_3 = "z3"
ZONE_KEYS: tuple[str, ...] = (ZONE_1, ZONE_2, ZONE_3)

# Human labels used in statistic metadata names.
ZONE_DISPLAY_NAMES = {ZONE_1: "Day", ZONE_2: "Night", ZONE_3: "Off-peak"}

# Tariff prefix -> number of settlement zones. Matched longest-prefix-first so
# that G12AS/G12W/G12R/G12N win over the bare G12.
_ZONE_COUNTS: tuple[tuple[str, int], ...] = (
    ("G13", 3),
    ("G12AS", 2),
    ("G12W", 2),
    ("G12R", 2),
    ("G12N", 2),
    ("G12", 2),
    ("G11", 1),
)

# Row values that mean "the whole day", i.e. no zone split. Energa emits
# "Strefa całodobowa:" as the sole zone of a G11 meter; treat that as no zone
# rather than inventing a z1 stream for single-zone users.
_TOTAL_LABELS = frozenset(
    {
        "",
        "0",
        "calodobowa",
        "calodobowy",
        "calodobowe",
        "calodobo",
        "total",
        "suma",
        "razem",
        "all",
        "allday",
    }
)

# Direction markers, checked against the normalised label. OBIS 1.8.0 is the
# import register, 2.8.0 the export one.
_IMPORT_MARKERS = ("a+", "1.8.0", "180", "import", "pobor", "pobrana", "pobrane", "consumption")
_EXPORT_MARKERS = ("a-", "2.8.0", "280", "export", "oddanie", "oddana", "oddane", "wprowadzona")

# "strefa 2", "zone2", "z2", "l2", "s2", "tariff 3" -> the trailing digit.
_ZONE_NUMBER_RE = re.compile(
    r"^(?:strefa|zone|zona|z|l|s|t|tariff|taryfa|register|rejestr)?\s*0*([123])$"
)

# Named zones, after normalisation.
_NAMED_ZONES = {
    "dzien": ZONE_1,
    "dzienna": ZONE_1,
    "dzienny": ZONE_1,
    "day": ZONE_1,
    "szczyt": ZONE_1,
    "szczytowa": ZONE_1,
    "peak": ZONE_1,
    "noc": ZONE_2,
    "nocna": ZONE_2,
    "nocny": ZONE_2,
    "night": ZONE_2,
    "pozaszczytowa": ZONE_2,
    "offpeak": ZONE_2,
}


def _strip_accents(value: str) -> str:
    """Fold Polish diacritics so 'dzień'/'całodobowa' match plain ASCII keys."""
    replaced = value.replace("ł", "l").replace("Ł", "L")
    decomposed = unicodedata.normalize("NFKD", replaced)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _normalise_label(value: object) -> str:
    """Lowercase, de-accent and strip punctuation/whitespace from a zone label."""
    text = _strip_accents(str(value)).lower().strip()
    # Drop trailing punctuation from labels like "Strefa 1 (dzienna):".
    text = text.replace("(", " ").replace(")", " ").replace(":", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_tariff(code: object) -> str:
    """Return a tariff code in canonical form, e.g. ``' g12w '`` -> ``'G12W'``."""
    if code is None:
        return ""
    return re.sub(r"\s+", "", str(code)).upper()


def zone_count(code: object) -> int:
    """Number of settlement zones for a tariff code.

    Unknown codes fall back to 1 — a wrong extra zone would create empty
    statistic streams, while a missing one merely keeps today's behaviour.
    """
    normalised = normalize_tariff(code)
    if not normalised:
        return 1
    for prefix, count in _ZONE_COUNTS:
        if normalised.startswith(prefix):
            return count
    return 1


def is_known_tariff(code: object) -> bool:
    """Whether the tariff code matched a known G-group prefix."""
    normalised = normalize_tariff(code)
    return any(normalised.startswith(prefix) for prefix, _ in _ZONE_COUNTS)


def zone_keys_for_count(count: int) -> list[str]:
    """Zone slugs for a zone count: 1 -> [], 2 -> [z1, z2], 3 -> [z1, z2, z3].

    A single-zone tariff has no zone dimension at all, hence the empty list.
    """
    if count < 2:
        return []
    return list(ZONE_KEYS[:count])


def split_direction(value: object) -> tuple[str, str]:
    """Split a raw zone label into (direction, remaining label).

    Energa encodes both in one string — ``"A+ strefa 1"`` is import zone 1,
    ``"A- strefa 2"`` is export zone 2 — so the marker has to come off before
    the zone can be read.
    """
    label = _normalise_label(value)
    if not label:
        return DIRECTION_IMPORT, ""

    for marker in _EXPORT_MARKERS:
        if marker in label:
            return DIRECTION_EXPORT, label.replace(marker, " ", 1).strip()
    for marker in _IMPORT_MARKERS:
        if marker in label:
            return DIRECTION_IMPORT, label.replace(marker, " ", 1).strip()
    return DIRECTION_IMPORT, label


def zone_label_to_key(value: object) -> str | None:
    """Map a zone label to a stable slug, or ``None`` for a whole-day label.

    Handles the three shapes seen across Polish operator APIs: bare numbers
    (``1``, ``"2"``), prefixed forms (``"Z1"``, ``"L2"``, ``"strefa 1"``) and
    descriptive labels (``"Strefa 2 (nocna)"``, ``"dzień"``). Anything else is
    slugified and kept, so an unrecognised zone still gets its own stream
    instead of being silently merged into the total.
    """
    label = _normalise_label(value)
    if not label:
        return None

    collapsed = label.replace(" ", "")
    if collapsed in _TOTAL_LABELS:
        return None
    # "strefa calodobowa" and friends.
    if any(total and total in collapsed for total in _TOTAL_LABELS if len(total) > 4):
        return None

    match = _ZONE_NUMBER_RE.match(label) or _ZONE_NUMBER_RE.match(collapsed)
    if match:
        return f"z{match.group(1)}"

    for token in label.split():
        if token in _NAMED_ZONES:
            return _NAMED_ZONES[token]
        inner = _ZONE_NUMBER_RE.match(token)
        if inner:
            return f"z{inner.group(1)}"

    if collapsed in _NAMED_ZONES:
        return _NAMED_ZONES[collapsed]

    slug = re.sub(r"[^a-z0-9]+", "_", label).strip("_")
    return slug or None


def zone_display_name(zone: str | None) -> str:
    """Human-readable zone name for statistic metadata."""
    if zone is None:
        return ""
    return ZONE_DISPLAY_NAMES.get(zone, zone.replace("_", " ").title())
