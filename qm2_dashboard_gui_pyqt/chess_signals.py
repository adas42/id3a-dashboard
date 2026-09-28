"""
chess_signals.py — shared CHESS live-signal helpers.

Meant to be imported by more than one script: the SPEC Dashboard's Summary
tab (spec_dashboard_qt.py, this package) uses it for its live readout cards,
and a Slack-bot-style monitor script could import the network half to poll
signals.chess.cornell.edu directly.

Two independent ways to get a channel's live value, used together:

1. SPEC-file column matching (`match_spec_columns` / `get_spec_live_values`)
   -- always available offline, reads the latest row of whatever SPEC file
   is currently loaded. Column names aren't numbered consistently between
   experiments/SPEC files, so channels are matched by flexible,
   case-insensitive name patterns rather than fixed column positions.

2. Direct network polling of signals.chess.cornell.edu
   (`ChessSignalsClient.get_values`) -- opt-in only, since that host is only
   reachable from on-site/the CHESS network.

Which channels exist, and their labels, SPEC column patterns, PV names,
multipliers and valid ranges, all come from the `signals:` section of the
beamline YAML config (configs/*.yaml), applied with configure(). Nothing
beamline-specific is hard-coded here; configs/qm2.yaml records where each
QM2/ID4B PV came from and how far it has been verified.
"""

import re
from typing import Dict, List, Optional

# Set by configure() from the beamline config's `signals:` section.
BASE_URL = "http://signals.chess.cornell.edu"

# Status page (e.g. new-status.chess.cornell.edu/ID4B) whose human-readable
# status message ("Investigating", "Refilling", "Beam Lost", operator notes,
# ...) is added to Slack alerts by fetch_beam_status_message(). None means
# the beamline has no such page configured.
NEW_STATUS_URL: Optional[str] = None

# "No beam" detection for the Summary tab's banner: a reading within this
# tolerance of 0 counts as no beam, rather than requiring an exact 0.0, since
# monitors can sit at a small nonzero "dark current" baseline.
NO_BEAM_THRESHOLD = 0.5

# canonical name -> {"label", "spec_patterns", "pv", "multiplier", "range"}
CHANNELS: Dict[str, Dict] = {}
_COMPILED_PATTERNS: Dict[str, List["re.Pattern"]] = {}

# Regex patterns tried, in order, to pull the current status text out of
# the status page's HTML. The page's exact markup has not been inspected
# directly, so this tries a few plausible id="statusmsgnow"-style patterns.
# If none match the real markup once run on-site, only this list needs
# updating -- fetch_beam_status_message() treats "no match" the same as
# "page unreachable".
_STATUS_MESSAGE_PATTERNS = [
    re.compile(r'id=["\']statusmsgnow["\'][^>]*>(.*?)<', re.IGNORECASE | re.DOTALL),
    re.compile(r'id=["\']statuscache["\'][^>]*>(.*?)<', re.IGNORECASE | re.DOTALL),
    re.compile(r'statusmsgnow["\']?\s*[:=]\s*["\'](.*?)["\']', re.IGNORECASE),
]


def configure(signals_cfg: Dict) -> None:
    """Load the endpoints and channel definitions from the beamline config's
    `signals:` section (see configs/qm2.yaml for the format)."""
    global BASE_URL, NEW_STATUS_URL, NO_BEAM_THRESHOLD, CHANNELS, _COMPILED_PATTERNS
    BASE_URL = signals_cfg.get("base_url") or BASE_URL
    NEW_STATUS_URL = signals_cfg.get("status_page_url")
    no_beam = signals_cfg.get("no_beam") or {}
    NO_BEAM_THRESHOLD = float(no_beam.get("threshold", NO_BEAM_THRESHOLD))

    CHANNELS = {}
    for canonical, info in (signals_cfg.get("channels") or {}).items():
        info = info or {}
        CHANNELS[canonical] = {
            "label": info.get("label") or canonical,
            "spec_patterns": [str(p).lower() for p in info.get("spec_patterns") or []],
            "pv": info.get("pv"),
            "multiplier": info.get("multiplier", 1),
            "range": info.get("range"),
        }

    # Each pattern is only allowed to match a column name where it isn't
    # immediately preceded/followed by another digit. Without this, "ic1"
    # would also match "ic10"/"ic11" -- a different channel on beamlines
    # with more ion chambers -- and give a confidently-wrong number.
    _COMPILED_PATTERNS = {
        canonical: [
            re.compile(r"(?<!\d)" + re.escape(pat) + r"(?!\d)")
            for pat in info["spec_patterns"]
        ]
        for canonical, info in CHANNELS.items()
    }


