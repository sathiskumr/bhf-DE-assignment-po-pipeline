import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook

import po_loader
import po_pipeline
from po_parser import ParseError, parse_po

ROOT = Path(__file__).resolve().parent.parent
S1, S2 = ROOT / "input/purchase_order_sample.txt", ROOT / "input/purchase_order_sample_2.txt"
TEMPLATE = ROOT / "template/output_template_blank.xlsx"


def po(path):
    return parse_po(path.read_text(), path.name)


def item(p, sku):
    return next(i for i in p["items"] if i["sku"] == sku)


def test_sample1_table_layout():
    p = po(S1)
    assert len(p["items"]) == 12 and p["header"]["po_number"] == "TX9K2QP"
    assert p["header"]["ship_date"].isoformat() == "2026-05-02" and p["header"]["total_cost"] == Decimal("447455.120")
    assert p["header"]["ship_to"].startswith("331 DISTRIBUTION/NORTHEAST")      # side-by-side blocks split correctly
    assert p["header"]["bill_to"].startswith("331 MERIDIAN SOURCING")
    first = p["items"][0]
    assert (first["sku"], first["upc"], first["vendor_part"], first["description"]) == \
        ("71097188", "0091120032129", "04212", "NORDPEAK ASHER T CMFST GRY")   # UPC comes from the line below
    assert [i["sku"] for i in p["items"] if i["flags"]] == ["71097218", "71293252", "71293276"]


def test_missing_upc_is_flagged_not_dropped_not_guessed():
    i = item(po(S1), "71097218")
    assert i["upc"] is None and i["flags"][0].startswith("MISSING_UPC") and i["ext_cost"] == Decimal("10077.600")


def test_ext_cost_mismatch_keeps_printed_value_and_flags_it():
    i = item(po(S1), "71293252")
    assert i["ext_cost"] == Decimal("4146.080") and i["calc_ext_cost"] == Decimal("4046.080")
    assert i["flags"][0].startswith("EXT_COST_MISMATCH") and "+100" in i["flags"][0]


def test_malformed_cost_repaired_only_because_the_row_proves_it():
    i = item(po(S1), "71293276")
    assert i["cost"] == Decimal("55.440") and i["flags"][0].startswith("COST_REPAIRED")
    unprovable = item(parse_po(S1.read_text().replace("6431.040", "9999.000")), "71293276")
    assert unprovable["cost"] is None and unprovable["flags"][0].startswith("BAD_VALUE")   # not guessed


def test_sample2_labeled_layout_swapped_blocks_all_clean():
    p = po(S2)
    assert len(p["items"]) == 6 and not any(i["flags"] for i in p["items"])
    h = p["header"]
    assert (h["po_number"], h["ref_master_po"], h["ship_date"].isoformat()) == ("PL7M3RK", "PL7K9SX", "2026-06-11")
    assert "JOLIET" in h["ship_to"] and h["ship_to"].startswith("418 DISTRIBUTION/MIDWEST")
    assert h["bill_to"].startswith("418 MERIDIAN SOURCING") and h["total_cost"] == Decimal("33040.200")
    assert (p["items"][0]["upc"], p["items"][0]["vendor_part"]) == ("0071230441087", "CL-1180")


def test_unknown_layout_and_missing_po_number_are_rejected():
    with pytest.raises(ParseError, match="new layout"):
        parse_po("ORDER 1\nWidgets x 10 @ 5.00\n")
    with pytest.raises(ParseError, match="PO number"):
        parse_po(S2.read_text().replace("PO NUMBER   : PL7M3RK", "PO NUMBER   :"))


def test_unparseable_row_is_reported_and_its_upc_is_not_given_to_the_wrong_sku():
    lines = S1.read_text().splitlines()
    n = next(i for i, l in enumerate(lines) if "71097225" in l)      # row after the SKU that has no UPC
    lines[n] = "210 71097225 04216 NORDPEAK SIMONE FQ CMFST BLU 179.99"   # truncated
    p = parse_po("\n".join(lines))
    assert len(p["items"]) == 11 and len(p["errors"]) == 2            # the row, and its stranded UPC line
    assert item(p, "71097218")["upc"] is None                         # still honestly missing


@pytest.fixture()
def workbooks(tmp_path):
    outs = []
    for f in (S1, S2):
        out = tmp_path / f"{f.stem}.xlsx"
        po_pipeline.write_workbook(po(f), TEMPLATE, out)
        outs.append(out)
    return outs


def test_output_headers_match_template_exactly(workbooks):
    expected = [c.value for c in load_workbook(TEMPLATE).active[1]]
    assert all([c.value for c in load_workbook(w)["Sheet1"][1]] == expected for w in workbooks)


def test_load_is_idempotent_and_updates_in_place(tmp_path, workbooks):
    db = str(tmp_path / "t.db")
    assert po_loader.load_workbooks(db, workbooks) == (0, 18)
    snapshot = sqlite3.connect(db).execute("SELECT * FROM po_line ORDER BY po_number, sku").fetchall()
    assert po_loader.load_workbooks(db, workbooks) == (18, 18)       # second run adds nothing
    assert sqlite3.connect(db).execute("SELECT * FROM po_line ORDER BY po_number, sku").fetchall() == snapshot

    wb = load_workbook(workbooks[1])
    wb["Sheet1"].cell(2, 16).value = 29.0                             # a corrected re-send of one line
    wb.save(workbooks[1])
    assert po_loader.load_workbooks(db, workbooks) == (18, 18)       # still 18 rows: updated, not duplicated
    assert sqlite3.connect(db).execute("SELECT cost FROM po_line WHERE sku='88304410'").fetchone()[0] == 29.0
    assert sqlite3.connect(db).execute("SELECT upc, flags IS NOT NULL FROM po_line WHERE sku='71097218'").fetchone() == (None, 1)


def test_exit_codes(tmp_path):
    base = ["--template", str(TEMPLATE), "--out", str(tmp_path / "o"), "--db", str(tmp_path / "d.db"), "--log-dir", str(tmp_path / "l")]
    assert po_pipeline.main([str(S2)] + base) == 0                    # clean
    assert po_pipeline.main([str(S1)] + base) == 1                    # flagged rows
    bad = tmp_path / "bad.txt"
    bad.write_text("not a PO\n")
    assert po_pipeline.main([str(bad)] + base) == 2                   # rejected file
