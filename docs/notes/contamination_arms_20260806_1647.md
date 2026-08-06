# Contamination arms — claude-opus-5, effort medium, panel v2

arms present: blind n=437, named n=1802, notext n=440
paired subset scored below: 436 events covered by every arm

| arm | n | ungated | gated | vetoed | spread | keeps |
|---|---|---|---|---|---|---|
| named | 431 | +49.2 | +153.8 | -144.8 | +298.6 | 65% |
| blind | 431 | +49.2 | +130.4 | -99.9 | +230.3 | 65% |
| notext | 431 | +49.2 | +238.2 | -60.2 | +298.4 | 37% |

## Identity ablation — how leaky is the scrub?

named the right issuer on 97.5% of 436 scrubbed releases.

| self-reported identification confidence | n | actually right |
|---|---|---|
| low | 1 | 100.0% |
| high | 435 | 97.5% |

| blind subset | n | gated | vetoed | spread |
|---|---|---|---|---|
| could NOT identify the issuer | 10 | +258.4 | -718.6 | +977.0 |
| identified the issuer | 421 | +127.1 | -87.4 | +214.6 |

named and blind agree on direction for 84.9% of 436 paired events.

## Memory probe — the upper bound on what recall can be worth

claimed to recall the report on 180/436 events (41.3%); volunteered a next-session direction on 175.
outcome-recall accuracy 66.3% on 175 volunteered directions, against 50% chance (binomial SE 3.8pp, z = +4.31).
gate on memory alone: n=158 +238.2bp vs vetoed -60.2bp -> spread +298.4bp.
There is no information in this prompt. Whatever this spread is, it is what memorisation alone buys.

measured API spend for the whole study: $105.97 ($105.97 including batches submitted but not yet collected)
