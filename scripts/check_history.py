"""CI helper: validate the committed history store's shape.

Run locally with:  python scripts/check_history.py
The store is committed so a fresh clone has trend context without a network fetch.
This validates structure only -- it never refetches, so CI has no market-data dependency.
"""

import sys

import pandas as pd

CHECKS = {
    "data/history/macro_history.csv": [
        "date", "SDI", "Commercial_Net", "fedwatch_dovish_prob", "MACRO_GATE",
        "Commercial_Net_52w_Low", "Commercial_Net_52w_High",
    ],
    "data/history/price_history.csv": ["date", "open", "high", "low", "close"],
    "data/history/spatial_history.csv": ["date", "Three_Day_Low", "Sweep_Floor", "ATR_14"],
}


def main():
    failed = False
    for path, cols in CHECKS.items():
        try:
            df = pd.read_csv(path)
        except Exception as exc:
            print(f"FAIL {path}: {exc}")
            failed = True
            continue
        missing = [c for c in cols if c not in df.columns]
        if missing or df.empty:
            print(f"FAIL {path}: missing={missing} rows={len(df)}")
            failed = True
        else:
            print(f"ok   {path}: {len(df)} rows")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
