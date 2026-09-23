"""
chess_signals.py — shared CHESS/ID4B live-signal helpers.

Meant to be imported by more than one script: the SPEC Dashboard's Summary
tab (spec_dashboard_qt.py, this package) uses the SPEC-file half of this
module to show live CESR/IC1/IC2/diode/Flow readouts; a Slack-bot-style monitor
script (in the style of chess_beam_monitor_ai.py) could import the network
half to poll signals.chess.cornell.edu directly. Keeping both in one place
means the PV names, multipliers, and endpoint format only need to be gotten
right (and fixed, if they change) in one spot.

Two independent ways to get the beam-monitor numbers (CESR, IC1, IC2, diode,
Flow), used together with SPEC-file-first priority:

1. SPEC-file column matching (`get_spec_live_values` / `match_spec_columns`)
   — always available offline, reads the latest row of whatever SPEC file is
   currently loaded. Column names aren't assumed to be numbered consistently
   between experiments/SPEC files (a real concern the user raised — "SPEC
   will not always number"), so channels are matched by flexible,
   case-insensitive name patterns (CHANNEL_NAME_PATTERNS) rather than fixed
   column positions.

2. Direct network polling of signals.chess.cornell.edu
   (`ChessSignalsClient.get_values`) — opt-in only, since that host is only
   reachable from on-site/the CHESS network (confirmed unreachable from the
   dashboard-development sandbox this file was written in — every attempt to
   reach signals.chess.cornell.edu or new-status.chess.cornell.edu from
   there failed with an egress-blocked error).

   PV names/multiplier history (read this before touching BEAM_PV_MAP):
   the user first shared a temperature-only script (ChessLiveDataExtractor
   / ChessBot) covering just ID4B_CRYOGL_STG1_T/SAM_T/STG2_T -- nothing
   about CESR/IC1/IC2/diode -- and probing ~12 guessed URL patterns rather
   than one confirmed endpoint. An earlier version of this module wrongly
   claimed the ID4B_CNT00/02/03_VLT -> IC1/IC2/diode mapping and x10000
   multiplier below were "confirmed working" from that script -- they
   weren't; that was a mistake, later corrected to all `pv=None`. The user
   subsequently shared a second, fuller script (also ChessLiveDataExtractor
   / ChessBot) whose own ALL_PV_MAPPING dict *does* contain exactly this
   mapping -- ID4B_CNT00_VLT -> "Ion Chamber 1 (IC1)", ID4B_CNT02_VLT ->
   "Ion Chamber 2 (IC2)", ID4B_CNT03_VLT -> "Beam Stop Diode", each with
   multiplier 10000, fetched via a single confirmed `/plot/UPDATE_{pv}`
   request (not a multi-template guess) -- so IC1/IC2/diode below are now
   genuinely sourced from something the user shared, not fabricated. CESR
   was absent from every script the user shared; the user then separately
   told me directly in chat that CESR's PV is `ID4B_CNT01_VLT` (fitting the
   numbering gap between IC1's CNT00 and IC2's CNT02). That PV name is now
   wired in below, but -- unlike ic1/ic2/diode -- it did NOT come from a
   script's own mapping dict and I have not independently verified it
   (still can't reach either signals.chess.cornell.edu or
   new-status.chess.cornell.edu from this sandbox). The multiplier for
   cesr is also unknown and is left at 1 (no scaling) rather than assumed
   to match ic1/ic2/diode's x10000, since CESR (beam current, mA) is a
   different kind of reading than the ion-chamber/diode voltages. Worth
   sanity-checking the dashboard's live CESR number against
   new-status.chess.cornell.edu/ID4B (currently reading ~0.01 mA) once
   this is run on-site.

BEAM_PV_MAP: IC1/IC2/diode have real, user-confirmed PVs (from a script's
own mapping dict). CESR has a PV (ID4B_CNT01_VLT) the user told me directly
in chat, not independently verified, with an unconfirmed (placeholder 1x)
multiplier -- see above.

Also wired in (this round): the 3 cryostat temperature channels (stage1/
sample/stage2, TEMPERATURE_PV_MAP) as a second, parallel set of Summary-tab
readouts -- "the same as CESR/IC1/IC2/diode" per the user's request. These
PVs (ID4B_CRYOGL_STG1_T/SAM_T/STG2_T) are the most solidly-sourced in this
module: they're both what the user's very first script used AND what the
user separately re-typed/confirmed directly in chat, so there's no "told
me but unverified" caveat needed the way there is for CESR. Mirrors the
beam-channel machinery exactly (match_temperature_columns(),
get_spec_live_temperatures(), get_live_temperature_values()) rather than
reusing/overloading the beam-channel functions, so the two stay fully
independent and adding temperature support can't change beam-channel
behavior.

Also wired in (this round): a "flow" readout, after the user shared the
real SPEC macro that reads it (flow_get, in
.../Macros/surrena/aalborg_flow.mac -- "Aalborg" being a mass-flow-
controller brand, so this is very likely a cryostat/cryojet gas flow
reading). Unlike CESR or the temperature channels, this one is added to
the existing generic beam-channel machinery (CHANNEL_NAME_PATTERNS/
CHANNEL_LABELS/CHANNEL_ORDER/BEAM_PV_MAP) rather than given its own
parallel set of functions, since "flow" behaves exactly like a 5th beam
channel and that machinery is already fully generic over those dicts.
"flow" is a genuine, already-logged column in the sample SPEC data's own
#L header line (right alongside cesr/ic1/ic2/diode), so it works
immediately via the existing SPEC-file-column path with no other code
changes. The shared macro only shows a SPEC-internal function call
(flow = _flow_get()), not an HTTP endpoint or PV name, so unlike ic1/ic2/
diode/cesr there's no known network PV for it yet -- BEAM_PV_MAP's entry
for "flow" leaves pv=None, which ChessSignalsClient.get_values() skips
entirely (same treatment CESR got before its PV was known). If a real
signals.chess.cornell.edu PV for flow turns up, it can be added the same
way CESR's was.
"""

