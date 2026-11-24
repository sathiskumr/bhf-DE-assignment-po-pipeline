"""Parse a vendor purchase-order .txt into header fields + line items, and check each line's math.

Nothing is keyed on a vendor name; the parser reads structure:
  header  "LABEL: value" cells separated by 2+ spaces       (alias table below)
  blocks  a title over a ----- underline (VENDOR / SHIP TO / BILL TO), possibly side by side
  items   either a table (header row over a ----- underline, UPC on the line below each row)
          or labeled blocks ("SKU: ...  UPC: ..." separated by blank lines)
A layout it doesn't recognise raises ParseError instead of guessing.
"""
import re
from datetime import datetime
from decimal import Decimal

TOL = Decimal("0.001")   # money is printed to 3 decimals

# The only vendor-specific knowledge: what each vendor calls things.
# printed label -> our field name (several labels can point to the same field).
HEADER_LABELS = {
    "BUYER":          "buyer",
    "SHIP TERMS":     "ship_terms",
    "PO#":            "po_number",
    "PO NUMBER":      "po_number",
    "REF MASTER PO#": "ref_master_po",
    "REF PO":         "ref_master_po",
    "SHIP DATE":      "ship_date",
    "VENDOR NAME":    "vendor_name",
}
BLOCK_TITLES = {
    "VENDOR":         "vendor",
    "VENDOR ADDRESS": "vendor",
    "SHIP TO":        "ship_to",
    "BILL TO":        "bill_to",
}
ITEM_FIELDS = {
    "DPT":          "dept",
    "DEPT":         "dept",
    "SKU":          "sku",
    "SKU/UPC":      "sku",
    "UPC":          "upc",
    "VENDOR PART#": "vendor_part",
    "DESC":         "description",
    "DESCRIPTION":  "description",
    "RETAIL":       "retail",
    "COST":         "cost",
    "EXT COST":     "ext_cost",
    "CTNS":         "ctns",
    "CARTONS":      "ctns",
    "CSPK":         "cspk",
    "CASE PACK":    "cspk",
    "EXT QTY":      "ext_qty",
    "CUBE":         "cube",
    "KILOGRAMS":    "kg",
    "KG":           "kg",
}
TEXT_FIELDS = ("dept", "sku", "upc", "vendor_part", "description")
INT_FIELDS = ("ctns", "cspk", "ext_qty")
DEC_FIELDS = ("retail", "ext_cost", "cube", "kg")     # "cost" is handled on its own (see build_item)


class ParseError(Exception):
    """The whole file can't be trusted. The caller skips it and says so loudly."""


# ---------------------------------------------------------------- small helpers
def cells(line):
    """Split on runs of 2+ spaces. 'KEY : v' is normalised to 'KEY: v'."""
    return [c.strip() for c in re.split(r"\s{2,}", re.sub(r"\s+:", ":", line.strip())) if c.strip()]


def labeled(cell):
    m = re.match(r"([^:]{1,30}):\s*(.*)$", cell)
    return (m.group(1).strip().upper(), m.group(2).strip()) if m else (None, None)


def is_underline(line):
    s = line.strip()
    return bool(s) and "-" in s and set(s) <= {"-", " "}


def columns_over_underline(title_line, underline):
    """Start column and upper-cased title of every ----- run in the underline."""
    starts = [m.start() for m in re.finditer(r"-+", underline)]
    titles = [title_line[s:e].strip().upper() for s, e in zip(starts, starts[1:] + [None])]
    return starts, titles


def parse_date(text):
    """US M/D/YY or M/D/YYYY -> date. If it isn't one, keep the raw text rather than lose it."""
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except (ValueError, TypeError):
            pass
    return text


# ---------------------------------------------------------------- header + address blocks
def parse_header(lines):
    out = {}
    for line in lines:
        for cell in cells(line):
            label, value = labeled(cell)
            if label in HEADER_LABELS:
                out.setdefault(HEADER_LABELS[label], value)
    for i in range(len(lines) - 1):                      # blocks: title line over an underline
        if not (lines[i].strip() and is_underline(lines[i + 1])):
            continue
        starts, titles = columns_over_underline(lines[i], lines[i + 1])
        cols = [[] for _ in starts]
        for body in lines[i + 2:]:
            if not body.strip():
                break
            for run in re.finditer(r"\S+(?: \S+)*", body):       # text runs; 2+ spaces end a run
                k = max([k for k, s in enumerate(starts) if run.start() >= s - 4] or [0])
                cols[k].append(run.group())
        for title, text in zip(titles, cols):
            if title in BLOCK_TITLES and text:
                out.setdefault(BLOCK_TITLES[title], " | ".join(text))
    out.setdefault("vendor", out.get("vendor_name"))
    return out


