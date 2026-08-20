"""Find iShares product IDs by scanning, because the screener is broken.

iShares serves point-in-time holdings from
  /uk/individual/en/products/<id>/fund/1506575576011.ajax?...&asOfDate=YYYYMMDD
but gives no way to look up <id> from a ticker or ISIN: the screener returns
HTTP 500 and the product pages are behind a JavaScript wall.

IDs are dense in blocks (251795-251802 all resolve, 251810 does not), so this
does a sparse sweep to locate the blocks, then a dense sweep inside them, and
fingerprints each fund by its sector mix and largest holdings. Our fifteen are
then matched against fingerprints taken from the funds' current top-ten lists.

Phase 1 (sparse) and phase 2 (dense) both cache to data/ishares_index.json, so
this is resumable and never refetches an id it already knows.
"""
import csv, io, json, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import urllib.request

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
DATA.mkdir(exist_ok=True)
INDEX = DATA / "ishares_index.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
BASE = ("https://www.ishares.com/uk/individual/en/products/{id}/fund/"
        "1506575576011.ajax?fileType=csv&fileName=h&dataType=fund&asOfDate={d}")
LOCK = threading.Lock()


def fetch(pid, date="20260630", timeout=25):
    try:
        req = urllib.request.Request(BASE.format(id=pid, d=date), headers=UA)
        return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
    except Exception:
        return None


def fingerprint(text):
    """Sector mix and the largest holdings, from a holdings CSV."""
    if not text or "Ticker" not in text:
        return None
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("Ticker,")), None)
    if start is None:
        return None
    rdr = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    rows, sectors = [], {}
    for r in rdr:
        t = (r.get("Ticker") or "").strip()
        w = (r.get("Weight (%)") or "").replace(",", "").strip()
        s = (r.get("Sector") or "").strip()
        try:
            w = float(w)
        except ValueError:
            continue
        if not t or w <= 0:
            continue
        rows.append((t, w))
        sectors[s] = sectors.get(s, 0.0) + w
    if not rows:
        return None
    rows.sort(key=lambda x: -x[1])
    top = sectors and max(sectors.items(), key=lambda kv: kv[1])
    return {"n": len(rows), "top": [t for t, _ in rows[:10]],
            "top_w": [round(w, 2) for _, w in rows[:5]],
            "sector_top": top[0] if top else "", "sector_top_w": round(top[1], 1) if top else 0.0}


def sweep(ids, label, workers=8):
    idx = json.loads(INDEX.read_text()) if INDEX.is_file() else {}
    todo = [i for i in ids if str(i) not in idx]
    print(f"{label}: {len(todo)} ids to try ({len(idx)} already known)")
    done = [0]

    def work(pid):
        fp = fingerprint(fetch(pid))
        with LOCK:
            idx[str(pid)] = fp
            done[0] += 1
            if done[0] % 100 == 0:
                print(f"  {done[0]}/{len(todo)}  hits={sum(1 for v in idx.values() if v)}", flush=True)
        time.sleep(0.15)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, todo))
    INDEX.write_text(json.dumps(idx))
    hits = {k: v for k, v in idx.items() if v}
    print(f"{label} done: {len(hits)} valid funds known")
    return idx


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "sparse"
    if mode == "sparse":
        # locate the blocks: every 25th id across the plausible range
        ids = list(range(240000, 340000, 25))
        idx = sweep(ids, "sparse sweep")
        blocks = sorted({int(k) // 1000 for k, v in idx.items() if v})
        print("\nblocks with hits (thousands):", blocks)
        (DATA / "ishares_blocks.json").write_text(json.dumps(blocks))
    else:
        blocks = json.loads((DATA / "ishares_blocks.json").read_text())
        ids = [i for b in blocks for i in range(b * 1000, (b + 1) * 1000)]
        sweep(ids, f"dense sweep over {len(blocks)} blocks")


if __name__ == "__main__":
    main()
