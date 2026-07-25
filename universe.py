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

# The tradable London/Xetra lines for each slot (post 07 gives Xetra + US;
# the .L column is the same ISIN listed in London).
TRADABLE = {
    "USA_MOM":   ("IE00BD1F4N50", "QDVA.DE", "IUMO.L",  "MTUM"),
    "USA_QUAL":  ("IE00BD1F4L37", "QDVB.DE", "IUQA.L",  "QUAL"),
    "USA_VAL":   ("IE00BD1F4M44", "QDVI.DE", "IUVL.L",  "VLUE"),
    "USA_SMALL": ("IE00B3VWM098", "SXRG.DE", "CUSS.L",  "SMLF"),
    "EUR_MOM":   ("IE00BQN1K786", "CEMR.DE", "IEMO.L",  "IMTM"),
    "EUR_QUAL":  ("IE00BQN1K562", "CEMQ.DE", "IEQU.L",  "IQLT"),
    "EUR_VAL":   ("IE00BQN1K901", "CEMS.DE", "IEVL.L",  "IVLU"),
    "EUR_SMALL": ("LU0322253906", "XXSC.DE", "XXSC.L",  "IEUS"),
    "CONS_DISC": ("IE00B4MCHD36", "QDVK.DE", "IUCD.L",  "XLY"),
    "CONS_STAP": ("IE00B40B8R38", "2B7D.DE", "IUCS.L",  "XLP"),
    "ENERGY":    ("IE00B42NKQ00", "QDVF.DE", "IUES.L",  "XLE"),
    "HEALTH":    ("IE00B43HR379", "QDVG.DE", "IHCU.L",  "XLV"),
    "INDUST":    ("IE00B4LN9N13", "2B7C.DE", "IUIS.L",  "XLI"),
    "INFOTECH":  ("IE00B3WJKG14", "QDVE.DE", "IUIT.L",  "XLK"),
    "MATERIALS": ("IE00B4MKCJ84", "2B7B.DE", "IUMS.L",  "XLB"),
}

FACTOR_SLOTS = list(FACTOR)
SECTOR_SLOTS = list(SECTOR_YF)
