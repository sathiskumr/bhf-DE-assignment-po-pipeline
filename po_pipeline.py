"""PO text files -> template-shaped Excel -> SQLite.   python po_pipeline.py input --out output --db po.db

Exit code: 0 clean | 1 finished but some rows are flagged | 2 a file was rejected, a line couldn't be
parsed, or the load failed. A scheduler can alert on non-zero without anyone opening a spreadsheet.
"""
import argparse
import logging
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

import po_loader
from po_parser import TOL, ParseError, parse_po

log = logging.getLogger("po_pipeline")


def template_row(po, item):
    h = po["header"]
    return {
        "FILENAME":       po["filename"],
        "BUYER":          h.get("buyer"),
        "SHIP TERMS":     h.get("ship_terms"),
        "PO#":            h["po_number"],
        "REF MASTER PO#": h.get("ref_master_po"),
        "SHIP DATE":      h["ship_date"],
        "VENDOR":         h.get("vendor"),
        "SHIP TO":        h.get("ship_to"),
        "BILL TO":        h.get("bill_to"),
        "Dept":           item["dept"],
        "SKU":            item["sku"],
        "UPC":            item["upc"],
        "Vendor Part #":  item["vendor_part"],
        "Description":    item["description"],
        "Retail":         item["retail"],
        "Cost":           item["cost"],
        "EXT Cost":       item["ext_cost"],
        "CTNS":           item["ctns"],
        "CSPK":           item["cspk"],
        "EXT QTY":        item["ext_qty"],
        "Cube":           item["cube"],
        "Kilograms":      item["kg"],
        "Total Cost":     h["total_cost"],   # assumption: the PO-level printed total, repeated on every row
    }


def write_workbook(po, template, out_path):
    wb = load_workbook(template)
    ws = wb.active                                     # Sheet1 keeps the template's headers untouched
    headers = [c.value for c in ws[1]]
    for item in po["items"]:
        row = template_row(po, item)
        assert set(row) == set(headers), "template headers changed"
        ws.append([row[h] for h in headers])
    flags = wb.create_sheet("Flags")                   # only rows that need a human look
    flags.append(["FILENAME", "PO#", "SKU", "FLAGS"])
    for item in po["items"]:
        if item["flags"]:
            flags.append([po["filename"], po["header"]["po_number"], item["sku"], " || ".join(item["flags"])])
    wb.save(out_path)


def process(path, template, out_dir, counts):
    try:
        po = parse_po(path.read_text(encoding="utf-8", errors="replace"), path.name)
    except ParseError as e:
        log.error("[%s] FILE REJECTED, nothing written: %s", path.name, e)
        counts["rejected"] += 1
        return None
    for err in po["errors"]:
        log.error("[%s] %s", path.name, err)
        counts["line_errors"] += 1
    for item in po["items"]:
        for flag in item["flags"]:
            log.warning("[%s] line %d SKU %s %s", path.name, item["line_no"], item["sku"], flag)
        counts["rows"] += 1
        counts["flagged"] += bool(item["flags"])

    # Cross-check against the PO's own printed total: also catches lines the parser dropped.
    total, items = po["header"]["total_cost"], po["items"]
    printed_sum = sum((i["ext_cost"] or Decimal(0) for i in items), Decimal(0))
    recomputed_sum = sum((i["calc_ext_cost"] if i["calc_ext_cost"] is not None else (i["ext_cost"] or Decimal(0))
                          for i in items), Decimal(0))
    if total is not None and abs(total - printed_sum) > TOL:
        log.warning("[%s] PO total %s != sum of printed line EXT COSTs %s (sum of recomputed lines = %s)",
                    path.name, total, printed_sum, recomputed_sum)
        counts["total_mismatches"] += 1

    out = out_dir / f"{path.stem}.xlsx"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_workbook(po, template, out)
    log.info("[%s] PO# %s: %d rows -> %s", path.name, po["header"]["po_number"], len(items), out)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", type=Path, help="PO .txt files and/or folders of them")
    ap.add_argument("--template", type=Path, default=Path("template/output_template_blank.xlsx"))
    ap.add_argument("--out", type=Path, default=Path("output"))
    ap.add_argument("--db", default="po.db")
    ap.add_argument("--log-dir", type=Path, default=Path("logs"))
    args = ap.parse_args(argv)

    args.log_dir.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", force=True,
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(args.log_dir / f"po_pipeline_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8")])
    files = sorted(f for p in args.inputs for f in (p.glob("*.txt") if p.is_dir() else [p]))
    counts = dict(rows=0, flagged=0, rejected=0, line_errors=0, total_mismatches=0)
    outputs = [o for f in files if (o := process(f, args.template, args.out, counts))]

    load_failed = False
    if outputs:
        try:
            po_loader.load_workbooks(args.db, outputs)
        except Exception:
            log.exception("LOAD FAILED")
            load_failed = True
    failed = load_failed or counts["rejected"] or counts["line_errors"] or not files
    code = 2 if failed else 1 if counts["flagged"] or counts["total_mismatches"] else 0
    log.info("RUN SUMMARY exit=%d files=%d rejected=%d rows=%d flagged_rows=%d line_errors=%d total_mismatches=%d",
             code, len(files), counts["rejected"], counts["rows"], counts["flagged"], counts["line_errors"],
             counts["total_mismatches"])
    return code


if __name__ == "__main__":
    sys.exit(main())