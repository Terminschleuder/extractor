"""Feed discovery + calendar parsing for JS-rendered event pages.

Some event sites render their calendar with JavaScript: the initial HTML is a
skeleton ("loading…") and the events are fetched from a separate feed endpoint
referenced only in a script. The LLM extractor's httpx fetch gets no events from
such a page, so this module discovers that data feed and parses it directly.

Discovery is general (site-agnostic):
  * ``<link rel="alternate" type="text/calendar|application/rss+xml|...">`` hrefs,
  * feed-ish URLs (``ical``, ``.ics``, ``calendar``, ``feed``, ``json``,
    ``wp-json``, …) found in inline ``<script>`` text and in externally-linked
    ``.js`` files (bounded fetch — ``max_scripts``).

Parsing:
  * **jcal** (JSON Calendar, ``application/calendar+json``) — parsed directly
    from JSON, no dependency.
  * **iCal** (``text/calendar`` / ``BEGIN:VCALENDAR``) — a small hand-rolled
    vevent parser (line unfolding, ``DATE`` vs ``DATE-TIME``, ``TZID``).
  * Anything else (RSS/Atom/JSON/HTML) is returned as ``None`` so the caller can
    fall back to the LLM listing pass on the raw content.

Expandable calendar endpoints expose empty ``start``/``end`` (or
``from``/``to``/``timeMin``/``timeMax``) params; :func:`fill_time_params` fills
them with ``now -> now+window`` so they return upcoming events.

All parsers return plain ``dict``s shaped like the extractor's event objects
(``title``, ``starts_at`` as a ``datetime``, ``ends_at``, ``venue_name``,
``description``, ``url``) so the caller can merge them with LLM-extracted events
and feed them to the same pydantic validation.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

# Feed types advertised via <link rel="alternate" type="...">.
_FEED_LINK_TYPES = {
    "text/calendar",
    "application/ics",
    "application/calendar+json",
    "application/rss+xml",
    "application/atom+xml",
    "application/json",
    "application/feed+json",
}

# Keywords that make a URL look like an event-data feed. Matched case-insensitively
# against the path/query of candidate URLs found in script text.
_FEED_KW = re.compile(
    r"(ical|\.ics|calendar|jcal|\.json|feed|rss|atom|wp-json|events?[/.])",
    re.IGNORECASE,
)

# Candidate URLs inside script text: quoted strings starting with http(s):// or
# a root-relative "/...". Capped to plausible lengths.
_URL_RE = re.compile(r"""['"`(=]\s*((?:https?://|/)[^'"`\s)<>]{3,200})""")


