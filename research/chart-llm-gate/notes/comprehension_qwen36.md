# Can it read the table? — qwen36

0 anonymised charts, identical to what the reading arms are shown. Every question has a verifiable answer stated in the table; none requires forecasting.

| question | correct | n |
|---|---|---|
| highest high | nan% | 0 |
| lowest low | nan% | 0 |
| close of bar minus 10 | nan% | 0 |
| up closes last 12 | nan% | 0 |
| final close vs vwap | nan% | 0 |

**Mean nan%, worst dimension nan% (highest high).**

Pre-registered bar: mean >= 85% and every dimension >= 60%. **FAIL** — this model must NOT be used for the reading arms.

A model that scores well here reads the artefact correctly, and any null in the reading arms is a fact about the market rather than about the instrument. A model that scores badly makes every other number in this study a capability floor.