# ---------------------------------------------------------------- one line item
def build_item(raw, line_no):
    """raw: field -> printed string. Converts types and records anything suspicious in item['flags']."""
    item = {"line_no": line_no, "flags": []}
    for f in TEXT_FIELDS:
        item[f] = (raw.get(f) or "").strip() or None
    if not item["sku"]:
        raise ValueError("no SKU")
    for f in INT_FIELDS + DEC_FIELDS + ("cost",):
        item[f] = None
    for f in INT_FIELDS + DEC_FIELDS:
        if raw.get(f):
            try:
                tok = raw[f].replace(",", "") if f in INT_FIELDS else raw[f]
                item[f] = int(tok) if f in INT_FIELDS else Decimal(tok)
            except (ValueError, ArithmeticError):
                item["flags"].append(f"BAD_VALUE: {f} printed as {raw[f]!r}, left blank")

    # COST like "55,440" is ambiguous (55440 or 55.440). Accept the decimal reading ONLY if the row proves
    # it: cost x qty must equal the printed EXT COST. Otherwise leave it blank and flag it.
    tok = raw.get("cost")
    if tok:
        qty = item["ctns"] * item["cspk"] if item["ctns"] and item["cspk"] else item["ext_qty"]
        try:
            item["cost"] = Decimal(tok)
        except ArithmeticError:
            guess = Decimal(tok.replace(",", ".")) if re.fullmatch(r"\d{1,3},\d{3}", tok) else None
            if guess is not None and qty and item["ext_cost"] is not None and abs(guess * qty - item["ext_cost"]) <= TOL:
                item["cost"] = guess
                item["flags"].append(f"COST_REPAIRED: printed {tok!r}, read as {guess} because "
                                     f"{guess} x {qty} = {item['ext_cost']} matches the printed EXT COST")
            else:
                item["flags"].append(f"BAD_VALUE: cost printed as {tok!r} and could not be resolved, left blank")
    return item
  

def check(item):
    """Recompute EXT QTY and EXT COST from the other columns and flag disagreements."""
    flags = item["flags"]
    if not item["upc"]:
        flags.append("MISSING_UPC: no UPC found for this SKU (left blank, not guessed)")
    item["calc_ext_qty"] = item["calc_ext_cost"] = None
    if item["ctns"] is not None and item["cspk"] is not None:
        item["calc_ext_qty"] = item["ctns"] * item["cspk"]
        if item["ext_qty"] is not None and item["ext_qty"] != item["calc_ext_qty"]:
            flags.append(f"EXT_QTY_MISMATCH: printed {item['ext_qty']}, CTNS x CSPK = {item['calc_ext_qty']}")
    if item["cost"] is not None and item["calc_ext_qty"] is not None:
        item["calc_ext_cost"] = item["cost"] * item["calc_ext_qty"]
        if item["ext_cost"] is not None and abs(item["ext_cost"] - item["calc_ext_cost"]) > TOL:
            flags.append(f"EXT_COST_MISMATCH: printed {item['ext_cost']}, COST x QTY = {item['calc_ext_cost']} "
                         f"(diff {item['ext_cost'] - item['calc_ext_cost']:+})")


# ---------------------------------------------------------------- the two item layouts
def find_table_header(lines):
    for i in range(len(lines) - 1):
        if is_underline(lines[i + 1]):
            _, titles = columns_over_underline(lines[i], lines[i + 1])
            if sum(t in ITEM_FIELDS for t in titles) >= 5:
                return i
    return None


