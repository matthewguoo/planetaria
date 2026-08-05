"""EDGAR 8-K feed: free, authoritative earnings full text.

Polls the `getcurrent` Atom feed for 8-K filings during the release windows
(premarket 06:00-09:30 ET, after-close 16:00-20:00 ET), and for filings that
carry Item 2.02 (Results of Operations) fetches the press-release exhibit
text and journals it as a `news` event keyed by accession number — the
journal's (source, key) dedupe makes re-polls and restarts idempotent.

Empirical shapes (verified 2026-08-04, AMD earnings night — do not re-derive
from docs):
- getcurrent Atom entries carry the Item codes in <summary> ("Item 2.02:
  Results of Operations..."), acceptance ts in <updated> WITH a proper ET
  offset, accession in <id>, and the -index.htm URL in <link>. Item
  filtering therefore needs no per-filing fetch.
- Filename heuristics for the press release DO NOT work: AMD's EX-99.1 is
  "q22026991.htm". The -index.htm table's Type column is authoritative
  (EX-99.1 -> filename); iXBRL rows wrap the href in "/ix?doc=".
- data.sec.gov submissions JSON acceptanceDateTime says "Z" but the value
  is EASTERN (16:16:24.000Z == the index page's "Accepted: 2026-08-04
  16:16:24" ET) — anything consuming it must re-localize. The Atom
  <updated> field is not affected (real offset).

Politeness (SEC fair-access rules): real User-Agent with a contact address,
<=10 req/s hard ceiling — we poll once per ~2.5s in-window plus at most two
archive fetches per fresh Item-2.02 filing with a gap between them, and back
off on 429/503 with escalating cooldowns.

What a backend outage costs: getcurrent only shows the current stream, so a
2.02 filed while the engine was down is missed (never journaled). That is
acceptable — the reaction strategy trades the same night only.
"""

import asyncio
import html as html_lib
import logging
import re
import time
import xml.etree.ElementTree as ETree
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .events import Event, EventBus
from .store import SignalStore

log = logging.getLogger("app.feeds.edgar")

ET_TZ = ZoneInfo("America/New_York")
ATOM_NS = "{http://www.w3.org/2005/Atom}"

GETCURRENT_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_ROOT = "https://www.sec.gov"

MAX_TEXT = 20_000          # chars of stripped press-release text per event
SEEN_LRU = 5_000           # accession numbers remembered between polls
ARCHIVE_GAP_S = 0.2        # pause between per-filing archive fetches
COOLDOWN_START_S = 10.0    # first 429/503 cooldown; doubles to a cap
COOLDOWN_MAX_S = 300.0


class RateLimited(RuntimeError):
    """SEC asked us to slow down (429/503 on the poll endpoint)."""


@dataclass(slots=True)
class Filing:
    accession: str
    form: str
    company: str
    cik: int
    items: list[str]
    accepted_utc: datetime
    index_url: str


def in_poll_window(now: datetime | None = None) -> bool:
    """Weekday release windows only: 06:00-09:30 and 16:00-20:00 ET.
    Between them EDGAR still accepts filings, but earnings 8-Ks land in
    these windows and idle polling is impolite for no signal."""
    now = (now or datetime.now(ET_TZ)).astimezone(ET_TZ)
    if now.weekday() >= 5:
        return False
    hhmm = now.hour * 60 + now.minute
    return (6 * 60 <= hhmm < 9 * 60 + 30) or (16 * 60 <= hhmm < 20 * 60)


def seconds_to_next_window(now: datetime | None = None) -> float:
    """How long run() may sleep before the next poll could matter. Capped by
    the caller so cancellation and status stay responsive."""
    now = (now or datetime.now(ET_TZ)).astimezone(ET_TZ)
    if in_poll_window(now):
        return 0.0
    for days_ahead in range(0, 4):  # Friday 20:00 -> Monday 06:00 spans 3 days
        day = now + timedelta(days=days_ahead)
        if day.weekday() >= 5:
            continue
        for hour, minute in ((6, 0), (16, 0)):
            edge = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if edge > now:
                return (edge - now).total_seconds()
    return 3600.0  # unreachable; defensive


