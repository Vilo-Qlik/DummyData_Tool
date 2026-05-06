# Qlik Dummy Data Generator — POC v0.2

A tool for Qlik Implementation Consultants. Takes a customer's load script,
generates synthetic data that satisfies the script's structure and filters,
and produces a patched copy of the script you can reload directly — without
ever touching the customer's real data.

## What's new in v0.2

- **Cloud API input mode**: pull the script directly from a Qlik Cloud app
  using `--tenant` + `--app-id` (no more manual "File → Export script").
- **Qlik Cloud lib:// path syntax**: `lib://Space:DataFiles/Subfolder/file.csv`
- **`--space` and `--subfolder`** flags for per-customer organization
- **`--csv-format`** flag: `comma` (with header) or `semicolon` (no header)

## Quick start

```bash
pip install faker
```

### Mode A — local .qvs file

```bash
python qlik_data_generator.py \
    --script-file path/to/CustomerScript.qvs \
    --out ./generated \
    --space "Customer App Dev" \
    --subfolder "AcmeCorp_Dummy" \
    --csv-format semicolon \
    --default-rows 1000
```

### Mode B — pull straight from Qlik Cloud

```bash
export QLIK_API_KEY=eyJhbGciOi...    # your Qlik Cloud API key
python qlik_data_generator.py \
    --tenant my-tenant.us.qlikcloud.com \
    --app-id 12345678-aaaa-bbbb-cccc-1234567890ab \
    --out ./generated \
    --space "Customer App Dev" \
    --subfolder "AcmeCorp_Dummy" \
    --csv-format semicolon \
    --default-rows 1000
```

The tool fetches the latest script version, saves it to `<out>/<AppName>.qvs`,
then runs the same generation pipeline as Mode A. Use `--api-key` instead of
the env var if you prefer (but env var keeps it out of shell history).

## How to find an app ID

In Qlik Cloud, open the app — the GUID in the URL after `/app/` is the app ID.
Or via Qlik CLI: `qlik app ls`.

## How to get an API key

In Qlik Cloud: profile menu → Settings → API keys → Generate new key. Make sure
your tenant has API key generation enabled (admin setting).

## Output layout

```
generated/
├── data/
│   ├── Primary_Fact_Table.csv           <- synthetic data, one CSV per source
│   └── Reg_Class_Fact_Table.csv
├── <ScriptName>_PATCHED.qvs             <- the script with FROM lines rewritten
├── <AppName>.qvs                        <- (Cloud mode only) fetched original
├── _DummyData_Include.qvs               <- alternative include-based redirection
└── row_counts.json                      <- edit and re-run with --config
```

## How to reload in Qlik

1. **Upload** the CSVs from `generated/data/` to your space's DataFiles under
   the subfolder you specified. (In the space: Add new → Data file → upload
   each CSV; create the subfolder if needed.)
2. **Open** the patched script in your Qlik app (or paste it into a new app
   in the same space).
3. **Reload**.

## Tweaking row counts

After the first run, edit `generated/row_counts.json` and re-run with
`--config generated/row_counts.json`.

## What it gets right

- Comment-tolerant parsing including `//` inside `[lib://...]` paths
- Aliasing (`PrimaryCaseID as CaseID`), bracketed identifiers (`[U.S. Wealth]`)
- Multiple LOAD blocks against the same source share one CSV (associations work)
- WHERE-clause awareness:
  - `Year(dt_X) = year(today())` → dates land in current year
  - `FieldX = 'literal'` → literal appears with high frequency in generated rows
  - `EXISTS(FieldX)` → child samples that field from parent (referential integrity)
- Fields used in WHERE but not in LOAD list still get added to source CSV
- Type inference from Qlik naming conventions (dt_, ts_, *ID, *Name, *Notes, etc.)
- Cross-source key alignment for `*ID`/`*Code` fields shared across sources

## Known limitations (v3 candidates)

- **No QVF binary parsing** — Cloud mode covers most cases, but a QVF that
  hasn't been imported anywhere needs to be uploaded first.
- **Negative filters not semantic.** `WHERE NOT (...)` treated as positive hints.
  Usually fine in practice.
- **No INLINE / RESIDENT / MAPPING / JOIN / CONCATENATE support yet.**
- **No SQL SELECT / REST connector parsing.** Only QVD/file sources.
- **No QVD output yet** — only CSV.
- **Heuristic type inference.** Claude API path is the obvious upgrade.

## Privacy note

- File mode: everything runs locally; nothing leaves your machine.
- Cloud mode: the only network traffic is GET requests to your tenant's
  documented Qlik Cloud REST API to fetch the script text. The script is
  saved locally and processed locally from there. No customer script
  content is ever sent to any third party.
