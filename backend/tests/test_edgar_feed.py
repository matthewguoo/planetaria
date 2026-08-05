"""EdgarFeed chaos coverage: normalization, dedupe/replay, malformed
payloads, exhibit-fetch failure, rate-limit cooldown, poll windows. Fixture
shapes replicate the REAL AMD 8-K structures captured 2026-08-04 (see
edgar.py module docstring)."""

from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.db.session import Database
from app.services.signals.edgar import (
    MAX_TEXT,
    EdgarFeed,
    RateLimited,
    in_poll_window,
    seconds_to_next_window,
    strip_html,
)
from app.services.signals.events import EventBus
from app.services.signals.store import SignalStore

ARCHIVE = "https://www.sec.gov/Archives/edgar/data/2488/000000248826000121"


def entry(form="8-K", company="Advanced Micro Devices Inc", cik="0000002488",
          acc="0000002488-26-000121", items=("2.02", "9.01"),
          updated="2026-08-04T16:16:24-04:00", broken=False) -> str:
    item_lines = "\n".join(
        f"&lt;br&gt;Item {i}: Something Described Here" for i in items)
    id_line = "" if broken else (
        f"<id>urn:tag:sec.gov,2008:accession-number={acc}</id>")
    return f"""<entry>
<title>{form} - {company} ({cik}) (Filer)</title>
<link rel="alternate" type="text/html" href="{ARCHIVE}/{acc}-index.htm"/>
<summary type="html">
 &lt;b&gt;Filed:&lt;/b&gt; 2026-08-04 &lt;b&gt;AccNo:&lt;/b&gt; {acc} &lt;b&gt;Size:&lt;/b&gt; 2 MB
{item_lines}
</summary>
<updated>{updated}</updated>
<category scheme="https://www.sec.gov/" label="form type" term="{form}"/>
{id_line}
</entry>"""