def fetch_beam_status_message(
    url: Optional[str] = None, timeout: float = 5.0, session=None
) -> Optional[str]:
    """GET the beamline status page (NEW_STATUS_URL unless `url` is given)
    and pull out the current status message text, so Slack alerts can
    include it alongside the plain "No Beam"/"Beam Restored" text.
    `requests` is imported lazily so the rest of this module keeps working
    where it isn't installed.

    Only reachable from on-site/the CHESS network -- returns None on any
    failure (no URL configured, unreachable, non-200, no pattern match,
    requests not installed, ...) rather than raising, so the alert just
    falls back to the generic text.

    NOTE: the page may render its status text with JavaScript after load
    (the original monitoring script read it with headless Chromium via
    Playwright). A plain GET + regex only finds text that's in the raw HTML;
    if this always returns None on-site, that's the likely reason."""
    url = url or NEW_STATUS_URL
    if not url:
        return None
    try:
        import requests

        requester = session if session is not None else requests
        resp = requester.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        )
    except Exception:
        return None
    if getattr(resp, "status_code", None) != 200:
        return None
    html = resp.text
    for pattern in _STATUS_MESSAGE_PATTERNS:
        match = pattern.search(html)
        if match:
            text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            if text:
                return text
    return None


def is_no_beam(value: Optional[float], threshold: Optional[float] = None) -> bool:
    """True if value is close enough to 0 (abs(value) < threshold) to be
    treated as "no beam". Returns False for None -- no reading at all is
    "unknown", not a confirmed no-beam state."""
    if value is None:
        return False
    if threshold is None:
        threshold = NO_BEAM_THRESHOLD
    return abs(value) < threshold


_LEADING_NUMBER = re.compile(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(.*)")


def split_quantity(value) -> Optional[tuple]:
    """(number, unit text) from a PV value: 3.67 -> (3.67, ""),
    "51.996 keV" -> (51.996, "keV"). None if it isn't a number or a text
    starting with one."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value), ""
    match = _LEADING_NUMBER.fullmatch(str(value))
    if not match:
        return None
    return float(match.group(1)), match.group(2).strip()


def leading_number(value) -> Optional[float]:
    """A PV value as a float, reading text like "51.996 keV" by its leading
    number. None if there's no number."""
    quantity = split_quantity(value)
    return quantity[0] if quantity else None


# ---------------------------------------------------------------------
# SPEC-file column matching
# ---------------------------------------------------------------------


def match_spec_columns(columns: List[str]) -> Dict[str, Optional[str]]:
    """Match every configured channel against a SPEC file's column header
    list, case-insensitively. Returns {canonical: actual_column_name or
    None}. Tries an exact match against each pattern first, then falls back
    to a digit-boundary-protected substring match, so this works whether a
    column is literally named "cesr" or something like "CESR_mon" -- while
    still preferring an exact match when both are present."""
    lower_map: Dict[str, str] = {}
    for c in columns or []:
        lower_map.setdefault(c.lower(), c)

    result: Dict[str, Optional[str]] = {}
    for canonical, info in CHANNELS.items():
        found = None
        for pat in info["spec_patterns"]:
            if pat in lower_map:
                found = lower_map[pat]
                break
        if found is None:
            regexes = _COMPILED_PATTERNS[canonical]
            for col_lower, col_orig in lower_map.items():
                if any(rx.search(col_lower) for rx in regexes):
                    found = col_orig
                    break
        result[canonical] = found
    return result


def get_spec_live_values(df, columns: List[str]) -> Dict[str, Optional[float]]:
    """Read the latest (last-row) value of every configured channel from a
    SPEC dataframe, using match_spec_columns() for name matching. Returns
    {canonical: float or None} -- None for any channel with no matching
    column, an empty/missing dataframe, or a non-numeric last value."""
    values: Dict[str, Optional[float]] = {c: None for c in CHANNELS}
    if df is None or columns is None:
        return values
    try:
        empty = df.empty
    except AttributeError:
        return values
    if empty:
        return values

    col_map = match_spec_columns(columns)
    for canonical, col in col_map.items():
        if col is None or col not in df.columns:
            continue
        try:
            values[canonical] = float(df[col].iloc[-1])
        except (ValueError, TypeError, IndexError, KeyError):
            values[canonical] = None
    return values


# ---------------------------------------------------------------------
# Direct network polling (opt-in; requires on-site/CHESS network access)
# ---------------------------------------------------------------------


class ChessSignalsClient:
    """Thin client for signals.chess.cornell.edu's /plot/UPDATE_{pv}
    endpoint. Only usable from a machine that can actually reach that host
    (on-site / the CHESS network) -- every request will simply fail (and
    get_values() will return None for every channel) from anywhere else.
    `requests` is imported lazily inside __init__ so the rest of this module
    (and the dashboard's SPEC-file-based readouts) keep working on a machine
    where `requests` isn't installed."""

    def __init__(self, base_url: Optional[str] = None, timeout: float = 5.0, session=None):
        self.base_url = base_url or BASE_URL
        self.timeout = timeout
        if session is not None:
            self.session = session
        else:
            import requests  # lazy import -- see docstring above

            self.session = requests.Session()
            self.session.headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Accept-Language": "en-US,en;q=0.9",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{self.base_url}/plot",
                }
            )

    def _fetch_last(self, pv_name: str):
        """GET {base_url}/plot/UPDATE_{pv_name}. The confirmed-working
        response shape is a JSON array; the live value is the last element.
        Returns None on any error (non-200, non-JSON, empty array,
        connection failure, timeout, blocked egress, ...) -- this is
        expected/normal when called from anywhere off the CHESS network."""
        url = f"{self.base_url}/plot/UPDATE_{pv_name}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if isinstance(data, list) and data:
            return data[-1]
        return None

    def fetch_raw(self, pv_name: str) -> Optional[float]:
        """The PV's latest value as a number. Some PVs come back as text
        with units (ID3A_MON_KEV gives "51.996 keV"); those are read by
        their leading number. None if unavailable or not numeric."""
        return leading_number(self._fetch_last(pv_name))

    def fetch_text(self, pv_name: str) -> Optional[str]:
        """The PV's latest value as text, units included (e.g. "51.996 keV",
        "nA/V"). None if unavailable."""
        value = self._fetch_last(pv_name)
        return None if value is None else str(value).strip()

    def get_values(self, pv_map: Dict[str, Dict]) -> Dict[str, Optional[float]]:
        """Fetch + validate + scale every channel in pv_map (a dict shaped
        like CHANNELS). A channel with pv=None is always reported as None
        without making any request for it. "range" validates the RAW value
        (before the multiplier); a null range skips the check."""
        results: Dict[str, Optional[float]] = {}
        for canonical, info in pv_map.items():
            pv = info.get("pv")
            if not pv:
                results[canonical] = None
                continue
            raw = self.fetch_raw(pv)
            if raw is None:
                results[canonical] = None
                continue
            lo, hi = info.get("range") or (None, None)
            if lo is not None and hi is not None and not (lo <= raw <= hi):
                results[canonical] = None
                continue
            results[canonical] = raw * info.get("multiplier", 1)
        return results


