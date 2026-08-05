"""Reference LLM strategy: proves the analyze() environment, trades nothing.

A strategy instance is an environment: params carry a SYSTEM PROMPT, a task,
and a JSON schema; every matching event (news, or a manual trigger with text)
is run through ctx.analyze() and the structured verdict is journaled. That is
the full tier-1 loop — untrusted text in, schema-validated judgment out, with
provenance — minus the order, which a real strategy would place by threading
analysis.signal_id into a TradeIntent.

Runbook (paper, needs ANTHROPIC_API_KEY in .env):
  1. POST /api/strategies  {"kind": "llm_probe", "name": "probe-news"}
  2. POST /api/strategies/{id}/enable
  3. POST /api/strategies/{id}/trigger
       {"payload": {"text": "ACME beats on EPS, guides FY revenue down 20%"}}
  4. GET /api/strategies/{id}/decisions  -> note with the parsed verdict
     GET /api/signals?type=analysis      -> the journaled LLM call itself
Enabled against the news feed it scores every headline for its symbols.
"""

from app.services.llm import AnalysisError
from app.services.signals.events import Event
from app.strategies import register
from app.strategies.base import Strategy, StrategyContext

# Verdicts are deliberately coarse: direction + confidence + one-line reason.
# Downstream code branches on enums, never on prose.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary": {"type": "string"},
    },
    "required": ["direction", "confidence", "summary"],
    "additionalProperties": False,
}


@register
class LLMProbe(Strategy):
    """Scores event text with ctx.analyze() and journals the verdict (LLM environment proof)."""

    kind = "llm_probe"
    subscriptions = ("manual", "news")
    # Analysis calls run seconds-to-tens-of-seconds; give each event room for
    # one call plus journaling without racing the default 30s budget.
    event_timeout_s = 120.0
    default_params = {
        "system": "You are a cautious markets analyst scoring one headline at a time.",
        "task": "Classify the likely near-term price impact of this text on the "
                "companies it concerns.",
        "symbols": None,   # optional list filter for news events
        "effort": None,    # None -> engine default (settings.llm_effort)
    }

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        clean = super().validate_params(params)
        for key in ("system", "task"):
            if not str(clean[key]).strip() or len(str(clean[key])) > 2000:
                raise ValueError(f"{key} must be a non-empty string under 2000 chars")
        if clean["symbols"] is not None and not isinstance(clean["symbols"], list):
            raise ValueError("symbols must be null or a list of tickers")
        return clean

    async def on_event(self, event: Event, ctx: StrategyContext) -> None:
        text = self._text_of(event)
        if not text:
            await ctx.note({"skip": f"no text in {event.type} event"},
                           signal_ids=_ids(event))
            return
        wanted = ctx.params["symbols"]
        if event.type == "news" and wanted and not set(event.symbols) & set(wanted):
            return  # not our tickers; silent skip keeps the journal readable

        try:
            analysis = await ctx.analyze(
                task=str(ctx.params["task"]),
                data=text,
                schema=VERDICT_SCHEMA,
                system=str(ctx.params["system"]),
                symbols=event.symbols,
                signal_ids=_ids(event),
                effort=ctx.params["effort"],
            )
        except AnalysisError as exc:
            # Already journaled as an analysis signal by the gateway; the
            # decision journal records what this INSTANCE did about it.
            await ctx.note({"skip": f"analysis failed: {exc}"}, signal_ids=_ids(event))
            return

        await ctx.note(
            {"verdict": analysis.result, "model": analysis.model,
             "latency_ms": analysis.latency_ms},
            signal_ids=tuple(i for i in (*_ids(event), analysis.signal_id)
                             if i is not None),
        )

    @staticmethod
    def _text_of(event: Event) -> str:
        p = event.payload or {}
        if event.type == "manual":
            return str(p.get("text") or "").strip()
        headline = str(p.get("headline") or "").strip()
        summary = str(p.get("summary") or "").strip()
        return f"{headline}\n{summary}".strip()


def _ids(event: Event) -> tuple[int, ...]:
    return (event.signal_id,) if event.signal_id is not None else ()