def atom(*entries: str) -> str:
    return ('<?xml version="1.0" encoding="ISO-8859-1" ?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            "<title>Latest Filings</title>" + "".join(entries) + "</feed>")


INDEX_HTML = """<html><body><table class="tableFile" summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>8-K</td><td><a href="/ix?doc=/Archives/edgar/data/2488/000000248826000121/amd-20260804.htm">amd-20260804.htm</a></td><td>8-K</td><td>30308</td></tr>
<tr><td>2</td><td>EX-99.1</td><td><a href="/Archives/edgar/data/2488/000000248826000121/q22026991.htm">q22026991.htm</a></td><td>EX-99.1</td><td>448891</td></tr>
<tr><td>3</td><td>EX-99.2</td><td><a href="/Archives/edgar/data/2488/000000248826000121/slides.htm">slides.htm</a></td><td>EX-99.2</td><td>35590</td></tr>
</table></body></html>"""

EXHIBIT_HTML = ("<html><head><style>p{color:red}</style>"
                "<script>alert('x')</script></head><body>"
                "<p>AMD&nbsp;reports <b>record</b> Q2 revenue of "
                "$7.7&#160;billion, EPS $1.66 vs $1.61 est.</p></body></html>")


def client(atom_text, index_text=INDEX_HTML, exhibit_text=EXHIBIT_HTML,
           statuses: dict | None = None) -> httpx.AsyncClient:
    statuses = statuses or {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "browse-edgar" in url:
            return httpx.Response(statuses.get("atom", 200), text=atom_text)
        if "company_tickers" in url:
            return httpx.Response(200, json={
                "0": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet"},
                "1": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet"},
            })
        if "-index.htm" in url:
            return httpx.Response(statuses.get("index", 200), text=index_text)
        return httpx.Response(statuses.get("exhibit", 200), text=exhibit_text)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database()
    await database.connect(f"sqlite+aiosqlite:///{tmp_path}/edgar.db")
    yield database
    await database.close()


def make_feed(db) -> tuple[EdgarFeed, "object"]:
    bus = EventBus()
    queue = bus.subscribe("news")
    feed = EdgarFeed(bus, SignalStore(db),
                     SimpleNamespace(edgar_user_agent="test (t@example.com)"))
    feed._tickers = {2488: "AMD", 1111111: "OTHR"}
    return feed, queue


@pytest.mark.asyncio
class TestPollNormalization:
    async def test_results_filing_journaled_and_published(self, db):
        feed, queue = make_feed(db)
        feed_page = atom(
            entry(),  # AMD, 2.02 -> journal
            entry(company="Other Co", cik="0001111111",
                  acc="0001111111-26-000001", items=("1.01",)),  # not results
            entry(company="Some Fund LP", cik="0009999999",
                  acc="0009999999-26-000002", items=("2.02",)),  # unmapped CIK
        )
        async with client(feed_page) as http:
            await feed._poll_once(http)

        assert feed.results_seen == 2 and feed.journaled == 1
        assert feed.skipped_unmapped == 1 and feed.errors == 0
        event = queue.get_nowait()
        assert event.symbols == ("AMD",)
        assert event.key == "0000002488-26-000121"
        assert event.source == "edgar-8k"
        assert event.signal_id is not None
        assert event.payload["item_codes"] == ["2.02", "9.01"]
        # Exhibit resolved via the Type column (not filename), HTML stripped,
        # entities decoded.
        assert event.payload["url"].endswith("q22026991.htm")
        assert event.payload["text"] == ("AMD reports record Q2 revenue of "
                                         "$7.7 billion, EPS $1.66 vs $1.61 est.")
        assert event.payload["latency_s"] >= 0
        assert event.ts == datetime.fromisoformat("2026-08-04T16:16:24-04:00")
        assert queue.qsize() == 0

    async def test_repoll_and_restart_are_idempotent(self, db):
        feed, queue = make_feed(db)
        async with client(atom(entry())) as http:
            await feed._poll_once(http)
            await feed._poll_once(http)  # same page again: seen-set short-circuits
        assert feed.journaled == 1 and queue.qsize() == 1

        # Fresh instance (restart): seen-set empty, journal dedupe holds.
        feed2, queue2 = make_feed(db)
        async with client(atom(entry())) as http:
            await feed2._poll_once(http)
        assert feed2.journaled == 0 and queue2.qsize() == 0

    async def test_amendment_not_traded(self, db):
        feed, queue = make_feed(db)
        async with client(atom(entry(form="8-K/A"))) as http:
            await feed._poll_once(http)
        assert feed.results_seen == 0 and queue.qsize() == 0

    async def test_malformed_entry_survives(self, db):
        feed, queue = make_feed(db)
        page = atom(entry(broken=True),
                    entry(acc="0000002488-26-000122",
                          updated="2026-08-04T16:20:00-04:00"))
        async with client(page) as http:
            await feed._poll_once(http)
        assert feed.errors == 1
        assert feed.journaled == 1 and queue.qsize() == 1

    async def test_unparseable_atom_counts_error_not_crash(self, db):
        feed, queue = make_feed(db)
        async with client("this is not xml") as http:
            await feed._poll_once(http)
        assert feed.errors == 1 and queue.qsize() == 0


@pytest.mark.asyncio
class TestExhibitFetch:
    async def test_index_failure_still_journals_headline(self, db):
        feed, queue = make_feed(db)
        async with client(atom(entry()), statuses={"index": 500}) as http:
            await feed._poll_once(http)
        event = queue.get_nowait()
        assert event.payload["text"] == ""
        assert "text_error" in event.payload
        assert event.payload["headline"].startswith("AMD 8-K results")

    async def test_no_exhibit_row_still_journals(self, db):
        feed, queue = make_feed(db)
        bare = "<html><body><table></table></body></html>"
        async with client(atom(entry()), index_text=bare) as http:
            await feed._poll_once(http)
        event = queue.get_nowait()
        assert event.payload["text_error"] == "no EX-99.* exhibit in index"

    async def test_text_capped(self, db):
        feed, queue = make_feed(db)
        huge = "<html><body>" + "word " * 10_000 + "</body></html>"
        async with client(atom(entry()), exhibit_text=huge) as http:
            await feed._poll_once(http)
        assert len(queue.get_nowait().payload["text"]) == MAX_TEXT


def test_exhibit_prefers_press_release_over_slides():
    assert EdgarFeed._find_exhibit(INDEX_HTML).endswith("q22026991.htm")
    slides_only = INDEX_HTML.replace("EX-99.1", "EX-99.7")
    assert EdgarFeed._find_exhibit(slides_only).endswith("q22026991.htm")


@pytest.mark.asyncio
class TestBackpressure:
    async def test_rate_limited_raises_for_cooldown(self, db):
        feed, _ = make_feed(db)
        async with client(atom(entry()), statuses={"atom": 429}) as http:
            with pytest.raises(RateLimited):
                await feed._poll_once(http)

    async def test_feed_down_propagates_to_supervisor(self, db):
        feed, _ = make_feed(db)
        async with client(atom(entry()), statuses={"atom": 500}) as http:
            with pytest.raises(httpx.HTTPStatusError):
                await feed._poll_once(http)

    async def test_ticker_map_first_occurrence_wins(self, db):
        feed, _ = make_feed(db)
        feed._tickers = {}
        async with client(atom()) as http:
            await feed._ensure_tickers(http)
        assert feed._tickers[1652044] == "GOOGL"


def test_cooldown_escalates_and_caps():
    seq, cd = [], 0.0
    for _ in range(7):
        cd = EdgarFeed._bump_cooldown(cd)
        seq.append(cd)
    assert seq == [10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0]


def test_poll_windows():
    def at(hour, minute=0, day="2026-08-04"):  # a Tuesday
        return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00-04:00")

    assert not in_poll_window(at(5, 59))
    assert in_poll_window(at(6, 0))
    assert in_poll_window(at(9, 29))
    assert not in_poll_window(at(9, 30))
    assert not in_poll_window(at(15, 59))
    assert in_poll_window(at(16, 0))
    assert in_poll_window(at(19, 59))
    assert not in_poll_window(at(20, 0))
    assert not in_poll_window(at(17, 0, day="2026-08-08"))  # Saturday

    assert seconds_to_next_window(at(16, 30)) == 0.0
    assert seconds_to_next_window(at(10, 0)) == 6 * 3600.0
    # Friday 20:30 -> Monday 06:00
    assert seconds_to_next_window(at(20, 30, day="2026-08-07")) == \
        (24 + 24 + 9.5) * 3600.0


def test_strip_html_keeps_numbers_drops_markup():
    text = strip_html(EXHIBIT_HTML)
    assert "$7.7 billion" in text and "alert" not in text and "<" not in text