import re
from typing import Dict, List, Optional

BASE_URL = "http://signals.chess.cornell.edu"

# The full ID4B status page (as opposed to signals.chess.cornell.edu's raw
# PV endpoint above) -- shows a human-readable current status message
# (things like "Investigating", "Refilling", "Beam Lost", operator notes,
# etc.) alongside the CESR mA reading. Used by fetch_beam_status_message()
# below so Slack alerts can include that text, not just a generic "No
# Beam"/"Beam Restored" line.
NEW_STATUS_URL = "http://new-status.chess.cornell.edu/ID4B"

# Regex patterns tried, in order, to pull the current status text out of
# the new-status page's HTML. This page's exact markup has not been
# directly inspected from this sandbox -- new-status.chess.cornell.edu is
# unreachable from here, same as signals.chess.cornell.edu (see
# ChessSignalsClient's docstring below) -- so this tries a few plausible
# id="statusmsgnow"-style patterns rather than assuming one exact
# tag/structure. If none of these match the page's real markup once run
# on-site, only this list needs updating -- everything that calls
# fetch_beam_status_message() just treats "no match" the same as "page
# unreachable" (returns None, alert falls back to the generic text).
_STATUS_MESSAGE_PATTERNS = [
    re.compile(r'id=["\']statusmsgnow["\'][^>]*>(.*?)<', re.IGNORECASE | re.DOTALL),
    re.compile(r'id=["\']statuscache["\'][^>]*>(.*?)<', re.IGNORECASE | re.DOTALL),
    re.compile(r'statusmsgnow["\']?\s*[:=]\s*["\'](.*?)["\']', re.IGNORECASE),
]