def strip_html(raw: str) -> str:
    """Press-release HTML -> plain text. Regex-grade on purpose: exhibits are
    static generated HTML; tables collapse to word runs but every number
    survives, which is all the analyst consumer needs."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class EdgarFeed:
    bus: EventBus
    store: SignalStore
    settings: object
    poll_s: float = 2.5

    name: str = field(default="edgar-8k", init=False)
    polls: int = field(default=0, init=False)
    entries_seen: int = field(default=0, init=False)
    results_seen: int = field(default=0, init=False)   # Item 2.02 filings
    journaled: int = field(default=0, init=False)
    errors: int = field(default=0, init=False)
    skipped_unmapped: int = field(default=0, init=False)
    last_poll_mono: float | None = field(default=None, init=False)
    last_latency_s: float | None = field(default=None, init=False)
    cooldown_s: float = field(default=0.0, init=False)

    def __post_init__(self):
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._tickers: dict[int, str] = {}
        self._tickers_day: str | None = None

    # -------------------------------------------------------------- lifecycle

    def _client(self) -> httpx.AsyncClient:
        from app.services.call_log import http_hooks

        return httpx.AsyncClient(
            headers={"User-Agent": getattr(self.settings, "edgar_user_agent", "")
                     or "planetaria/0.1"},
            timeout=15.0,
            follow_redirects=True,
            event_hooks=http_hooks("data"),
        )

    async def run(self) -> None:  # long-lived; raising -> supervise() backoff
        async with self._client() as http:
            while True:
                wait = seconds_to_next_window()
                if wait > 0:
                    await asyncio.sleep(min(wait, 60.0))
                    continue
                await self._ensure_tickers(http)
                try:
                    await self._poll_once(http)
                except RateLimited:
                    self.cooldown_s = self._bump_cooldown(self.cooldown_s)
                    log.warning("edgar rate-limited; cooling down %.0fs",
                                self.cooldown_s)
                    await asyncio.sleep(self.cooldown_s)
                    continue
                self.cooldown_s = 0.0
                await asyncio.sleep(self.poll_s)

    @staticmethod
    def _bump_cooldown(current: float) -> float:
        return min(COOLDOWN_MAX_S, current * 2 if current else COOLDOWN_START_S)

    # ------------------------------------------------------------------ tickers

    async def _ensure_tickers(self, http: httpx.AsyncClient) -> None:
        """CIK -> ticker map, refreshed daily. First occurrence wins: the SEC
        file lists primary listings before secondary share classes (GOOGL
        before GOOG for CIK 1652044)."""
        today = datetime.now(ET_TZ).date().isoformat()
        if self._tickers and self._tickers_day == today:
            return
        try:
            resp = await http.get(TICKER_MAP_URL)
            resp.raise_for_status()
            fresh: dict[int, str] = {}
            for row in resp.json().values():
                fresh.setdefault(int(row["cik_str"]), str(row["ticker"]).upper())
            if not fresh:
                raise ValueError("empty ticker map")
            self._tickers = fresh
            self._tickers_day = today
            log.info("edgar ticker map loaded: %d CIKs", len(fresh))
        except Exception:
            if not self._tickers:
                raise  # nothing to map with -> let supervise() retry the feed
            log.exception("ticker map refresh failed - keeping yesterday's")

    # -------------------------------------------------------------------- poll

    async def _poll_once(self, http: httpx.AsyncClient) -> None:
        self.polls += 1
        self.last_poll_mono = time.monotonic()
        resp = await http.get(GETCURRENT_URL, params={
            "action": "getcurrent", "type": "8-K", "company": "", "dateb": "",
            "owner": "include", "count": "100", "output": "atom",
        })
        if resp.status_code in (429, 503):
            raise RateLimited(f"getcurrent returned {resp.status_code}")
        resp.raise_for_status()
        filings = self._parse_atom(resp.text)
        # Oldest first so journal ids follow acceptance order within a poll.
        for filing in reversed(filings):
            if filing.accession in self._seen:
                continue
            self._mark_seen(filing.accession)
            self.entries_seen += 1
            if filing.form != "8-K":
                continue  # 8-K/A amendments re-announce stale info; skip
            if "2.02" not in filing.items:
                continue
            self.results_seen += 1
            try:
                await self._handle_results_filing(http, filing)
            except Exception:
                self.errors += 1
                log.exception("edgar 2.02 handling failed (%s)", filing.accession)

    def _parse_atom(self, xml_text: str) -> list[Filing]:
        """One malformed entry must not kill the poll: parse each entry inside
        a guard; failures count and log."""
        try:
            root = ETree.fromstring(xml_text)
        except ETree.ParseError:
            self.errors += 1
            log.exception("edgar atom feed unparseable (%d chars)", len(xml_text))
            return []
        out: list[Filing] = []
        for entry in root.iter(f"{ATOM_NS}entry"):
            try:
                out.append(self._parse_entry(entry))
            except Exception:
                self.errors += 1
                log.exception("edgar atom entry unparseable")
        return out

    @staticmethod
    def _parse_entry(entry) -> Filing:
        title = entry.findtext(f"{ATOM_NS}title") or ""
        # "8-K - Advanced Micro Devices Inc (0000002488) (Filer)"
        form, _, rest = title.partition(" - ")
        cik_match = re.search(r"\((\d{10})\)", rest)
        company = rest[: cik_match.start()].strip() if cik_match else rest.strip()
        entry_id = entry.findtext(f"{ATOM_NS}id") or ""
        accession = entry_id.rpartition("=")[2]
        if not accession or cik_match is None:
            raise ValueError(f"entry missing accession/CIK: {title!r}")
        updated = entry.findtext(f"{ATOM_NS}updated") or ""
        accepted = datetime.fromisoformat(updated).astimezone(timezone.utc)
        summary = entry.findtext(f"{ATOM_NS}summary") or ""
        items = re.findall(r"Item\s+(\d+\.\d+)", summary)
        link = entry.find(f"{ATOM_NS}link")
        index_url = link.get("href") if link is not None else ""
        return Filing(
            accession=accession, form=form.strip(), company=company,
            cik=int(cik_match.group(1)), items=items, accepted_utc=accepted,
            index_url=index_url,
        )

    def _mark_seen(self, accession: str) -> None:
        self._seen[accession] = None
        while len(self._seen) > SEEN_LRU:
            self._seen.popitem(last=False)

    # ------------------------------------------------------------ 2.02 filings

    async def _handle_results_filing(self, http: httpx.AsyncClient,
                                     filing: Filing) -> None:
        ticker = self._tickers.get(filing.cik)
        if ticker is None:
            # Untickered filers (funds, private issuers) are untradeable.
            self.skipped_unmapped += 1
            log.info("8-K 2.02 from unmapped CIK %d (%s) - skipped",
                     filing.cik, filing.company)
            return
        text, exhibit_url, text_error = await self._fetch_release_text(http, filing)
        now = datetime.now(timezone.utc)
        latency_s = max(0.0, (now - filing.accepted_utc).total_seconds())
        payload = {
            "headline": f"{ticker} 8-K results: {filing.company}",
            "item_codes": filing.items,
            "text": text,
            "url": exhibit_url or filing.index_url,
            "accepted_at": filing.accepted_utc.isoformat(),
            "latency_s": round(latency_s, 1),
        }
        if text_error:
            payload["text_error"] = text_error
        event = Event(
            type="news", ts=filing.accepted_utc, source=self.name,
            key=filing.accession, symbols=(ticker,), payload=payload,
        )
        event, fresh = await self.store.record(event)
        if fresh:
            self.journaled += 1
            self.last_latency_s = round(latency_s, 1)
            log.info("edgar 8-K %s (%s): acceptance->journal %.1fs, %d chars",
                     ticker, filing.accession, latency_s, len(text))
            self.bus.publish(event)

    async def _fetch_release_text(
        self, http: httpx.AsyncClient, filing: Filing
    ) -> tuple[str, str | None, str | None]:
        """(text, exhibit_url, error). A failed text fetch still journals the
        acceptance itself — headline + item codes are tradeable information;
        the error travels in the payload instead of hiding the filing."""
        try:
            resp = await http.get(filing.index_url)
            resp.raise_for_status()
            exhibit_path = self._find_exhibit(resp.text)
            if exhibit_path is None:
                return "", None, "no EX-99.* exhibit in index"
            exhibit_url = (exhibit_path if exhibit_path.startswith("http")
                           else f"{SEC_ROOT}{exhibit_path}")
            await asyncio.sleep(ARCHIVE_GAP_S)  # politeness gap
            resp = await http.get(exhibit_url)
            resp.raise_for_status()
            return strip_html(resp.text)[:MAX_TEXT], exhibit_url, None
        except Exception as exc:
            return "", None, repr(exc)

    @staticmethod
    def _find_exhibit(index_html: str) -> str | None:
        """The -index.htm Type column is the only reliable exhibit locator
        (see module docstring). Prefer EX-99.1 (the press release) over
        EX-99.2+ (slides); iXBRL hrefs are wrapped in /ix?doc=."""
        rows = re.findall(
            r'<a href="(?:/ix\?doc=)?(/Archives/[^"]+\.html?)"[^>]*>[^<]*</a>'
            r"\s*</td>\s*<td[^>]*>\s*(EX-99[^<\s]*)",
            index_html, flags=re.I,
        )
        if not rows:
            return None
        for href, ex_type in rows:
            if ex_type.upper().rstrip(".") in ("EX-99.1", "EX-99"):
                return href
        return rows[0][0]

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        return {
            "in_window": in_poll_window(),
            "polls": self.polls,
            "entries_seen": self.entries_seen,
            "results_seen": self.results_seen,
            "journaled": self.journaled,
            "errors": self.errors,
            "skipped_unmapped": self.skipped_unmapped,
            "tickers": len(self._tickers),
            "cooldown_s": self.cooldown_s,
            "last_poll_age_s": (
                round(time.monotonic() - self.last_poll_mono, 1)
                if self.last_poll_mono else None
            ),
            "last_latency_s": self.last_latency_s,
        }
