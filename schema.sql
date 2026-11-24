-- One row per (PO, SKU). Re-loading the same PO hits the same key and updates in place.
-- Money is REAL here for simplicity; on SQL Server it would be DECIMAL(18,3).
CREATE TABLE IF NOT EXISTS po_line (
    po_number      TEXT NOT NULL,
    sku            TEXT NOT NULL,
    filename       TEXT NOT NULL,      -- which source file the row came from
    buyer          TEXT,
    ship_terms     TEXT,
    ref_master_po  TEXT,
    ship_date      TEXT,               -- ISO date
    vendor         TEXT,
    ship_to        TEXT,
    bill_to        TEXT,
    dept           TEXT,
    upc            TEXT,               -- TEXT: leading zeros matter; NULL when the PO had none
    vendor_part    TEXT,               -- TEXT: '04212' is not the number 4212
    description    TEXT,
    retail         REAL,
    cost           REAL,
    ext_cost       REAL,               -- as printed on the PO, never silently "corrected"
    ctns           INTEGER,
    cspk           INTEGER,
    ext_qty        INTEGER,
    cube           REAL,
    kilograms      REAL,
    total_cost     REAL,               -- PO-level printed total, repeated on each row
    flags          TEXT,               -- NULL = clean; otherwise what looked wrong (see Flags sheet)
    PRIMARY KEY (po_number, sku)
);
