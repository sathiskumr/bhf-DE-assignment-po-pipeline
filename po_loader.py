"""Load the generated workbooks into SQLite.  python po_loader.py --db po.db output/*.xlsx

Idempotent because it is an UPSERT on the natural key (po_number, sku): a new key is inserted,
an existing key is updated in place, so loading the same file twice gives the same table.
The whole load is one transaction.
"""
import argparse
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

log = logging.getLogger("po_loader")

HEADER_TO_COL = {   # template header -> table column
    "FILENAME":       "filename",
    "BUYER":          "buyer",
    "SHIP TERMS":     "ship_terms",
    "PO#":            "po_number",
    "REF MASTER PO#": "ref_master_po",
    "SHIP DATE":      "ship_date",
    "VENDOR":         "vendor",
    "SHIP TO":        "ship_to",
    "BILL TO":        "bill_to",
    "Dept":           "dept",
    "SKU":            "sku",
    "UPC":            "upc",
    "Vendor Part #":  "vendor_part",
    "Description":    "description",
    "Retail":         "retail",
    "Cost":           "cost",
    "EXT Cost":       "ext_cost",
    "CTNS":           "ctns",
    "CSPK":           "cspk",
    "EXT QTY":        "ext_qty",
    "Cube":           "cube",
    "Kilograms":      "kilograms",
    "Total Cost":     "total_cost",
}
COLUMNS = list(HEADER_TO_COL.values()) + ["flags"]
UPSERT = (f"INSERT INTO po_line ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))}) "
          "ON CONFLICT(po_number, sku) DO UPDATE SET "
          + ", ".join(f"{c}=excluded.{c}" for c in COLUMNS if c not in ("po_number", "sku")))


def read_workbook(path):
    """Sheet1 rows (template columns) plus the matching text from the Flags sheet."""
    wb = load_workbook(path, data_only=True)
    rows = list(wb["Sheet1"].iter_rows(values_only=True))
    if set(rows[0]) != set(HEADER_TO_COL):
        raise ValueError(f"{Path(path).name}: Sheet1 headers don't match the template")
    flags = {(r[1], r[2]): r[3] for r in wb["Flags"].iter_rows(min_row=2, values_only=True)}
    out = []
    for r in rows[1:]:
        rec = {HEADER_TO_COL[h]: v for h, v in zip(rows[0], r)}
        if isinstance(rec["ship_date"], datetime):
            rec["ship_date"] = rec["ship_date"].date().isoformat()
        rec["flags"] = flags.get((rec["po_number"], rec["sku"]))
        out.append(tuple(rec[c] for c in COLUMNS))
    return out


def load_workbooks(db, paths):
    rows = [r for p in paths for r in read_workbook(p)]
    keys = [(r[COLUMNS.index("po_number")], r[COLUMNS.index("sku")]) for r in rows]
    if len(set(keys)) != len(keys):
        log.warning("[load] the same (PO#, SKU) appears in more than one input row; the last one wins")
    conn = sqlite3.connect(db)
    conn.executescript(Path(__file__).with_name("schema.sql").read_text())
    before = conn.execute("SELECT COUNT(*) FROM po_line").fetchone()[0]
    with conn:
        conn.executemany(UPSERT, rows)
    after = conn.execute("SELECT COUNT(*) FROM po_line").fetchone()[0]
    conn.close()
    log.info("[load] %s: read %d rows; table went %d -> %d rows (%d new, the rest already existed and were updated in place)",
             db, len(rows), before, after, after - before)
    return before, after


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("workbooks", nargs="+", type=Path)
    ap.add_argument("--db", default="po.db")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stdout)
    load_workbooks(a.db, a.workbooks)