# ---------------------------------------------------------------------
# Combined entry point used by the dashboard
# ---------------------------------------------------------------------


def get_live_values(
    df=None,
    columns: Optional[List[str]] = None,
    use_network: bool = False,
    client: Optional[ChessSignalsClient] = None,
) -> Dict[str, Dict]:
    """Get the live value of every configured channel.

    Priority depends on use_network:

    - use_network=False (default): only the SPEC-file column values are
      used (df/columns, if given).

    - use_network=True: for each channel, a live signals.chess.cornell.edu
      reading is preferred over the SPEC file's value, falling back to the
      SPEC-file value only if the network didn't provide one (no PV, or the
      request failed/is unreachable). Network-first, because a loaded SPEC
      file's last row is a static snapshot that generally never changes
      again -- with SPEC-first, turning the network fetch on had no visible
      effect for any channel the file already had a column for.

    Returns {canonical: {"value": float or None, "source": "spec" or
    "network" or None, "column": the matched SPEC column name or PV name
    the value came from, or None}}. "column" lets the caller (e.g. the
    Summary tab's tooltip) show exactly which column/PV produced a number,
    so a wrong match is obvious rather than silently wrong.
    """
    result: Dict[str, Dict] = {
        c: {"value": None, "source": None, "column": None} for c in CHANNELS
    }

    if df is not None and columns:
        col_map = match_spec_columns(columns)
        for c, v in get_spec_live_values(df, columns).items():
            if v is not None:
                result[c] = {"value": v, "source": "spec", "column": col_map.get(c)}

    if use_network:
        active_client = client or ChessSignalsClient()
        for c, v in active_client.get_values(CHANNELS).items():
            if v is not None:
                result[c] = {"value": v, "source": "network", "column": CHANNELS[c]["pv"]}

    return result
