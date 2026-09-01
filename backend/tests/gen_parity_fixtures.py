"""Generate parity fixtures consumed by the frontend vitest suite.

Run:  python -m tests.gen_parity_fixtures
Writes frontend/src/lib/__fixtures__/parity.json (committed to the repo).
"""

import json
import pathlib

from app.services.options_math import (
    Leg,
    bs_price,
    norm_cdf,
    payoff_at_expiry,
    position_pl,
    prob_itm,
    prob_touch,
    terminal_ev,
)

OUT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "frontend" / "src" / "lib" / "__fixtures__" / "parity.json"
)


def main() -> None:
    cases: dict = {"norm_cdf": [], "bs": [], "prob_itm": [], "prob_touch": [], "position": []}

    for x in [-8.5, -6, -3.1, -1, -0.2, 0.0, 0.37, 1.0, 2.5, 5.0, 7.9, 9.0]:
        cases["norm_cdf"].append({"x": x, "out": norm_cdf(x)})

    grid_S = [10.0, 100.0, 452.37, 737.44]
    grid_K_rel = [0.9, 0.98, 1.0, 1.02, 1.15]
    grid_tau = [0.0, 0.25 / 252, 2.0 / 252, 15.0 / 252]
    grid_sigma = [0.0, 0.08, 0.21, 0.85]
    for S in grid_S:
        for krel in grid_K_rel:
            K = round(S * krel, 2)
            for tau in grid_tau:
                for sigma in grid_sigma:
                    for right in ("C", "P"):
                        cases["bs"].append(
                            {"S": S, "K": K, "tau": tau, "sigma": sigma, "right": right,
                             "out": bs_price(S, K, tau, sigma, right)}
                        )
                    cases["prob_itm"].append(
                        {"S": S, "K": K, "tau": tau, "sigma": sigma, "right": "C",
                         "out": prob_itm(S, K, tau, sigma, "C")}
                    )
                    if sigma > 0 and tau > 0:
                        cases["prob_touch"].append(
                            {"S": S, "barrier": K, "tau": tau, "sigma": sigma,
                             "out": prob_touch(S, K, tau, sigma)}
                        )

    spread = [
        {"right": "C", "strike": 450.0, "qty": 1, "side": 1, "entry": 4.0, "iv": 0.2},
        {"right": "C", "strike": 455.0, "qty": 1, "side": -1, "entry": 1.8, "iv": 0.19},
    ]
    legs = [Leg.from_dict(d) for d in spread]
    for S in [430.0, 448.0, 452.2, 456.0, 470.0]:
        for tau in [0.0, 1.0 / 252, 6.0 / 252]:
            cases["position"].append(
                {"legs": spread, "S": S, "tau": tau,
                 "pl": position_pl(legs, S, tau),
                 "payoff": payoff_at_expiry(legs, S),
                 "ev": terminal_ev(legs, S, tau, 0.2, 4.5, 1.0)}
            )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cases))
    total = sum(len(v) for v in cases.values())
    print(f"wrote {total} fixture cases -> {OUT}")


if __name__ == "__main__":
    main()
