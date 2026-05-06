# Qlik Dummy Data Generator — v0.4

Parses a Qlik load script, infers field types, generates synthetic CSV data, and produces a patched `.qvs` that reloads immediately — no QVDs, databases, or live connections required.

---

## Navigation

- [Quick start](#quick-start)
- [CLI reference](#cli-reference)
- [How it works](#how-it-works)
- [Qlik Cloud paths](#qlik-cloud-paths)
- [Reload workflow](#reload-workflow)
- [Output files](#output-files)
- [Supported constructs](#supported-constructs)
- [Known limitations](#known-limitations)
- [Changelog](#changelog)
- [Roadmap](#roadmap)

---

## Requirements

- Python 3.8+
- `faker` — `pip install faker`

No other dependencies. All HTTP calls use stdlib `urllib`.

---

## Quick start

### From a local .qvs file

```bash
python qlik_data_generator.py \
    --script-file CaseDetailsScript.qvs \
    --space "Customer App Dev" \
    --subfolder "AcmeCorp_Dummy" \
    --csv-format comma \
    --default-rows 500
```

### Fetch directly from Qlik Cloud (no manual script export)

```bash
export QLIK_API_KEY=eyJhbGc...

python qlik_data_generator.py \
    --tenant my-tenant.us.qlikcloud.com \
    --app-id 12345678-aaaa-bbbb-cccc-1234567890ab \
    --space "Customer App Dev" \
    --subfolder "AcmeCorp_Dummy" \
    --default-rows 500
```

### Re-run with custom row counts

```bash
python qlik_data_generator.py \
    --script-file MyScript.qvs \
    --config ./qlik_dummy_out/row_counts.json \
    --space "Customer App Dev" \
    --subfolder "AcmeCorp_Dummy"
```

> `--script-file` and `--tenant`/`--app-id` are mutually exclusive.

[↑ Back to top](#navigation)

---

## CLI reference

### Script input — choose one

| Argument | Description |
|---|---|
| `--script-file <path>` | Path to a local `.qvs` script. |
| `--tenant <url>` | Qlik Cloud tenant, e.g. `my-tenant.us.qlikcloud.com`. Use with `--app-id`. |
| `--app-id <guid>` | Qlik Cloud app GUID (from the app URL). Use with `--tenant`. |
| `--api-key <key>` | API key. Defaults to `$QLIK_API_KEY` env var. Prefer the env var to keep the key out of shell history. |

### Output options

| Argument | Default | Description |
|---|---|---|
| `--space <name>` | none | Qlik Cloud space name. Omit for personal space. |
| `--subfolder <name>` | `DummyData` | Subfolder under DataFiles. Use a per-customer name (e.g. `AcmeCorp_Dummy`) to avoid collisions. |
| `--csv-format` | `comma` | `comma` or `semicolon` — affects both the CSV delimiter and the FROM clause in the patched script. |
| `--default-rows <n>` | `1000` | Rows to generate per source table. |
| `--config <path>` | none | Path to a `row_counts.json` for per-source row counts. |
| `--out <dir>` | `./qlik_dummy_out` | Output directory for all generated files. |

### CSV format

Both options write a **header row** (`embedded labels`). This is required so `WHERE` clauses can reference fields not explicitly named in the `LOAD` list — matching QVD load behavior.

| `--csv-format` | Delimiter | FROM clause |
|---|---|---|
| `comma` (default) | `,` | `(txt, utf8, embedded labels, delimiter is ',', msq)` |
| `semicolon` | `;` | `(txt, utf8, embedded labels, delimiter is ';', msq)` |

> **Note:** v0.2 used `no labels` for semicolon format, which caused "Field not found" reload errors. Both formats now always use `embedded labels` as of v0.3.

[↑ Back to top](#navigation)

---

## How it works

### 1. Statement classification

Each `;`-delimited statement is classified. Only external blocks generate dummy data.

| Kind | Description | Generates data? |
|---|---|---|
| `external_file` | `LOAD ... FROM [lib://...] (qvd\|ooxml\|csv\|...)` | ✅ Yes |
| `external_sql` | `SELECT ... FROM db.table` via `LIB CONNECT TO` | ✅ Yes |
| `internal_resident` | `LOAD ... RESIDENT <table>` | ❌ No |
| `internal_concat` | `CONCATENATE (X) LOAD ... RESIDENT` | ❌ No |
| `internal_inline` | `LOAD * INLINE [...]` | ❌ No |
| `internal_mapping` | `MAPPING LOAD` (any source) | ❌ No |

### 2. Source merging

Multiple LOAD or SELECT blocks reading the same source are merged into one CSV with the union of all fields, so cross-block associations remain consistent and join-ready.

### 3. Type inference

| Pattern | Type | Example |
|---|---|---|
| `dt_` prefix | date | `dt_ComplaintDate` |
| `ts_` prefix | duration_ms | `ts_LoadTime` |
| `*Date` suffix | date | `ClosedDate` |
| `*DateTime`, `*Timestamp` | datetime | `CreatedDatetime` |
| `*ID`, `*Code` | id_string | `CaseID`, `ItemCode` |
| `*Counter`, `*Count` | integer | `ViolationCounter` |
| `*Name` | person_name | `AssigneeName` |
| `*Notes`, `*Description`, `*Reason` | long_text | `CaseNotes` |
| `*Flag`, `*Indicator` | yn_flag (Y/N) | `ControllableFlag` |
| `*State`, `*Region`, `*Site` | place_name | `ComplaintState` |
| `*Vendor`, `*Supplier` | company_name | `VendorName` |
| `*ZipCode`, `*Zip` | zipcode | `OfficeZip` |
| *(everything else)* | category_string | `Cat_01` – `Cat_08` |

### 4. Filter awareness

WHERE clauses are analyzed and applied to generated data:

- `= 'LiteralValue'` — field contains that literal ~70% of the time
- `Year(FieldName) = year(today())` — all values are in the current year
- `EXISTS(ChildField)` — child rows sample key values from the parent (100% referential integrity)
- Fields in WHERE but not in LOAD list — automatically added to the CSV

### 5. Patched script output

Every external source clause is rewritten:

- `FROM [lib://Space/File.QVD] (qvd)` → CSV path with embedded labels
- `SELECT ... FROM db.schema.table` → `LOAD * FROM [lib://...csv]`
- `LIB CONNECT TO 'Connection'` → commented out with `// [PATCHED]`

RESIDENT, CONCATENATE, MAPPING, and INLINE blocks are preserved unchanged.

[↑ Back to top](#navigation)

---

## Qlik Cloud paths

The tool generates FROM clauses in the correct format:

```
lib://<Space>:DataFiles/<Subfolder>/<File>.csv
```

Example with `--space "Customer App Dev" --subfolder "AcmeCorp_Dummy"`:

```
FROM [lib://Customer App Dev:DataFiles/AcmeCorp_Dummy/Primary_Fact_Table.csv]
(txt, utf8, embedded labels, delimiter is ',', msq)
```

**Personal space** (omit `--space`):

```
FROM [lib://DataFiles/AcmeCorp_Dummy/Primary_Fact_Table.csv]
(txt, utf8, embedded labels, delimiter is ',', msq)
```

**On-premises Qlik Sense Enterprise:** omit `--space` — same personal-space format with your configured folder data connection.

[↑ Back to top](#navigation)

---

## Reload workflow

1. **Run the tool.** The `NEXT STEPS — UPLOAD THESE FILES` block at the end of output shows the exact upload path and filenames.
2. **Upload CSVs** to `DataFiles/<Subfolder>/` in the target space. No data connection setup needed.
3. **Open a new Qlik Cloud app** in the same space.
4. **Paste or `$(Must_Include=...)`** the `_PATCHED.qvs` into the script editor.
5. **Reload.**

### After reload — what to check

- **Row counts** per table (WHERE filters reduce counts — e.g. `ItemTypeOriginal = 'Complaint'` keeps ~70%)
- **Associations** — click a shared field (e.g. `CaseID`) to confirm it bridges the expected tables
- **Synthetic keys** — a `$Syn` table in the viewer means multiple fields are shared between tables
- **Date fields** — drag a `dt_*` field onto a chart to confirm it renders as a date, not a string

> **Phase 1 scope:** The goal is a clean reload with no errors. Computed columns may show NULLs due to type inference misses — this is expected and will be addressed in a future release via per-field type overrides.

[↑ Back to top](#navigation)

---

## Output files

All outputs go to `--out` (default `./qlik_dummy_out/`).

| File | Description |
|---|---|
| `data/<source>.csv` | One CSV per external source. Upload these to Qlik Cloud DataFiles. |
| `<ScriptName>_PATCHED.qvs` | Original script with external FROM and SELECT clauses rewritten. Use this for the reload. |
| `row_counts.json` | Row counts per source. Edit and re-run with `--config` to tune volumes. |
| `_DummyData_Include.qvs` | Helper include file with variable mappings (informational). |

### Row count config format

```json
{
  "row_counts": {
    "lib://NAS/Primary Fact Table.QVD": 2000,
    "sql:prod.schema.claims": 5000
  }
}
```

[↑ Back to top](#navigation)

---

## Supported constructs

| Construct | Status |
|---|---|
| QVD file loads | ✅ |
| SQL SELECT via LIB CONNECT TO | ✅ v0.4 |
| Excel / ooxml file loads | ✅ v0.4 |
| CSV / txt file loads | ✅ |
| RESIDENT loads | ✅ Preserved unchanged |
| CONCATENATE (X) LOAD | ✅ Preserved unchanged |
| LEFT / RIGHT / INNER / OUTER JOIN (X) LOAD | ✅ v0.4 |
| KEEP (X) LOAD | ✅ v0.4 |
| MAPPING LOAD (inline / resident / external) | ✅ v0.4 |
| LOAD * INLINE [...] | ✅ Preserved unchanged |
| Preceding LOAD chains | ✅ Chained correctly to source |
| LIB CONNECT TO statements | ✅ Commented out in patched script |
| DROP TABLE / DROP FIELDS | ✅ Detected (informational) |
| Sub ... End Sub blocks | ✅ Stripped before parsing |
| // and /* */ comments | ✅ |
| Bracketed [field names] | ✅ |
| Double-quoted "field names" | ✅ v0.4 |
| Backtick `field names` | ✅ v0.4 |
| SET / LET / TRACE / QUALIFY | ✅ Skipped |
| FOR / IF / CALL control flow | ✅ Skipped |
| AUTOGENERATE | ✅ Treated as internal |
| $(Include=...) / $(Must_Include=...) | ⚠️ Not followed — included file not parsed |
| NOT (...) / WildMatch negation in WHERE | ⚠️ Treated as positive hint — reload works, negation not enforced |

[↑ Back to top](#navigation)

---

## Known limitations

**Type inference misses** — unrecognizable field names fall back to `Cat_01`–`Cat_08`. If downstream expressions like `If(IsNum(field), ...)` or `Date#(field, ...)` depend on the correct type, computed columns may be NULL.

**Computed fields** — expressions like `Num(Left(OrderID, 4)) as OrderYear` scan for identifiable source field names but don't evaluate the expression itself.

**$(Include=...) not followed** — variables and table definitions from included scripts aren't visible during parsing.

**Referential integrity for non-EXISTS joins** — 100% integrity is enforced for explicit `EXISTS()` constraints and ~70% for matching `id_string` field names across tables. Other joins may have low match rates.

**NOT / WildMatch negation** — negated WHERE filters don't constrain generated data. Reload succeeds but excluded combinations may still appear.

**Period / batch ID date fields** — fields like `reporting_accounting_period` or `v_batch_id` (used as YYYY-MM) aren't recognized as dates. Downstream `Date#()` or `MakeDate()` calls will produce NULLs.

[↑ Back to top](#navigation)

---

## Changelog

### v0.4 (current)
- SQL `SELECT` support — rewrites `SELECT ... FROM db.table` → `LOAD * FROM csv`
- Automatic `LIB CONNECT TO` commenting (fixes "Connector not found" reload errors)
- `LEFT / RIGHT / INNER / OUTER JOIN (X)` and `KEEP (X)` prefix handling
- `MAPPING LOAD` classification (inline, resident, and external file variants)
- Double-quoted and backtick field name/alias support
- Full statement-walking parser — distinguishes `external_*` from `internal_*` blocks
- Multiple SELECTs against the same SQL table merge into one CSV

### v0.3
- Fixed "Field not found" errors — `no labels` → `embedded labels` for all CSV formats
- Both comma and semicolon formats now always write a header row

### v0.2
- Qlik Cloud REST API mode (`--tenant` + `--app-id`)
- `--space` with correct `lib://Space:DataFiles/Subfolder/` path syntax
- `--subfolder`, `--csv-format` flags
- `NEXT STEPS — UPLOAD THESE FILES` block in run output

### v0.1
- Initial POC: QVD parsing, type inference, filter awareness, EXISTS() referential integrity, FROM rewriting
- Validated with clean end-to-end reload in Qlik Cloud

[↑ Back to top](#navigation)

---

## Roadmap

- **Per-field type override UI** — schema preview with dropdowns to correct inferred types before generation
- **Custom value lists** — paste in specific values for `match()` / `if()` fields so those branches exercise real logic
- **Local web UI** — FastAPI + browser frontend with drag-and-drop upload, per-table row sliders, download bundle
- **QVD output** — generate QVDs directly for faster reloads at high volumes
- **$(Include=...) resolution** — optionally parse included script files
- **Qlik Cloud app browser** — pick apps from a space dropdown, auto-upload CSVs, push patched script back

[↑ Back to top](#navigation)