def discover_feed_urls(
    html: str,
    source_url: str,
    fetch_text: Callable[[str], tuple[str | None, str | None]],
    *,
    max_scripts: int = 3,
) -> list[str]:
    """Return ordered, de-duplicated candidate feed URLs for a page.

    Sources: ``<link rel=alternate>`` feed types, inline ``<script>`` text, and a
    bounded scan of externally-linked ``.js`` files. URLs are absolutized against
    ``source_url``. ``fetch_text(url) -> (text, content_type)`` (or ``(None,None)``
    on failure) is used only for the external JS scan.
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []

    # 1. <link rel="alternate" type="..."> feed hints (free — no fetch).
    for link in soup.find_all("link", attrs={"rel": True}):
        rel = link.get("rel")
        if not rel or "alternate" not in [r.lower() for r in (rel if isinstance(rel, list) else [rel])]:
            continue
        ltype = (link.get("type") or "").lower()
        href = link.get("href")
        if href and (ltype in _FEED_LINK_TYPES or "calendar" in ltype or "rss" in ltype or "atom" in ltype):
            candidates.append(urljoin(source_url, href.strip()))

    # 2. Inline <script> text (skip JSON-LD — that is data, not a feed reference,
    # and is already handed to the listing pass).
    for script in soup.find_all("script"):
        if (script.get("type") or "").lower() == "application/ld+json":
            continue
        if script.string:
            candidates.extend(_feed_urls_in_text(script.string, source_url))

    # 3. Externally-linked .js files (bounded fetch + scan).
    js_srcs: list[str] = []
    for script in soup.find_all("script", src=True):
        src = urljoin(source_url, script["src"].strip())
        if src.startswith(("http://", "https://")) and src not in js_srcs:
            js_srcs.append(src)
    for src in js_srcs[:max_scripts]:
        text, _ctype = fetch_text(src)
        if text:
            candidates.extend(_feed_urls_in_text(text, source_url))

    # De-dup preserving order; drop the source page itself.
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if not c or not c.startswith(("http://", "https://")):
            continue
        if c == source_url:
            continue
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def _feed_urls_in_text(text: str, source_url: str) -> list[str]:
    """Pull feed-looking URLs out of a chunk of script text."""
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        url = m.group(1).rstrip("',")
        if _FEED_KW.search(url):
            out.append(urljoin(source_url, url))
    return out


def fill_time_params(url: str, now: datetime, window: timedelta) -> str:
    """Fill empty/missing time-range params so expandable feeds return upcoming events.

    ``start``/``end``/``from``/``to`` -> Unix integer seconds; ``timeMin``/
    ``timeMax`` -> RFC3339. If ``start`` is present but ``end`` is absent, ``end``
    is added (some CalDAV servers require both for ``expand=true``).
    """
    parts = urlparse(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    has = {k for k, _ in query}

    start_ts = int(now.timestamp())
    end_ts = int((now + window).timestamp())
    end_iso = (now + window).isoformat()

    filled: list[tuple[str, str]] = []
    for k, v in query:
        if k.lower() in ("start", "from") and not v.strip():
            filled.append((k, str(start_ts)))
        elif k.lower() in ("end", "to") and not v.strip():
            filled.append((k, str(end_ts)))
        elif k.lower() == "timemin" and not v.strip():
            filled.append((k, now.isoformat()))
        elif k.lower() == "timemax" and not v.strip():
            filled.append((k, end_iso))
        else:
            filled.append((k, v))

    # CalDAV sabre/dav requires both start and end when expand=true.
    if "start" in has and "end" not in has and "end" not in {k for k, _ in filled}:
        filled.append(("end", str(end_ts)))

    return urlunparse(parts._replace(query=urlencode(filled)))


def parse_calendar(content: str, content_type: str) -> list[dict[str, Any]] | None:
    """Parse a calendar feed into event dicts, or return None if not a calendar.

    ``None`` signals the caller to fall back to the LLM listing pass on the raw
    content. Recognizes jcal (JSON Calendar) and iCal (text/calendar).
    """
    ctype = (content_type or "").lower()
    stripped = content.lstrip()[:64]
    if "calendar+json" in ctype or "json" in ctype or stripped.startswith('["vcalendar"'):
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            return None
        if isinstance(data, list) and data and data[0] == "vcalendar":
            return _parse_jcal(data)
        return None  # other JSON -> let the LLM handle it
    if "text/calendar" in ctype or "ics" in ctype or stripped.upper().startswith("BEGIN:VCALENDAR"):
        return _parse_ical(content)
    return None


# --- jcal (JSON Calendar, RFC 7265) ---


def _parse_jcal(data: list[Any]) -> list[dict[str, Any]]:
    """jcal: ["vcalendar", [calprops], [components]]; components include vevents."""
    events: list[dict[str, Any]] = []
    if not isinstance(data, list) or len(data) < 3:
        return events
    for comp in data[2]:
        if not isinstance(comp, list) or not comp or comp[0] != "vevent":
            continue
        props = _jcal_props(comp[1])
        events.extend(_vevent_to_dict(props))
    return events


def _jcal_props(prop_list: list[Any]) -> dict[str, Any]:
    """jcal props are [name, params, type, value]; keep the value keyed by name."""
    out: dict[str, Any] = {}
    for p in prop_list:
        if isinstance(p, list) and len(p) >= 4:
            out[p[0]] = p[3]
    return out


# --- iCal (text/calendar) ---


def _parse_ical(content: str) -> list[dict[str, Any]]:
    """A minimal iCal vevent parser: unfold continuation lines, split properties."""
    events: list[dict[str, Any]] = []
    # Unfold: a line beginning with space/tab continues the previous line.
    raw_lines = content.splitlines()
    lines: list[str] = []
    for line in raw_lines:
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)

    in_event = False
    props: dict[str, str] = {}
    for line in lines:
        if line == "BEGIN:VEVENT":
            in_event, props = True, {}
            continue
        if line == "END:VEVENT":
            if in_event:
                events.extend(_vevent_to_dict(props))
            in_event = False
            continue
        if not in_event:
            continue
        # NAME;PARAMS:VALUE
        if ":" not in line:
            continue
        name_part, value = line.split(":", 1)
        name = name_part.split(";")[0].lower()
        props[name] = value
    return events


def _ical_dt(value: str, params: dict[str, str]) -> datetime | None:
    """Parse an iCal DATE / DATE-TIME value into a datetime (naive or UTC)."""
    value = value.strip()
    # Pure date: YYYYMMDD (8 digits).
    if len(value) == 8 and value.isdigit():
        return datetime(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    # DATE-TIME: YYYYMMDDTHHMMSS[Z] — normalize to ISO and use fromisoformat.
    iso = value
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    # Translate the compact iCal form to ISO (20260903T160000 -> 2026-09-03T16:00:00).
    if "T" in iso and len(iso.split("T")[0]) == 8 and "-" not in iso:
        iso = f"{iso[0:4]}-{iso[4:6]}-{iso[6:8]}T{iso[9:11]}:{iso[11:13]}:{iso[13:15]}{iso[15:]}"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    # TZID present: we can't reliably resolve arbitrary zones without tzdata; keep
    # the local time as-is (naive) rather than guessing. UTC ("Z") stays tz-aware.
    return dt


# --- shared vevent -> event dict ---


def _vevent_to_dict(props: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a vevent's props (jcal dict or iCal dict) to an event dict list.

    Handles both jcal (dtstart is an ISO string) and iCal (dtstart is compact
    iCal, with params for DATE/DATE-TIME). Returns [] (not [bad]) when the event
    lacks a title or start — matches the LLM path's "omit if unsure" rule.
    """
    title = _scalar(props.get("summary") or props.get("name"))
    if not title:
        return []
    start_raw = props.get("dtstart")
    if not start_raw:
        return []
    # iCal may carry params inline as "DTSTART;VALUE=DATE:20260904" — already split
    # by _parse_ical into name "dtstart"; params are not preserved there, so detect
    # date-only by shape. jcal gives an ISO string directly.
    if isinstance(start_raw, str):
        if len(start_raw) == 8 and start_raw.isdigit():
            starts_at: datetime | None = datetime(
                int(start_raw[0:4]), int(start_raw[4:6]), int(start_raw[6:8])
            )
        else:
            starts_at = _ical_dt(start_raw, {})
    else:
        starts_at = None
    if starts_at is None:
        return []

    end_raw = props.get("dtend")
    ends_at: datetime | None = None
    if isinstance(end_raw, str) and end_raw:
        if len(end_raw) == 8 and end_raw.isdigit():
            ends_at = datetime(int(end_raw[0:4]), int(end_raw[4:6]), int(end_raw[6:8]))
        else:
            ends_at = _ical_dt(end_raw, {})

    return [
        {
            "title": title,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "venue_name": _scalar(props.get("location")) or None,
            "description": _scalar(props.get("description")) or None,
            "url": _scalar(props.get("url")) or None,
        }
    ]


def _scalar(v: Any) -> str | None:
    """Flatten a jcal/iCal prop value to a single string (jcal may use lists)."""
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, list):
        return " ".join(str(x) for x in v).strip() or None
    return str(v).strip() or None


def is_past(dt: datetime, now: datetime) -> bool:
    """Compare a possibly-naive datetime against a tz-aware ``now`` safely."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < now


def filter_upcoming(events: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Drop events whose ``starts_at`` is in the past (best-effort, keeps naive/None)."""
    out: list[dict[str, Any]] = []
    for e in events:
        s = e.get("starts_at")
        if isinstance(s, datetime) and is_past(s, now):
            continue
        out.append(e)
    return out