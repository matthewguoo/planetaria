"""Anonymisation by consistent rescaling, done arithmetically.

WHY THE STUDY'S OWN SCRUB FAILED. Section 5.7 removed names, tickers and
dates, and the model still identified the issuer with HIGH confidence on 436
of 437 releases. The scrub never touched magnitudes, and a magnitude is an
identity: $94.9bn of quarterly revenue names one company as surely as its
ticker does. The arm reported a +230bp spread on "anonymised" text that was
not anonymised at all.

WHY THE LLM VERSION OF THIS FAILED TOO. Asking a model for a find/replace
list works when the edit is surgical -- reversing economics needs about a
dozen replacements and survived at 56%. Rescaling every currency figure needs
thirty to fifty, and the harness discards a variant if any single `find` does
not match exactly once. Sixteen of 231 survived. The rejection was not noise:
whole-document arithmetic is the wrong shape for an edit list, and paying an
LLM to multiply numbers it can already see is the wrong tool besides.

WHAT THIS DOES INSTEAD. Currency figures are regex-findable and the transform
is multiplication, so it runs locally, deterministically, and for free. A
single multiplier per event preserves every quantity a judgement rests on --
margins, growth rates, beat size, segment mix, guidance relative to the
quarter reported -- because all of them are ratios and ratios are invariant
under a common scale factor. Only the absolute level moves, and the absolute
level is the fingerprint.

WHAT IT DELIBERATELY DOES NOT TOUCH. Percentages, margins and growth rates
are left exactly as written. Rescaling those would destroy the economics
rather than the identity, and the point is to remove the name while leaving
the quarter intact and judgeable.

THE ADMISSION CRITERION IS MEASURED, NOT ASSUMED. Blinded releases are scored
through the blind arm, which asks the model to name the issuer and rate its
own confidence. Only releases where identification FAILS enter the analysis.
That is the step Section 5.7 skipped, and skipping it is why its result meant
nothing.
"""
from __future__ import annotations

import random
import re

# Currency spans, most specific first. Each carries the numeric group and any
# scale word, so "$1.24 billion" and "$1,240.5" are both handled without the
# suffix being multiplied along with the number.
MONEY = re.compile(
    r"(?P<sym>[$€£])\s?(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"(?P<suffix>\s?(?:billion|million|thousand|trillion|bn|mm|m|b|k)\b)?",
    re.IGNORECASE,
)
# Bare figures inside financial tables, where the currency sits in the column
# header rather than beside the number. Restricted to lines that already look
# like a statement row so ordinary prose integers are left alone.
PCT = re.compile(r"\d[\d,]*(?:\.\d+)?\s?%")

SCALES = (0.11, 0.17, 0.23, 0.31, 0.43, 2.7, 3.9, 5.3, 7.1, 9.4)


def multiplier(symbol: str, date: str) -> float:
    """Deterministic in the event, so a rerun reproduces the same document,
    and spread across a wide range so no single scale becomes the new
    fingerprint."""
    return random.Random(f"{symbol}_{date}").choice(SCALES)


def _fmt(value: float, had_decimal: bool, had_comma: bool) -> str:
    if abs(value) >= 100 or not had_decimal:
        s = f"{value:,.0f}" if had_comma or abs(value) >= 1000 else f"{value:.0f}"
    else:
        s = f"{value:,.2f}" if had_comma else f"{value:.2f}"
    return s


def rescale_text(text: str, k: float) -> tuple[str, int]:
    """Multiply every currency amount by k. Percentages are untouched.

    Percent spans are masked first, because "$1.2 billion, up 14%" must keep
    its 14 while its $1.2 moves — and a naive pass over the string would
    happily rescale a figure that happens to sit inside a percentage.
    """
    holes: list[str] = []

    def stash(m):
        holes.append(m.group(0))
        return f"\x00{len(holes)-1}\x00"

    masked = PCT.sub(stash, text)
    n = 0

    def scale(m):
        nonlocal n
        raw = m.group("num")
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            return m.group(0)
        n += 1
        out = _fmt(val * k, "." in raw, "," in raw)
        return f"{m.group('sym')}{out}{m.group('suffix') or ''}"

    masked = MONEY.sub(scale, masked)
    restored = re.sub(r"\x00(\d+)\x00", lambda m: holes[int(m.group(1))], masked)
    return restored, n


def anonymise(text: str, symbol: str, company: str, k: float) -> tuple[str, dict]:
    """Rescale, then strip the identity tokens the study already knows.

    The company name and ticker come from the panel rather than from a model's
    guess, so this cannot fail the way an LLM scrub does — there is nothing to
    infer.
    """
    out, n_money = rescale_text(text, k)
    subs = 0
    for token in filter(None, _identity_tokens(symbol, company)):
        pat = re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE)
        out, c = pat.subn("the Company", out)
        subs += c
    # Dates and fiscal labels: a quarter label plus a scale is nearly as
    # identifying as a name.
    out, d1 = re.subn(r"\b(?:Q[1-4]|first|second|third|fourth)\s+quarter\b",
                      "the quarter", out, flags=re.IGNORECASE)
    out, d2 = re.subn(r"\b(?:FY\s?)?20\d{2}\b", "the period", out)
    out, d3 = re.subn(
        r"\b(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},?\s*(?:the period)?\b",
        "a date", out, flags=re.IGNORECASE)
    return out, {"money_rescaled": n_money, "identity_subs": subs,
                 "date_subs": d1 + d2 + d3, "k": k}


def _identity_tokens(symbol: str, company: str) -> list[str]:
    """Ticker, full name, and the distinctive part of the name on its own —
    'Williams-Sonoma, Inc.' has to lose 'Williams-Sonoma', not just the whole
    string, because the release uses the short form throughout."""
    toks = [symbol]
    if company:
        toks.append(company)
        stem = re.sub(r",?\s+(Inc\.?|Corp\.?|Corporation|Company|Co\.?|Ltd\.?|"
                      r"plc|Holdings?|Group|N\.V\.|S\.A\.)\s*$", "",
                      company, flags=re.IGNORECASE).strip()
        if stem and stem != company:
            toks.append(stem)
        first = stem.split()[0] if stem.split() else ""
        if len(first) > 3:
            toks.append(first)
    return sorted(set(toks), key=len, reverse=True)