def parse_table(lines, h, errors):
    """One row per SKU; the UPC is a digits-only line under it. Only DESCRIPTION may contain spaces."""
    _, titles = columns_over_underline(lines[h], lines[h + 1])
    fields = [ITEM_FIELDS.get(t) for t in titles]
    d = fields.index("description")
    n_after = len(fields) - d - 1
    items, prev = [], None            # prev = the item produced by the line immediately above, if any
    for i in range(h + 2, len(lines)):
        if not lines[i].strip():
            break
        t = lines[i].split()
        if len(t) == 1 and t[0].isdigit():
            if prev is not None and not prev["upc"]:
                prev["upc"] = t[0]
            else:   # never attach it to an older item: if the row above failed, this UPC isn't theirs
                errors.append(f"line {i + 1}: UPC {t[0]} does not follow a parsed item row")
            prev = None
            continue
        prev = None
        if len(t) < len(fields):
            errors.append(f"line {i + 1}: expected {len(fields)} columns, found {len(t)}: {lines[i].strip()!r}")
            continue
        values = t[:d] + [" ".join(t[d:len(t) - n_after])] + t[len(t) - n_after:]
        try:
            prev = build_item(dict(zip(fields, values)), i + 1)
            items.append(prev)
        except ValueError as e:
            errors.append(f"line {i + 1}: {e}: {lines[i].strip()!r}")
    return items


def parse_labeled(lines, start, errors):
    """Blank-line-separated blocks of 'LABEL: value' cells; the block with a SKU is an item."""
    items, i = [], start
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        first, block = i, []
        while i < len(lines) and lines[i].strip():
            block.append(lines[i])
            i += 1
        if re.match(r"\s*TOTALS?\b", block[0], re.I):
            break
        raw, current = {}, None
        for line in block:
            for cell in cells(line):
                label, value = labeled(cell)
                if label in ITEM_FIELDS:
                    current = ITEM_FIELDS[label]
                    raw[current] = value
                elif current == "description":
                    raw[current] += " " + cell
        if "sku" in raw:
            try:
                items.append(build_item(raw, first + 1))
            except ValueError as e:
                errors.append(f"line {first + 1}: {e}")
    return items


def find_total_cost(lines):
    """PO-level total: 'EXT COST <n>' on the TOTALS line, else the first decimal number on it."""
    for line in lines:
        if re.match(r"\s*TOTALS?\b", line, re.I):
            m = re.search(r"EXT COST\s*:?\s*([\d,]+\.\d+)", line, re.I) or re.search(r"([\d,]+\.\d+)", line)
            return Decimal(m.group(1).replace(",", "")) if m else None
    return None


# ---------------------------------------------------------------- entry point
def parse_po(text, filename="<memory>"):
    lines = text.splitlines()

    # 1. Detect the layout: where do the line items start?
    #    Table layout (sample 1) if a header row sits over a ----- underline,
    #    otherwise the first line starting with "SKU:" (sample 2).
    h = find_table_header(lines)
    start = h if h is not None else next(
        (i for i, l in enumerate(lines) if (cells(l) or [""])[0].upper().startswith("SKU:")), None)
    if start is None:   # neither layout found: reject rather than guess
        raise ParseError("no line items recognised (no table header and no 'SKU:' blocks): looks like a new layout")

    # 2. Header fields come only from the lines ABOVE the items,
    #    so an item label like "COST:" can't be mistaken for a header field.
    header = parse_header(lines[:start])
    if not header.get("po_number"):   # PO# is part of the table key, so without it the file can't be loaded
        raise ParseError("PO number not found, so rows can't be keyed")
    header["ship_date"] = parse_date(header.get("ship_date"))     # "5/02/26" -> date(2026, 5, 2)
    header["total_cost"] = find_total_cost(lines[start:])         # PO-level total from the TOTALS line

    # 3. Read the items with the reader for this layout.
    #    Lines that can't become a row go into `errors`: reported, never silently dropped.
    errors = []
    items = parse_table(lines, h, errors) if h is not None else parse_labeled(lines, start, errors)
    if not items:
        raise ParseError("layout recognised but no line items could be extracted")

    # 4. Whole-file rule: each SKU must appear once, because (PO#, SKU) is the database key.
    skus = [it["sku"] for it in items]
    if len(set(skus)) != len(skus):   # a set drops duplicates, so a shorter set means a repeat
        raise ParseError("the same SKU appears twice in this PO, which would break the (PO#, SKU) key")

    # 5. Recompute EXT QTY / EXT COST and add flags to rows that don't add up.
    for it in items:
        check(it)

    return {"filename": filename, "header": header, "items": items, "errors": errors}