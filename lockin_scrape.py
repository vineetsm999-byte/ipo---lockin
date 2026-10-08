"""
lockin_scrape.py - daily job (GitHub Actions)

Reads Chittorgarh's anchor lock-in report (Mainboard + SME), merges it into lockin_data.csv
(rows are only added or updated, never dropped) and writes the file back.
Exits with an error if the scrape looks broken, so GitHub emails you instead of saving bad data.
"""
import io
import re
import sys
from datetime import date

import pandas as pd

CSV_PATH = "lockin_data.csv"
FROM_DATE = pd.Timestamp("2026-05-01")
OUT_COLS = ["Type", "Listing Date", "Name", "1M Date", "1M Qty", "3M Date", "3M Qty"]
MIN_NEW_ROWS = 20          # fewer rows than this = scrape is broken, do not touch the CSV
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

NEXT_SELECTORS = [
    "li.next:not(.disabled) a", ".paginate_button.next:not(.disabled)", "a.next:not(.disabled)",
    "li.page-item:not(.disabled) a:has-text('Next')", "a:has-text('Next')", "button:has-text('Next')",
    "[aria-label*='Next' i]", "[title*='Next' i]",
    "a:has-text('\u203a')", "a:has-text('\u00bb')", "button:has-text('\u203a')", "button:has-text('\u00bb')",
]


def find_next(page, n):
    cands = list(NEXT_SELECTORS) + [f"a:text-is('{n}')", f"button:text-is('{n}')", f"li:text-is('{n}')"]
    for sel in cands:
        loc = page.locator(sel)
        for k in range(min(loc.count(), 5)):
            e = loc.nth(k)
            try:
                if not e.is_visible():
                    continue
                if e.evaluate("el => !!el.closest('.disabled, [aria-disabled=true]')"):
                    continue
                return e
            except Exception:
                continue
    return None


def scrape_all(url, max_pages=40):
    from playwright.sync_api import sync_playwright
    frames = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(user_agent=UA).new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_selector("table tbody tr", timeout=30000)
        page.wait_for_timeout(1500)
        for i in range(max_pages):
            frames.append(max(pd.read_html(io.StringIO(page.content())), key=len))
            before = page.locator("table tbody tr").first.inner_text()
            print(f"  page {i + 1}: {len(frames[-1])} rows", flush=True)
            nxt = find_next(page, i + 2)
            if nxt is None:
                break
            try:
                nxt.click(timeout=3000)
                page.wait_for_function(
                    "old => { const r = document.querySelector('table tbody tr'); return !!r && r.innerText !== old; }",
                    arg=before, timeout=8000)
            except Exception:
                break
        browser.close()
    return frames


def norm(c):
    return re.sub(r"[^a-z0-9]", "", str(c).lower())


def pick(df, *keys):
    for c in df.columns:
        if all(k in norm(c) for k in keys):
            return c
    return None


def to_qty(v):
    s = str(v).replace(",", "").strip()
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def build(frames, label):
    """One row per lock-in tranche in the report -> one row per IPO (1M and 3M)."""
    df = pd.concat(frames, ignore_index=True)
    c_name, c_list = pick(df, "company"), pick(df, "listing", "date")
    c_exp, c_qty = pick(df, "expiry"), pick(df, "shares", "locked")
    if None in (c_name, c_list, c_exp, c_qty):
        sys.exit(f"ERROR: report columns changed for {label}: {[str(c) for c in df.columns]}")
    d = pd.DataFrame({
        "Name": df[c_name],
        "Listing Date": pd.to_datetime(df[c_list], errors="coerce", format="mixed", dayfirst=True),
        "Expiry": pd.to_datetime(df[c_exp], errors="coerce", format="mixed", dayfirst=True),
        "Qty": df[c_qty].map(to_qty),
    })
    d = d[d["Name"].notna()].dropna(subset=["Expiry"]).drop_duplicates()
    out = []
    for (name, lst), g in d.groupby(["Name", "Listing Date"], dropna=False):
        g = g.sort_values("Expiry")
        a, c = g.iloc[0], (g.iloc[-1] if len(g) > 1 else None)
        out.append({"Type": label, "Listing Date": lst, "Name": name,
                    "1M Date": a["Expiry"], "1M Qty": a["Qty"],
                    "3M Date": c["Expiry"] if c is not None else pd.NaT,
                    "3M Qty": c["Qty"] if c is not None else None})
    return pd.DataFrame(out)


def iso(series):
    return pd.to_datetime(series, errors="coerce").dt.strftime("%Y-%m-%d").fillna("")


def to_out(df):
    out = pd.DataFrame({
        "Type": df["Type"],
        "Listing Date": iso(df["Listing Date"]),
        "Name": df["Name"].astype(str).str.strip(),
        "1M Date": iso(df["1M Date"]),
        "1M Qty": pd.to_numeric(df["1M Qty"], errors="coerce").round().astype("Int64"),
        "3M Date": iso(df["3M Date"]),
        "3M Qty": pd.to_numeric(df["3M Qty"], errors="coerce").round().astype("Int64"),
    })
    return out[OUT_COLS]


def read_old():
    try:
        d = pd.read_csv(CSV_PATH, dtype=str, keep_default_na=False)
    except FileNotFoundError:
        return pd.DataFrame(columns=OUT_COLS)
    for c in ("1M Qty", "3M Qty"):
        d[c] = pd.to_numeric(d[c], errors="coerce").round().astype("Int64")
    return d[OUT_COLS]


def merge(old, new):
    """Union of old and new; same Type+Name -> new wins. Nothing is ever dropped."""
    both = pd.concat([old, new], ignore_index=True)
    both = both.drop_duplicates(subset=["Type", "Name"], keep="last")
    lst = pd.to_datetime(both["Listing Date"], errors="coerce")
    both = both[lst.isna() | (lst >= FROM_DATE)].copy()
    both["_k"] = pd.to_datetime(both["Listing Date"], errors="coerce")
    both = both.sort_values(["_k", "Name"], ascending=[False, True], na_position="first")
    return both.drop(columns="_k").reset_index(drop=True)[OUT_COLS]


def main():
    today = date.today()
    years = [today.year] + ([today.year - 1] if today.month <= 4 else [])
    parts = []
    for year in years:
        for kind, label in (("mainboard", "Mainboard"), ("sme", "SME")):
            url = f"https://www.chittorgarh.com/report/anchor-investor-lock-in-end-dates/156/{kind}/?year={year}"
            print(f"--- {label} {year} ---", flush=True)
            frames = scrape_all(url)
            if frames:
                r = build(frames, label)
                if len(r):
                    parts.append(r)
    if not parts:
        sys.exit("ERROR: no data scraped (site changed or blocked the runner)")
    new = to_out(pd.concat(parts, ignore_index=True))
    if len(new) < MIN_NEW_ROWS:
        sys.exit(f"ERROR: only {len(new)} rows scraped, expected {MIN_NEW_ROWS}+. CSV left untouched.")
    old = read_old()
    merged = merge(old, new)
    merged.to_csv(CSV_PATH, index=False)
    print(f"Done. scraped={len(new)} old={len(old)} total={len(merged)} (+{len(merged) - len(old)})")


if __name__ == "__main__":
    main()
