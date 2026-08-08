# Can it read the table? — qwen3

150 anonymised charts, identical to what the reading arms are shown. Every question has a verifiable answer stated in the table; none requires forecasting.

| question | correct | n |
|---|---|---|
| highest high | 80.7% | 150 |
| lowest low | 89.3% | 150 |
| close of bar minus 10 | 100.0% | 150 |
| up closes last 12 | 8.7% | 150 |
| final close vs vwap | 51.3% | 150 |

**Mean across the five: 66.0%.**

A model that scores well here reads the artefact correctly, and any null in the reading arms is a fact about the market rather than about the instrument. A model that scores badly makes every other number in this study a capability floor.
