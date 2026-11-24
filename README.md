# BHF Data Engineer take-home: purchase-order pipeline

Parses vendor PO text files into the template spreadsheet, flags numbers that don't add up,
loads them into SQLite idempotently, and logs what happened.

```
pip install -r requirements.txt
python po_pipeline.py input --out output --db po.db     # parse -> xlsx -> SQLite   (run it twice: the table doesn't change)
python -m pytest                                        # 10 tests
```

| File | What it does |
|---|---|
| `po_parser.py` | Parses one PO (header, address blocks, line items) and checks each line's math |
| `po_pipeline.py` | Runs the parser over files, writes the workbooks, logs, calls the loader |
| `po_loader.py`, `schema.sql` | SQLite DDL and the upsert load |
| `output/`, `logs/` | Generated workbooks; a sample run log and `idempotency_demo.txt` |

Result on the samples: 18 rows (12 + 6); 3 flagged. Sheet1 has exactly the template's headers.
A second sheet, `Flags`, lists only the rows that need a human look.

## How it works

The parser reads *structure*, not vendor names: `LABEL: value` header cells, address blocks as a title over a
`-----` underline (the *title* decides ship-to vs bill-to, so swapped order just works), and two item layouts
(a table with the UPC on the next line, or labeled `SKU:` blocks) chosen by what the file contains.
Vendor wording lives in three small alias tables at the top of `po_parser.py`.

**Assumptions (the template doesn't say):**
`Total Cost` = the PO's printed total, repeated on every row. `VENDOR`/`SHIP TO`/`BILL TO` hold the whole block
joined with ` | `. Dates are US month/day, written as ISO dates. Sample UPCs fail the GTIN check digit, so I don't validate it.

## The three planted problems

Rule: keep the row, never silently change data, never guess unless the document proves the answer, always flag.

| Case | Decision | Why |
|---|---|---|
| SKU 71097218: no UPC | Row kept, UPC blank (NULL), flagged `MISSING_UPC` | The quantities and costs are real; dropping the row loses data. A guessed UPC points at the wrong product with no warning. |
| SKU 71293252: EXT COST 4146.08 vs 4046.08 | Printed value kept, flagged `EXT_COST_MISMATCH` with both numbers | I can't tell from the line which number is wrong, so I don't "fix" it. The PO total (447,455.12) equals the sum of the *recomputed* lines, which points at this line's EXT COST as the typo. The log shows that. |
| SKU 71293276: COST `55,440` | Read as 55.440, flagged `COST_REPAIRED` | Ambiguous on its own, but 55.440 x 116 = 6431.04 matches the printed EXT COST exactly. It's only repaired when the row proves it; otherwise blank and flagged (tested). |

Also: a row the parser can't read is **skipped and logged as an error** (never silent). A missing PO#, an unknown
layout, or a repeated SKU in one PO **rejects that file**; the other files in the batch still run.

## Second layout: fixed vs flexible

**Flexible:** label wording and order, swapped address blocks, UPC inline or on the next line, table vs labeled layout.
**Assumed fixed:** plain text, one PO per file, `.` decimals, header cells separated by 2+ spaces, only DESCRIPTION
contains spaces in the table layout, US dates, each SKU once per PO.
**A third vendor would break on:** new label wording (the field comes back blank; if it's the PO#, the file is rejected:
one line to fix in the alias table), a different structure (CSV, UPC above the row, several POs per file, `$` or `1,234.56`
numbers: it raises `ParseError` or flags the row rather than guessing), and **page breaks that repeat the table header**
(later pages would be dropped; only the PO-total check in the log would show it). I haven't built multi-page support.

## Load and idempotency

`po_line` has `PRIMARY KEY (po_number, sku)` and the load is an upsert (`INSERT ... ON CONFLICT DO UPDATE`) in one
transaction. Loading the same files again gives the same table: the log line goes from
`table went 0 -> 18 rows (18 new...)` to `18 -> 18 rows (0 new...)`; see `logs/idempotency_demo.txt` and
`test_load_is_idempotent_and_updates_in_place`. A corrected re-send of a line updates it in place.
Known gap: if a PO is re-sent with a line *removed*, the old line stays; replacing a whole PO is a product decision I didn't make.
On SQL Server this would be a `MERGE`.

## Knowing something went wrong without opening the file

Each run writes `logs/po_pipeline_<timestamp>.log`: one line per flagged row (file, line number, SKU, reason) and a final
`RUN SUMMARY` line. The process **exit code** is `0` clean, `1` rows flagged or a PO total didn't reconcile, `2` a file
was rejected / a line couldn't be parsed / the load failed. A scheduler alerts on non-zero. Not built: a "no run happened"
alert, since a job that never starts writes no log.

## With more time

Vendor profiles in config plus a fixture per vendor; multi-page POs; archive processed files and quarantine rejected
ones; real alerting; `DECIMAL` columns and `MERGE`; sanity checks such as cost > retail.

## Verify it yourself

```
pip install -r requirements.txt
python -m pytest                                      # 10 passed
python po_pipeline.py input --out output --db po.db   # exit code 1: 3 rows flagged, PO TX9K2QP's total doesn't reconcile
python po_pipeline.py input --out output --db po.db   # same files again: log says "18 -> 18 rows (0 new ...)"
python -c "import sqlite3; [print(r) for r in sqlite3.connect('po.db').execute('SELECT sku, upc, cost, ext_cost, flags FROM po_line WHERE flags IS NOT NULL')]"
```

Each run also writes `output/*.xlsx` (Sheet1 = the template columns, `Flags` = rows to check) and a new file in `logs/`.

## AI assistant usage

* **Tool:** Claude, Anthropic's AI assistant. I used the claude.ai chat to build this, and Claude Code to review the
  finished repo against the assignment. Neither used an instruction file such as `CLAUDE.md`, so there is none to commit.
  The pipeline itself is plain Python (`openpyxl` for the spreadsheets) and SQLite.
* **Who wrote the code:** Claude wrote all of the code, the tests and the first draft of this README. I didn't write
  any of the code by hand.
* **How I directed and checked it:** I gave it the assignment, the two sample POs and the template, and asked for a
  pipeline that meets the brief. An earlier, larger version (audit tables, JSON summary, more sheets) was cut down to
  this one to stay within "don't over-engineer". I then read through all of the code until I understood what each part
  does and why it is there, ran the pipeline twice to check the load is idempotent, and ran the tests.

**One thing the AI got wrong:** In my own review I didn't find anything in the final code that I had to correct or
override. The assistant did get things wrong along the way, and fixed them after a run failed, not because I caught
them. For example, its first parser assumed the numbers on sample 1's `TOTALS:` line sat under their table columns.
They don't: the line is printed 18 columns to the left, so the first run crashed trying to read `119.18` as a
whole-number column. It was replaced with a simpler rule: use the labelled `EXT COST`, or the first decimal number on
the line.
