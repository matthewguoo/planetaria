"""Curated liquid-underlying universe for the symbol autocomplete.

Static by design: this app trades 1-3 DTE options, which only makes sense on
deeply liquid names. When Alpaca keys are configured this list could be
augmented from get_all_assets(), but the curated set stays the ranking core.
"""

SYMBOLS: list[tuple[str, str]] = [
    # Index / sector ETFs (the 0-2 DTE workhorses first)
    ("SPY", "SPDR S&P 500 ETF"),
    ("QQQ", "Invesco Nasdaq-100 ETF"),
    ("IWM", "iShares Russell 2000 ETF"),
    ("DIA", "SPDR Dow Jones ETF"),
    ("SMH", "VanEck Semiconductor ETF"),
    ("XLF", "Financial Select SPDR"),
    ("XLE", "Energy Select SPDR"),
    ("XLK", "Technology Select SPDR"),
    ("TLT", "iShares 20+ Year Treasury ETF"),
    ("GLD", "SPDR Gold Shares"),
    ("SLV", "iShares Silver Trust"),
    ("USO", "United States Oil Fund"),
    ("UVXY", "ProShares Ultra VIX Futures"),
    ("VXX", "iPath VIX Short-Term Futures"),
    # Megacaps
    ("AAPL", "Apple Inc"),
    ("MSFT", "Microsoft Corp"),
    ("NVDA", "NVIDIA Corp"),
    ("GOOGL", "Alphabet Inc Class A"),
    ("GOOG", "Alphabet Inc Class C"),
    ("AMZN", "Amazon.com Inc"),
    ("META", "Meta Platforms Inc"),
    ("TSLA", "Tesla Inc"),
    ("BRK.B", "Berkshire Hathaway Class B"),
    ("AVGO", "Broadcom Inc"),
    ("LLY", "Eli Lilly & Co"),
    ("JPM", "JPMorgan Chase & Co"),
    ("V", "Visa Inc"),
    ("MA", "Mastercard Inc"),
    ("UNH", "UnitedHealth Group"),
    ("XOM", "Exxon Mobil Corp"),
    ("JNJ", "Johnson & Johnson"),
    ("WMT", "Walmart Inc"),
    ("PG", "Procter & Gamble"),
    ("HD", "Home Depot Inc"),
    ("COST", "Costco Wholesale"),
    ("ORCL", "Oracle Corp"),
    ("CVX", "Chevron Corp"),
    ("ABBV", "AbbVie Inc"),
    ("KO", "Coca-Cola Co"),
    ("PEP", "PepsiCo Inc"),
    ("MRK", "Merck & Co"),
    ("BAC", "Bank of America"),
    ("ADBE", "Adobe Inc"),
    ("CRM", "Salesforce Inc"),
    ("NFLX", "Netflix Inc"),
    ("AMD", "Advanced Micro Devices"),
    ("INTC", "Intel Corp"),
    ("QCOM", "Qualcomm Inc"),
    ("TXN", "Texas Instruments"),
    ("CSCO", "Cisco Systems"),
    ("TMO", "Thermo Fisher Scientific"),
    ("MCD", "McDonald's Corp"),
    ("NKE", "Nike Inc"),
    ("DIS", "Walt Disney Co"),
    ("WFC", "Wells Fargo & Co"),
    ("GS", "Goldman Sachs Group"),
    ("MS", "Morgan Stanley"),
    ("CAT", "Caterpillar Inc"),
    ("BA", "Boeing Co"),
    ("GE", "GE Aerospace"),
    ("HON", "Honeywell International"),
    ("UPS", "United Parcel Service"),
    ("PFE", "Pfizer Inc"),
    ("T", "AT&T Inc"),
    ("VZ", "Verizon Communications"),
    ("CMCSA", "Comcast Corp"),
    ("MU", "Micron Technology"),
    ("PLTR", "Palantir Technologies"),
    ("COIN", "Coinbase Global"),
    ("MSTR", "MicroStrategy Inc"),
    ("SQ", "Block Inc"),
    ("PYPL", "PayPal Holdings"),
    ("SHOP", "Shopify Inc"),
    ("UBER", "Uber Technologies"),
    ("ABNB", "Airbnb Inc"),
    ("SNOW", "Snowflake Inc"),
    ("PANW", "Palo Alto Networks"),
    ("CRWD", "CrowdStrike Holdings"),
    ("MRVL", "Marvell Technology"),
    ("SMCI", "Super Micro Computer"),
    ("ARM", "Arm Holdings"),
    ("TSM", "Taiwan Semiconductor ADR"),
    ("BABA", "Alibaba Group ADR"),
    ("NIO", "NIO Inc ADR"),
    ("F", "Ford Motor Co"),
    ("GM", "General Motors"),
    ("DAL", "Delta Air Lines"),
    ("AAL", "American Airlines"),
    ("CCL", "Carnival Corp"),
    ("SOFI", "SoFi Technologies"),
    ("HOOD", "Robinhood Markets"),
    ("DKNG", "DraftKings Inc"),
    ("RIVN", "Rivian Automotive"),
    ("LCID", "Lucid Group"),
    ("MARA", "Marathon Digital Holdings"),
    ("RIOT", "Riot Platforms"),
]


def search(query: str, limit: int = 8) -> list[dict]:
    """Rank: exact symbol > symbol prefix > name word-prefix > name substring.
    Within a tier, preserve curated order (roughly = liquidity)."""
    q = query.strip().upper()
    if not q:
        return [{"symbol": s, "name": n} for s, n in SYMBOLS[:limit]]

    exact, prefix, word, substr = [], [], [], []
    for sym, name in SYMBOLS:
        uname = name.upper()
        if sym == q:
            exact.append((sym, name))
        elif sym.startswith(q):
            prefix.append((sym, name))
        elif any(w.startswith(q) for w in uname.split()):
            word.append((sym, name))
        elif q in uname:
            substr.append((sym, name))
    ranked = exact + prefix + word + substr
    return [{"symbol": s, "name": n} for s, n in ranked[:limit]]