def fetch_beam_status_message(
    url: str = NEW_STATUS_URL, timeout: float = 5.0, session=None
) -> Optional[str]:
    """GET new-status.chess.cornell.edu/ID4B and pull out the current
    status message text (whatever populates the page's #statusmsgnow
    element -- things like "Investigating", "Refilling", "Beam Lost",
    operator names, etc.), so Slack alerts can include it alongside the
    plain "No Beam"/"Beam Restored" text. `requests` is imported lazily
    (like ChessSignalsClient below) so the rest of this module keeps
    working even where `requests` isn't installed.

    Only reachable from on-site/the CHESS network -- returns None on any
    failure (unreachable, non-200, no pattern match, requests not
    installed, ...) rather than raising, so a failed fetch just means the
    Slack alert falls back to the generic text instead of breaking the
    alert entirely.

    NOTE: this page may render its status text via JavaScript after the
    initial page load rather than including it in the raw HTML response --
    the user's own original monitoring script read it via a full headless-
    Chromium browser (Playwright), not a plain HTTP GET, which suggests
    this may be the case. A plain GET + regex, as done here, only finds
    the text if it's actually present in the HTML that comes back from the
    GET itself. If this always returns None once run on-site even though
    the page clearly shows a message, that's the most likely reason -- the
    fix would be to swap this for a Playwright-based fetch instead (a much
    heavier dependency, so not the default here)."""
    try:
        import requests

        sess = session
        if sess is None:
            resp = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
            )
        else:
            resp = sess.get(
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

# "No beam" detection for the Summary tab's banner. CESR near 0 (within
# this tolerance) is treated as "no beam" rather than requiring an exact
# 0.0 match, since ion chambers/diodes (and possibly CESR itself) can sit
# at a small nonzero "dark current" baseline that wanders a bit even with
# genuinely no beam. This is a placeholder value, not a confirmed CHESS
# spec -- worth revisiting once CESR's PV/multiplier are independently
# verified (see BEAM_PV_MAP above), since the multiplier is currently an
# unconfirmed 1x and could change what "near 0" should mean numerically.
NO_BEAM_CESR_THRESHOLD = 0.5


def is_no_beam(cesr_value: Optional[float], threshold: float = NO_BEAM_CESR_THRESHOLD) -> bool:
    """True if cesr_value is close enough to 0 (abs(value) < threshold) to
    be treated as "no beam". Returns False (not "no beam") for None --
    i.e. when there's simply no CESR reading available at all (no matching
    SPEC column and network fetch off/failed), that's "unknown", not a
    confirmed no-beam state, so the banner shouldn't claim no beam just
    because it doesn't know."""
    if cesr_value is None:
        return False
    return abs(cesr_value) < threshold

# ---------------------------------------------------------------------
# SPEC-file column matching
# ---------------------------------------------------------------------

# Canonical channel name -> list of lowercase substrings to match against
# SPEC column headers. Checked in order; the first pattern that matches
# ANY column (exact match first, then substring) wins. Extend these lists
# if a particular beamline/SPEC file uses a different naming convention.
#
# "flow" added after the user shared a real SPEC macro that reads it
# (`flow_get`, in .../Macros/surrena/aalborg_flow.mac -- "Aalborg" being a
# mass-flow-controller brand, so this is very likely a cryostat/cryojet gas
# flow reading). Unlike CESR, this one didn't need any guessing: "flow" is
# already a genuine logged column in the sample SPEC data's own #L header
# line (right alongside cesr/ic1/ic2/diode/sampleT), so it's matched via
# the same SPEC-file-column path those channels use, no PV needed for that
# to work. The macro itself only shows a SPEC-side call
# (`flow = _flow_get()`) -- not an HTTP endpoint/PV name -- so unlike ic1/
# ic2/diode/cesr there's currently no known network PV for it; BEAM_PV_MAP
# below leaves flow's pv as None (skipped entirely by
# ChessSignalsClient.get_values(), same as every other channel started out
# before its PV was known). If there's a signals.chess.cornell.edu PV for
# flow, it can be added the same way CESR's was.
CHANNEL_NAME_PATTERNS: Dict[str, List[str]] = {
    "cesr": ["cesr"],
    "ic1": ["ic1", "ion_chamber1", "ion_chamber_1", "ionchamber1"],
    "ic2": ["ic2", "ion_chamber2", "ion_chamber_2", "ionchamber2"],
    "diode": ["diode", "pin_diode", "pindiode", "beam_stop_diode", "beamstopdiode"],
    "flow": ["flow", "gas_flow", "flow_rate", "cryo_flow"],
}

# Display order + labels for the Summary tab readouts. "flow" -> "Flow
# Rate" per the user's explicit renaming request; note the *value*-fetching
# side (CHANNEL_NAME_PATTERNS key, BEAM_PV_MAP entry, get_live_beam_values()
# result key) is still the lowercase "flow" canonical name -- only this
# display label changed, so nothing else needed to change to pick it up.
CHANNEL_LABELS: Dict[str, str] = {
    "cesr": "CESR",
    "ic1": "IC1 (Ion Chamber 1)",
    "ic2": "IC2 (Ion Chamber 2)",
    "diode": "Diode",
    "flow": "Flow Rate",
}
CHANNEL_ORDER: List[str] = ["cesr", "ic1", "ic2", "diode", "flow"]

# Compiled once: each pattern is only allowed to match a column name where
# it isn't immediately preceded/followed by another digit. Without this, a
# plain substring check for "ic1" would also match columns like "ic10" or
# "ic11" -- a real, different, differently-numbered channel on beamlines
# that have more than 2 ion chambers -- which is exactly the kind of
# false-positive match that produces a confidently-wrong number instead of
# an honest "no match". (Reported: a real SPEC file's "ic1"/"cesr" readouts
# came out wrong while "ic2"/"diode" came out right -- consistent with an
# accidental match onto a same-prefix, different-numbered column for the
# other two.)
_COMPILED_PATTERNS: Dict[str, List["re.Pattern"]] = {
    canonical: [re.compile(r"(?<!\d)" + re.escape(pat) + r"(?!\d)") for pat in patterns]
    for canonical, patterns in CHANNEL_NAME_PATTERNS.items()
}


def match_spec_columns(columns: List[str]) -> Dict[str, Optional[str]]:
    """Match the 4 canonical channel names against a SPEC file's column
    header list, case-insensitively. Returns {canonical: actual_column_name
    or None}. Tries an exact (case-insensitive) match against each pattern
    first, then falls back to a substring match, so this works whether a
    given SPEC file's column is literally named "cesr" or something like
    "CESR_mon" -- while still preferring an exact match over a looser one
    when both are present. The substring fallback requires the pattern not
    be immediately adjacent to another digit, so "ic1" won't accidentally
    match a genuinely different channel like "ic10" or "ic11"."""
    lower_map: Dict[str, str] = {}
    for c in columns or []:
        lower_map.setdefault(c.lower(), c)

    result: Dict[str, Optional[str]] = {}
    for canonical, patterns in CHANNEL_NAME_PATTERNS.items():
        found = None
        for pat in patterns:
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


def get_spec_live_values(
    df, columns: List[str]
) -> Dict[str, Optional[float]]:
    """Read the latest (last-row) value of each of the 4 canonical beam-
    monitor channels from a SPEC dataframe, using match_spec_columns() for
    name matching. Returns {canonical: float or None} -- None for any
    channel with no matching column, an empty/missing dataframe, or a
    non-numeric last value."""
    values: Dict[str, Optional[float]] = {c: None for c in CHANNEL_NAME_PATTERNS}
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

# canonical -> {pv, multiplier, range}. "range" validates the RAW value
# read back from the endpoint (before the multiplier is applied) -- for
# ic1/ic2/diode this matches the validation the user's own script performs
# (-10 to 100 V, accepting a wide swing including negative/near-zero
# readings for an off/low beam).
#
# ic1/ic2/diode are confirmed by a real script the user shared (its own
# ALL_PV_MAPPING dict). cesr's PV (ID4B_CNT01_VLT) was told to me directly
# by the user in chat -- it plausibly fits the numbering gap between IC1's
# ID4B_CNT00_VLT and IC2's ID4B_CNT02_VLT, but unlike ic1/ic2/diode it does
# NOT come from a script's own mapping dict, and I have not independently
# verified it (this sandbox can't reach signals.chess.cornell.edu or
# new-status.chess.cornell.edu to check). The multiplier for cesr is
# UNKNOWN and deliberately left at 1 (no scaling) rather than guessing
# ic1/ic2/diode's x10000 -- CESR beam current is a different kind of
# reading (mA, from new-status.chess.cornell.edu, currently ~0.01 mA)
# than the ion-chamber/diode voltages, so there's no reason to assume the
# same scale factor applies. The range below is a permissive placeholder
# (not derived from any confirmed spec) just wide enough not to reject a
# plausible reading outright. TODO once this is genuinely live: compare
# the dashboard's displayed CESR value against the reading shown on
# new-status.chess.cornell.edu/ID4B and adjust the multiplier/range here
# if they don't match.
BEAM_PV_MAP: Dict[str, Dict] = {
    "cesr": {"pv": "ID4B_CNT01_VLT", "multiplier": 1, "range": (-10, 1000)},
    "ic1": {"pv": "ID4B_CNT00_VLT", "multiplier": 10000, "range": (-10, 100)},
    "ic2": {"pv": "ID4B_CNT02_VLT", "multiplier": 10000, "range": (-10, 100)},
    "diode": {"pv": "ID4B_CNT03_VLT", "multiplier": 10000, "range": (-10, 100)},
    # No known network PV for flow -- the user's flow_get SPEC macro calls
    # a SPEC-side function (_flow_get()), not an HTTP/PV endpoint, so there
    # was nothing to wire in here without guessing. pv=None means
    # ChessSignalsClient.get_values() skips it entirely (no request made,
    # always reports None) -- same treatment CESR got before its PV was
    # known. Flow still works fine as a Summary-tab readout via the SPEC-
    # file-column path (get_spec_live_values()), since "flow" is a real
    # logged column in the sample SPEC data. Add a real pv/multiplier/range
    # here the same way CESR's was added, if one turns up.
    "flow": {"pv": None, "multiplier": 1, "range": (-10, 100)},
}

# The cryostat temperature PVs from the user's original temperature-only
# script (ChessLiveDataExtractor/ChessBot) -- these three PV names are the
# most solidly-sourced of anything in this module: they came from that
# script AND the user separately re-typed/confirmed them directly in chat
# (ID4B_CRYOGL_STG1_T, ID4B_CRYOGL_SAM_T, ID4B_CRYOGL_STG2_T), so unlike
# CESR's PV there's no "user told me, unverified" caveat needed here. The
# multiplier is left at 1 (no scaling) since temperature readings don't
# have the ion-chamber/diode-style x10000 raw-voltage-to-reading scaling --
# and the (50, 400) range is a permissive placeholder wide enough to admit
# plausible Kelvin cryostat readings without rejecting real values, not a
# confirmed CHESS spec.
TEMPERATURE_PV_MAP: Dict[str, Dict] = {
    "stage1": {"pv": "ID4B_CRYOGL_STG1_T", "multiplier": 1, "range": (50, 400)},
    "sample": {"pv": "ID4B_CRYOGL_SAM_T", "multiplier": 1, "range": (50, 400)},
    "stage2": {"pv": "ID4B_CRYOGL_STG2_T", "multiplier": 1, "range": (50, 400)},
}

# Canonical temperature channel name -> SPEC column name patterns, mirroring
# CHANNEL_NAME_PATTERNS above but for the 3 cryostat temperature channels
# instead of the 4 beam-monitor channels. Kept as a separate dict (not
# merged into CHANNEL_NAME_PATTERNS) so beam-channel matching/behavior is
# completely unaffected by adding temperature support.
TEMPERATURE_NAME_PATTERNS: Dict[str, List[str]] = {
    "stage1": ["stage1", "stg1", "cryo_stage1", "cryo_stg1", "cryogl_stg1"],
    "sample": ["sample_t", "sample_temp", "sampletemp", "cryo_sample", "cryogl_sam"],
    "stage2": ["stage2", "stg2", "cryo_stage2", "cryo_stg2", "cryogl_stg2"],
}

TEMPERATURE_LABELS: Dict[str, str] = {
    "stage1": "Stage 1 (A)",
    "sample": "Sample Temp",
    "stage2": "Stage 2 (C)",
}
TEMPERATURE_ORDER: List[str] = ["stage1", "sample", "stage2"]

# Same digit-boundary protection as _COMPILED_PATTERNS above (e.g. so a
# "stage1" pattern can't accidentally match a differently-numbered
# "stage10" column on some other beamline's SPEC file).
_TEMP_COMPILED_PATTERNS: Dict[str, List["re.Pattern"]] = {
    canonical: [re.compile(r"(?<!\d)" + re.escape(pat) + r"(?!\d)") for pat in patterns]
    for canonical, patterns in TEMPERATURE_NAME_PATTERNS.items()
}


def match_temperature_columns(columns: List[str]) -> Dict[str, Optional[str]]:
    """Match the 3 canonical temperature channel names (stage1/sample/
    stage2) against a SPEC file's column header list. Same exact-then-
    substring, case-insensitive, digit-boundary-protected matching
    strategy as match_spec_columns() -- see that function's docstring --
    just against TEMPERATURE_NAME_PATTERNS instead of
    CHANNEL_NAME_PATTERNS."""
    lower_map: Dict[str, str] = {}
    for c in columns or []:
        lower_map.setdefault(c.lower(), c)

    result: Dict[str, Optional[str]] = {}
    for canonical, patterns in TEMPERATURE_NAME_PATTERNS.items():
        found = None
        for pat in patterns:
            if pat in lower_map:
                found = lower_map[pat]
                break
        if found is None:
            regexes = _TEMP_COMPILED_PATTERNS[canonical]
            for col_lower, col_orig in lower_map.items():
                if any(rx.search(col_lower) for rx in regexes):
                    found = col_orig
                    break
        result[canonical] = found
    return result


def get_spec_live_temperatures(df, columns: List[str]) -> Dict[str, Optional[float]]:
    """Read the latest (last-row) value of each of the 3 canonical cryostat
    temperature channels from a SPEC dataframe, using
    match_temperature_columns() for name matching. Mirrors
    get_spec_live_values() exactly, just for temperatures instead of beam-
    monitor channels. Returns {canonical: float or None}."""
    values: Dict[str, Optional[float]] = {c: None for c in TEMPERATURE_NAME_PATTERNS}
    if df is None or columns is None:
        return values
    try:
        empty = df.empty
    except AttributeError:
        return values
    if empty:
        return values

    col_map = match_temperature_columns(columns)
    for canonical, col in col_map.items():
        if col is None or col not in df.columns:
            continue
        try:
            values[canonical] = float(df[col].iloc[-1])
        except (ValueError, TypeError, IndexError, KeyError):
            values[canonical] = None
    return values


def get_live_temperature_values(
    df=None,
    columns: Optional[List[str]] = None,
    use_network: bool = False,
    client: Optional["ChessSignalsClient"] = None,
) -> Dict[str, Dict]:
    """Get the 3 live cryostat temperature values (stage1, sample, stage2).
    Same SPEC-file-first-then-network-overrides priority as
    get_live_beam_values() -- see that function's docstring for the full
    rationale (network-first when use_network=True, so toggling the
    checkbox has a visible effect even when the SPEC file already has
    matching columns). Returns the same {canonical: {"value", "source",
    "column"}} shape, just for "stage1"/"sample"/"stage2" instead of
    "cesr"/"ic1"/"ic2"/"diode"."""
    result: Dict[str, Dict] = {
        c: {"value": None, "source": None, "column": None} for c in TEMPERATURE_NAME_PATTERNS
    }

    if df is not None and columns:
        col_map = match_temperature_columns(columns)
        for c, v in get_spec_live_temperatures(df, columns).items():
            if v is not None:
                result[c] = {"value": v, "source": "spec", "column": col_map.get(c)}

    if use_network:
        active_client = client or ChessSignalsClient()
        for c, v in active_client.get_values(TEMPERATURE_PV_MAP).items():
            if v is not None:
                result[c] = {
                    "value": v,
                    "source": "network",
                    "column": TEMPERATURE_PV_MAP.get(c, {}).get("pv"),
                }

    return result


class ChessSignalsClient:
    """Thin client for signals.chess.cornell.edu's /plot/UPDATE_{pv}
    endpoint. Only usable from a machine that can actually reach that host
    (on-site / the CHESS network) -- every request will simply fail (and
    get_values() will return None for every channel) from anywhere else,
    including this dashboard's own development/test sandbox. `requests` is
    imported lazily inside __init__ so the rest of this module (and the
    dashboard's SPEC-file-based readouts) keep working even on a machine
    where `requests` isn't installed."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 5.0, session=None):
        self.base_url = base_url
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

    def fetch_raw(self, pv_name: str) -> Optional[float]:
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
            try:
                return float(data[-1])
            except (ValueError, TypeError):
                return None
        return None

    def get_values(self, pv_map: Dict[str, Dict]) -> Dict[str, Optional[float]]:
        """Fetch + validate + scale every channel in pv_map (a dict shaped
        like BEAM_PV_MAP/TEMPERATURE_PV_MAP). A channel with pv=None (e.g.
        "cesr" until its real PV name is known) is always reported as
        None without making any request for it."""
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
            lo, hi = info.get("range", (None, None))
            if lo is not None and hi is not None and not (lo <= raw <= hi):
                results[canonical] = None
                continue
            results[canonical] = raw * info.get("multiplier", 1)
        return results


# ---------------------------------------------------------------------
# Combined entry point used by the dashboard
# ---------------------------------------------------------------------


def get_live_beam_values(
    df=None,
    columns: Optional[List[str]] = None,
    use_network: bool = False,
    client: Optional[ChessSignalsClient] = None,
) -> Dict[str, Dict]:
    """Get the live beam-monitor values (CESR, IC1, IC2, diode, Flow).

    Priority depends on use_network:

    - use_network=False (default): only the SPEC-file column values are
      used (df/columns, if given).

    - use_network=True: for each channel, a live signals.chess.cornell.edu
      reading (ChessSignalsClient.get_values(BEAM_PV_MAP)) is preferred
      over the SPEC file's value, falling back to the SPEC-file value only
      if the network didn't provide one for that channel (no confirmed PV
      for it yet, e.g. CESR, or the request failed/is unreachable). This
      is deliberately network-first rather than SPEC-first when the
      network path is turned on: a loaded SPEC file is a static snapshot
      of its last row and generally does NOT change again once loaded
      (the app only re-reads it if the file itself changes on disk), so
      an earlier SPEC-first version of this function meant checking "Try
      live network fetch" had *no effect at all* on any channel the SPEC
      file already had a column for -- the readout would just silently
      keep showing the same frozen SPEC-file number forever, which is
      exactly the "none of the values changed" behavior a user reported
      seeing. Network-first fixes that: with the checkbox on, IC1/IC2/
      Diode should now actually update over time (once genuinely reachable
      -- this sandbox still can't reach signals.chess.cornell.edu to
      verify that end-to-end).

    Returns {canonical: {"value": float or None, "source": "spec" or
    "network" or None, "column": the matched SPEC column name or PV name
    that the value came from, or None}} for each of "cesr", "ic1", "ic2",
    "diode" -- the "column" entry is included specifically so a caller
    (e.g. the Summary tab's tooltip) can show exactly which column/PV
    produced a given number, to make a wrong match (matching the wrong
    column) obvious/diagnosable rather than just silently confidently
    wrong.
    """
    result: Dict[str, Dict] = {
        c: {"value": None, "source": None, "column": None} for c in CHANNEL_NAME_PATTERNS
    }

    if df is not None and columns:
        col_map = match_spec_columns(columns)
        for c, v in get_spec_live_values(df, columns).items():
            if v is not None:
                result[c] = {"value": v, "source": "spec", "column": col_map.get(c)}

    if use_network:
        active_client = client or ChessSignalsClient()
        for c, v in active_client.get_values(BEAM_PV_MAP).items():
            if v is not None:
                # Network-first: overwrite whatever the SPEC file had for
                # this channel, since the network reading is the genuinely
                # live one. Channels the network didn't provide a value for
                # (no confirmed PV, request failed, etc.) simply keep
                # whatever the SPEC-file loop above already set (or stay
                # None if that didn't match either).
                result[c] = {
                    "value": v,
                    "source": "network",
                    "column": BEAM_PV_MAP.get(c, {}).get("pv"),
                }

    return result
