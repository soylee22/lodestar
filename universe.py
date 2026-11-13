"""The MarketFighter universe, as the author states it (post 03, post 07).

Fifteen indices. Eight MSCI factor indices, seven S&P 500 sector indices.
The author ranks the INDEX, not the ETF, on month-end prices, in EUR.
Verified: his published MSCI World benchmark column matches the MSCI World
PRICE index (STRD) in EUR to 0.0025pp mean absolute error over 126 months.
"""

# MSCI index codes, recovered via Yahoo's index symbol search (^<code>-<CUR>-<VARIANT>).
FACTOR = {
    "USA_MOM":   "703025",   # MSCI USA MOMENTUM
    "USA_QUAL":  "702789",   # MSCI USA QUALITY
    "USA_VAL":   "705973",   # MSCI USA ENHANCED VALUE
    "USA_SMALL": "106229",   # MSCI USA SMALL CAP
    "EUR_MOM":   "703764",   # MSCI EUROPE MOMENTUM
    "EUR_QUAL":  "702790",   # MSCI EUROPE QUALITY
    "EUR_VAL":   "705382",   # MSCI EUROPE ENHANCED VALUE
    "EUR_SMALL": "106233",   # MSCI EUROPE SMALL CAP
}

# Alternative index variants the ETFs actually track today, kept so the fit can
# choose rather than us assuming.
FACTOR_ALT = {
    "USA_MOM_SR":   "731834",  # MSCI USA MOMENTUM SR VARIANT (what MTUM tracks)
    "USA_SNQUAL":   "705911",  # MSCI USA SECTOR NEUTRAL QUALITY (what QUAL tracks)
    "USA_SNQUAL2":  "708289",
}

BENCHMARKS = {
    "WORLD":  "990100",   # MSCI World  (his stated benchmark)
    "USA":    "984000",   # MSCI USA
}

# S&P 500 GICS sector price indices on Yahoo. Energy is ^GSPE, not ^SP500-10.
SECTOR_YF = {
    "CONS_DISC":  "^SP500-25",
    "CONS_STAP":  "^SP500-30",
    "ENERGY":     "^GSPE",
    "HEALTH":     "^SP500-35",
    "INDUST":     "^SP500-20",
    "INFOTECH":   "^SP500-45",
    "MATERIALS":  "^SP500-15",
}

FACTOR_NAMES = {
    "USA_MOM": "USA Momentum", "USA_QUAL": "USA Quality", "USA_VAL": "USA Value",
    "USA_SMALL": "USA Small Cap", "EUR_MOM": "Europe Momentum",
    "EUR_QUAL": "Europe Quality", "EUR_VAL": "Europe Value",
    "EUR_SMALL": "Europe Small Cap",
}
SECTOR_NAMES = {
    "CONS_DISC": "Consumer Discretionary", "CONS_STAP": "Consumer Staples",
    "ENERGY": "Energy", "HEALTH": "Health Care", "INDUST": "Industrials",
    "INFOTECH": "Information Technology", "MATERIALS": "Materials",
}

# The tradable GBP-denominated London lines for each slot (post 07 gives
# Xetra + US; the .L column is the same ISIN listed in sterling on LSE).
TRADABLE = {
    "USA_MOM":   ("IE00BD1F4N50", "QDVA.DE", "IUMF.L",  "MTUM"),
    "USA_QUAL":  ("IE00BD1F4L37", "QDVB.DE", "IUQF.L",  "QUAL"),
    "USA_VAL":   ("IE00BD1F4M44", "QDVI.DE", "IUVF.L",  "VLUE"),
    "USA_SMALL": ("IE00B3VWM098", "SXRG.DE", "CUS1.L",  "SMLF"),
    "EUR_MOM":   ("IE00BQN1K786", "CEMR.DE", "IEFM.L",  "IMTM"),
    "EUR_QUAL":  ("IE00BQN1K562", "CEMQ.DE", "IEFQ.L",  "IQLT"),
    "EUR_VAL":   ("IE00BQN1K901", "CEMS.DE", "IEFV.L",  "IVLU"),
    "EUR_SMALL": ("LU0322253906", "XXSC.DE", "XXSC.L",  "IEUS"),
    "CONS_DISC": ("IE00B4MCHD36", "QDVK.DE", "ICDU.L",  "XLY"),
    "CONS_STAP": ("IE00B40B8R38", "2B7D.DE", "ICSU.L",  "XLP"),
    "ENERGY":    ("IE00B42NKQ00", "QDVF.DE", "IESU.L",  "XLE"),
    "HEALTH":    ("IE00B43HR379", "QDVG.DE", "IHCU.L",  "XLV"),
    "INDUST":    ("IE00B4LN9N13", "2B7C.DE", "IISU.L",  "XLI"),
    "INFOTECH":  ("IE00B3WJKG14", "QDVE.DE", "IITU.L",  "XLK"),
    "MATERIALS": ("IE00B4MKCJ84", "2B7B.DE", "IMSU.L",  "XLB"),
}

FACTOR_SLOTS = list(FACTOR)
SECTOR_SLOTS = list(SECTOR_YF)

NAMES = {**FACTOR_NAMES, **SECTOR_NAMES}
LSE = {slot: row[2] for slot, row in TRADABLE.items()}
FACTOR_PROXY = {slot: row[3] for slot, row in TRADABLE.items() if slot in FACTOR}
SECTOR_PROXY = {slot: row[3] for slot, row in TRADABLE.items() if slot in SECTOR_YF}


def held_now(root=None, today=None) -> tuple[str, str, str]:
    """Return (factor_slot, sector_slot, signal_month) from the archived book."""
    import json
    from datetime import date as _date
    from pathlib import Path
    import pandas as pd

    base_dir = Path(root) if root is not None else Path(__file__).resolve().parent
    ref_day = today if today is not None else _date.today()
    data_dir = base_dir / "data"

    history_path = data_dir / "signal_history.json"
    if history_path.exists():
        history = json.loads(history_path.read_text())
        expected = str(pd.Timestamp(ref_day).to_period("M") - 1)
        eligible = sorted(month for month in history if month <= expected)
        if eligible:
            month = eligible[-1]
            row = history[month]
            if row.get("cash"):
                return "CASH", "CASH", month
            return row["factor"], row["sector"], month

    last_alert = data_dir / "last_alert.json"
    if last_alert.exists():
        state = json.loads(last_alert.read_text())
        picks = state.get("picks", "")
        if picks == "CASH":
            return "CASH", "CASH", state.get("month", "?")
        f, _, s = picks.partition("+")
        if f and s:
            return f, s, state.get("month", "?")

    book_csv = data_dir / "final_book.csv"
    if book_csv.exists():
        book = pd.read_csv(book_csv, index_col=0)
        if not book.empty:
            month = str(book.index[-1])
            row = book.iloc[-1]
            if bool(row.get("cash", False)):
                return "CASH", "CASH", month
            return str(row["factor"]), str(row["sector"]), month

    raise RuntimeError("No archived current holding found in data/")
