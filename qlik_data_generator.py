"""
Qlik Dummy Data Generator — POC v0.1

Parses a Qlik load script, infers field types, and generates synthetic
CSV data plus an include script so the original .qvs can be reloaded
without access to the real source QVDs / databases.

Usage:
    python qlik_data_generator.py <script.qvs> [--config config.json] [--out output_dir]

If --config is omitted, a default of 1000 rows per source is used and
a config template is written to <output_dir>/row_counts.json so you
can re-run with custom counts.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import string
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from faker import Faker
except ImportError:
    print("ERROR: faker is required. Install with: pip install faker", file=sys.stderr)
    sys.exit(1)

import json as _json
import os
import urllib.error
import urllib.parse
import urllib.request


# ---------------------------------------------------------------------------
# Qlik Cloud API client (stdlib-only, no extra deps)
# ---------------------------------------------------------------------------

class QlikCloudError(Exception):
    """Raised for any failure talking to the Qlik Cloud REST API."""


def _normalize_tenant_url(tenant: str) -> str:
    """Accept 'tenant.us.qlikcloud.com', 'https://tenant.us.qlikcloud.com',
    'https://tenant.us.qlikcloud.com/', or even a full URL with trailing path,
    and return a clean 'https://<host>' base."""
    t = tenant.strip()
    if not t:
        raise QlikCloudError("Tenant URL is empty.")
    if not t.startswith(("http://", "https://")):
        t = "https://" + t
    parsed = urllib.parse.urlparse(t)
    if not parsed.netloc:
        raise QlikCloudError(f"Could not parse tenant URL: {tenant!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _api_get(base_url: str, path: str, api_key: str, timeout: int = 30) -> Any:
    """GET <base_url><path> with bearer auth. Returns parsed JSON, or
    raw text if the response isn't JSON. Raises QlikCloudError on failure."""
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            body_bytes = resp.read()
            text = body_bytes.decode("utf-8", errors="replace")
            if "application/json" in content_type:
                return _json.loads(text)
            return text
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        if e.code == 401:
            raise QlikCloudError(
                "401 Unauthorized — check that the API key is valid and not expired."
            ) from e
        if e.code == 403:
            raise QlikCloudError(
                "403 Forbidden — the API key user does not have access to this app."
            ) from e
        if e.code == 404:
            raise QlikCloudError(
                f"404 Not Found — {url}\n"
                f"Verify the tenant URL and app ID. Detail: {err_body[:200]}"
            ) from e
        raise QlikCloudError(
            f"HTTP {e.code} from {url}\nDetail: {err_body[:500]}"
        ) from e
    except urllib.error.URLError as e:
        raise QlikCloudError(
            f"Connection error reaching {url}: {e.reason}"
        ) from e
    except TimeoutError as e:
        raise QlikCloudError(f"Timeout reaching {url}") from e


def fetch_script_from_cloud(
    tenant: str,
    app_id: str,
    api_key: str,
    out_dir: Path,
) -> Path:
    """Download the current script for the given Qlik Cloud app and write
    it to <out_dir>/<safe_app_name>.qvs. Returns the saved path.

    Uses two documented endpoints:
      GET /api/v1/apps/{appId}                  -> app metadata (for filename)
      GET /api/v1/apps/{appId}/scripts          -> version list, latest first
      GET /api/v1/apps/{appId}/scripts/{id}     -> script text for that version
    """
    base = _normalize_tenant_url(tenant)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Fetch app metadata (so we can name the local .qvs file sensibly)
    app_info = _api_get(base, f"/api/v1/apps/{app_id}", api_key)
    app_name = (
        (app_info.get("attributes") or {}).get("name")
        if isinstance(app_info, dict)
        else None
    ) or f"app_{app_id}"
    safe_app_name = re.sub(r"[^A-Za-z0-9_-]+", "_", app_name).strip("_") or "fetched_app"

    # 2. Fetch script version list (latest first)
    versions = _api_get(base, f"/api/v1/apps/{app_id}/scripts", api_key)
    if isinstance(versions, dict):
        # Some endpoints wrap arrays in {"data": [...]} — handle either shape
        versions_list = versions.get("data") or versions.get("scripts") or []
    elif isinstance(versions, list):
        versions_list = versions
    else:
        versions_list = []
    if not versions_list:
        raise QlikCloudError(
            f"No script versions found for app {app_id}. The app may have an empty script."
        )
    latest = versions_list[0]
    version_id = latest.get("scriptId") or latest.get("id")
    if not version_id:
        raise QlikCloudError(
            f"Could not determine latest script version id from response: {latest!r}"
        )

    # 3. Fetch the actual script text. Endpoint may return JSON {script: "..."}
    #    or plain text — handle both.
    script_resp = _api_get(base, f"/api/v1/apps/{app_id}/scripts/{version_id}", api_key)
    if isinstance(script_resp, dict):
        script_text = script_resp.get("script") or script_resp.get("text") or ""
    else:
        script_text = script_resp or ""
    if not script_text.strip():
        raise QlikCloudError(
            f"Fetched script for app {app_id} is empty."
        )

    saved_path = out_dir / f"{safe_app_name}.qvs"
    saved_path.write_text(script_text, encoding="utf-8")
    return saved_path

fake = Faker()
Faker.seed(42)
random.seed(42)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Field:
    source_name: str          # name as it appears in the source (before "as")
    alias: str | None = None  # name after "as", if aliased
    inferred_type: str = "string"
    notes: str = ""

    @property
    def output_name(self) -> str:
        return self.alias or self.source_name


# Block-kind constants — only external_* need dummy data; internal_* and preceding skip.
BLOCK_EXTERNAL_FILE = "external_file"          # LOAD ... FROM <path> (qvd|ooxml|txt|...)
BLOCK_EXTERNAL_SQL = "external_sql"            # SELECT ... FROM <db.table>
BLOCK_INTERNAL_RESIDENT = "internal_resident"  # LOAD ... RESIDENT <table>
BLOCK_INTERNAL_CONCAT = "internal_concat"      # CONCATENATE (X) LOAD ... RESIDENT — same parent
BLOCK_INTERNAL_INLINE = "internal_inline"      # LOAD * INLINE [...]
BLOCK_INTERNAL_MAPPING = "internal_mapping"    # Mapping Load (inline / resident)
BLOCK_PRECEDING = "preceding"                  # LOAD with no source — chained to next
BLOCK_UNKNOWN = "unknown"


@dataclass
class LoadBlock:
    table_name: str
    block_kind: str = BLOCK_UNKNOWN
    source_path: str | None = None     # for external_file
    source_format: str | None = None   # qvd, txt, ooxml...
    sql_table: str | None = None       # for external_sql, e.g. db.schema.table
    sql_select_text: str | None = None # raw SELECT statement, for in-place replacement
    fields: list[Field] = field(default_factory=list)
    where_clause: str | None = None
    raw_text: str = ""
    concatenate_target: str | None = None  # if this is a CONCATENATE (X) LOAD

    @property
    def source_key(self) -> str:
        """Identifier used to group blocks that share a source."""
        if self.block_kind == BLOCK_EXTERNAL_FILE and self.source_path:
            return self.source_path
        if self.block_kind == BLOCK_EXTERNAL_SQL and self.sql_table:
            return f"sql:{self.sql_table}"
        return f"__internal__{self.table_name}"

    @property
    def is_external(self) -> bool:
        return self.block_kind in (BLOCK_EXTERNAL_FILE, BLOCK_EXTERNAL_SQL)


@dataclass
class ScriptModel:
    load_blocks: list[LoadBlock] = field(default_factory=list)
    dropped_fields: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Step 1: Strip comments
# ---------------------------------------------------------------------------

def strip_comments(text: str) -> str:
    """Remove // line comments and /* block comments */ but preserve string
    literals AND bracketed identifiers (e.g. [lib://NAS_DATA/.../File.QVD])."""
    out = []
    i = 0
    n = len(text)
    bracket_depth = 0
    while i < n:
        ch = text[i]

        # String literal — copy verbatim until closing quote
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            while i < n and text[i] != quote:
                out.append(text[i])
                i += 1
            if i < n:
                out.append(text[i])
                i += 1
            continue

        # Track bracketed identifier depth — // inside [...] is part of a path, not a comment
        if ch == "[":
            bracket_depth += 1
            out.append(ch)
            i += 1
            continue
        if ch == "]":
            bracket_depth = max(0, bracket_depth - 1)
            out.append(ch)
            i += 1
            continue

        # // line comment (only when not inside brackets)
        if bracket_depth == 0 and ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue

        # /* block comment */ (also only when not inside brackets)
        if bracket_depth == 0 and ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue

        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Step 2: Parse LOAD blocks
# ---------------------------------------------------------------------------

# Matches "TableName:" on a line by itself (loose)
TABLE_LABEL_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*$", re.MULTILINE)
LOAD_RE = re.compile(r"\bLOAD\b", re.IGNORECASE)
FROM_RE = re.compile(r"\bFROM\b", re.IGNORECASE)
WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)
DROP_FIELDS_RE = re.compile(r"\bDrop\s+Fields?\b\s+([^;]+);", re.IGNORECASE)


def split_top_level_commas(text: str) -> list[str]:
    """Split on commas not inside () or []."""
    parts = []
    depth_paren = 0
    depth_bracket = 0
    current = []
    in_str = None
    for ch in text:
        if in_str:
            current.append(ch)
            if ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"'):
            in_str = ch
            current.append(ch)
            continue
        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren -= 1
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]":
            depth_bracket -= 1

        if ch == "," and depth_paren == 0 and depth_bracket == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def parse_field_expression(expr: str) -> Field | None:
    """
    Parse one item from a LOAD or SELECT field list.
    Handles: Foo,  Foo as Bar,  [Foo Bar] as Baz,  "Foo Bar" as Baz,
             `db_field` as Bar,  Date(Foo) as Bar
    Returns None for things that look like garbage.
    """
    expr = expr.strip().rstrip(",").strip()
    if not expr:
        return None

    # " as <name>" at end — alias may be bare, [bracketed], "double-quoted", or `backticked`
    as_match = re.search(
        r'\s+as\s+(\[[^\]]+\]|"[^"]+"|`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)\s*$',
        expr,
        re.IGNORECASE,
    )
    alias = None
    if as_match:
        alias = as_match.group(1).strip('[]"`').strip()
        source_part = expr[: as_match.start()].strip()
    else:
        source_part = expr

    bare = source_part
    # Strip wrapping brackets/quotes/backticks if the entire source is one of those forms
    if bare.startswith("[") and bare.endswith("]"):
        bare = bare[1:-1].strip()
    elif bare.startswith('"') and bare.endswith('"') and bare.count('"') == 2:
        bare = bare[1:-1].strip()
    elif bare.startswith("`") and bare.endswith("`") and bare.count("`") == 2:
        bare = bare[1:-1].strip()

    is_expression = bool(re.search(r"[(\)\+\*/]", bare))
    return Field(
        source_name=bare,
        alias=alias,
        inferred_type="expression" if is_expression else "string",
        notes="expression" if is_expression else "",
    )


def strip_subroutines(text: str) -> str:
    """Remove `Sub <name>(...) ... End Sub` blocks. We can't safely flatten
    them (parameter substitution is non-trivial), so we ignore their contents
    rather than misparsing the LOADs inside."""
    out = []
    pos = 0
    n = len(text)
    sub_open = re.compile(r"\bSub\s+[A-Za-z_][A-Za-z0-9_]*", re.IGNORECASE)
    sub_close = re.compile(r"\bEnd\s+Sub\b", re.IGNORECASE)
    while pos < n:
        m = sub_open.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        out.append(text[pos:m.start()])
        end_m = sub_close.search(text, m.end())
        if not end_m:
            # Unterminated — drop the rest to avoid garbage
            break
        pos = end_m.end()
    return "".join(out)


def split_top_level_statements(text: str) -> list[str]:
    """Split on `;` at top level — respect string literals, [bracketed],
    `backticked`, and (parens). Empty statements are dropped."""
    out = []
    current = []
    depth_paren = 0
    depth_bracket = 0
    in_str = None  # ' or " or `
    for ch in text:
        if in_str:
            current.append(ch)
            if ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            current.append(ch)
            continue
        if ch == "[":
            depth_bracket += 1
        elif ch == "]":
            depth_bracket -= 1
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren -= 1
        if ch == ";" and depth_paren == 0 and depth_bracket == 0:
            stmt = "".join(current).strip()
            if stmt:
                out.append(stmt)
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        out.append(tail)
    return out


# Regex helpers for statement classification
SELECT_KW_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
LOAD_KW_RE = re.compile(r"^\s*LOAD\b", re.IGNORECASE)
MAPPING_LOAD_RE = re.compile(r"^\s*MAPPING\s+LOAD\b", re.IGNORECASE)
# Concatenate / Join / Keep — all take (TableName) and apply to a following LOAD
JOIN_PREFIX_RE = re.compile(
    r"^\s*(?:CONCATENATE|"
    r"(?:LEFT|RIGHT|INNER|OUTER)?\s*(?:JOIN|KEEP)|"
    r"NOCONCATENATE)\s*\(\s*\[?([A-Za-z_][A-Za-z0-9_ ]*)\]?\s*\)\s*",
    re.IGNORECASE,
)
RESIDENT_RE = re.compile(r"\bRESIDENT\b", re.IGNORECASE)
INLINE_RE = re.compile(r"\bINLINE\b", re.IGNORECASE)
TABLE_LABEL_INLINE_RE = re.compile(
    r"^\s*(\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)\s*:\s*", re.MULTILINE
)
SET_LET_RE = re.compile(r"^\s*(SET|LET|TRACE|UNQUALIFY|QUALIFY|REM)\b", re.IGNORECASE)
DROP_RE = re.compile(r"^\s*DROP\s+(TABLE|FIELD)S?\b", re.IGNORECASE)
LIB_CONNECT_RE = re.compile(r"^\s*LIB\s+CONNECT\s+TO\b", re.IGNORECASE)
SUB_CALL_RE = re.compile(r"^\s*(CALL|SUB|END\s+SUB|EXIT\s+SCRIPT|IF|END\s+IF|FOR|END\s+FOR|NEXT|DO|LOOP|SWITCH|END\s+SWITCH)\b", re.IGNORECASE)


def _strip_label_prefix(stmt: str) -> tuple[str | None, str]:
    """If the statement starts with `Label:` (optionally bracketed), return
    (label, rest). Otherwise (None, original)."""
    m = TABLE_LABEL_INLINE_RE.match(stmt)
    if m:
        label = m.group(1).strip("[]").strip()
        return label, stmt[m.end():]
    return None, stmt


def _parse_load_body(body: str) -> tuple[list[Field], str | None, str | None, str | None, str | None]:
    """Given the text of a LOAD body (everything after the LOAD keyword), return
    (fields, source_path, source_format, where_clause, resident_table).
    The body may also contain INLINE [...] or RESIDENT <table> markers."""
    text = body

    # Handle Mapping Load * INLINE [...]
    inline_m = re.search(r"\bINLINE\b\s*\[", text, re.IGNORECASE)
    if inline_m:
        # Field list is what's before INLINE keyword (e.g. "*" or explicit fields)
        # The data inside [...] is literal — we don't generate dummy data for it.
        field_section = text[:inline_m.start()]
        # Strip optional leading "distinct" or "*"
        field_section = re.sub(r"^\s*(distinct|\*)\s*,?\s*", "", field_section, flags=re.IGNORECASE)
        field_items = split_top_level_commas(field_section) if field_section.strip() else []
        fields = []
        for item in field_items:
            f = parse_field_expression(item)
            if f and f.source_name:
                fields.append(f)
        return fields, None, "inline", None, None

    # RESIDENT?
    resident_m = RESIDENT_RE.search(text)
    if resident_m:
        field_section = text[:resident_m.start()]
        after_resident = text[resident_m.end():]
        # Resident table name: bracketed or unquoted identifier
        rt_m = re.match(r"\s*(\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)", after_resident)
        resident_table = rt_m.group(1).strip("[]") if rt_m else None
        rest_after_table = after_resident[rt_m.end():] if rt_m else after_resident
        # WHERE may follow
        where_m = WHERE_RE.search(rest_after_table)
        where_text = rest_after_table[where_m.end():].strip() if where_m else None
        # Strip GROUP BY from WHERE if present
        if where_text:
            gb_m = re.search(r"\bGROUP\s+BY\b", where_text, re.IGNORECASE)
            if gb_m:
                where_text = where_text[:gb_m.start()].strip()
        # Strip leading "distinct" / "*"
        field_section = re.sub(r"^\s*(distinct)\s+", "", field_section, flags=re.IGNORECASE)
        field_items = split_top_level_commas(field_section) if field_section.strip() else []
        fields = []
        for item in field_items:
            f = parse_field_expression(item)
            if f and f.source_name:
                fields.append(f)
        return fields, None, "resident", where_text, resident_table

    # FROM <path> (format)?
    from_m = FROM_RE.search(text)
    if from_m:
        field_section = text[:from_m.start()]
        rest = text[from_m.end():]
        field_section = re.sub(r"^\s*(distinct)\s+", "", field_section, flags=re.IGNORECASE)
        field_items = split_top_level_commas(field_section) if field_section.strip() else []
        fields = []
        for item in field_items:
            f = parse_field_expression(item)
            if f and f.source_name:
                fields.append(f)

        # Extract source path
        source_path = None
        source_format = None
        m = re.match(r"\s*(\[[^\]]+\]|'[^']+'|\S+)", rest)
        if m:
            source_path = m.group(1).strip("[]'\"")
            after_source = rest[m.end():]
            fmt_m = re.match(r"\s*\(([^)]+)\)", after_source)
            if fmt_m:
                source_format = fmt_m.group(1).strip().lower().split(",")[0].strip()

        # WHERE may follow
        where_m = WHERE_RE.search(rest)
        where_text = rest[where_m.end():].strip() if where_m else None
        return fields, source_path, source_format, where_text, None

    # No FROM, no RESIDENT, no INLINE — preceding LOAD or AUTOGENERATE
    autogen_m = re.search(r"\bAUTOGENERATE\b", text, re.IGNORECASE)
    if autogen_m:
        field_section = text[:autogen_m.start()]
    else:
        field_section = text
    field_section = re.sub(r"^\s*(distinct|\*)\s*,?\s*", "", field_section, flags=re.IGNORECASE)
    field_items = split_top_level_commas(field_section) if field_section.strip() else []
    fields = []
    for item in field_items:
        f = parse_field_expression(item)
        if f and f.source_name:
            fields.append(f)
    fmt = "autogenerate" if autogen_m else None
    return fields, None, fmt, None, None


def _parse_select_body(body: str) -> tuple[list[Field], str | None, str | None]:
    """Parse `<field list> FROM <table> [WHERE ...]`. Returns
    (fields, sql_table, where_clause)."""
    from_m = FROM_RE.search(body)
    if not from_m:
        return [], None, None
    field_section = body[:from_m.start()]
    rest = body[from_m.end():]

    # Field list — items may be backticked, with table prefixes, etc.
    field_items = split_top_level_commas(field_section)
    fields = []
    for item in field_items:
        f = parse_field_expression(item)
        if f and f.source_name:
            fields.append(f)

    # Table name — for SQL, usually `db`.`schema`.`table` or unquoted
    table_m = re.match(r"\s*([`\w.\"\-]+(?:\s*\.\s*[`\w.\"\-]+)*)", rest)
    sql_table = table_m.group(1).strip() if table_m else None
    if sql_table:
        # Normalize: strip backticks for the key, but keep dots
        sql_table = re.sub(r"[`\"]", "", sql_table)

    # WHERE
    where_m = WHERE_RE.search(rest)
    where_text = rest[where_m.end():].strip() if where_m else None
    return fields, sql_table, where_text


def parse_script(text: str) -> ScriptModel:
    cleaned = strip_comments(text)
    cleaned = strip_subroutines(cleaned)

    model = ScriptModel()

    # Capture Drop Fields statements (informational)
    for m in DROP_FIELDS_RE.finditer(cleaned):
        for f in m.group(1).split(","):
            name = f.strip().strip("[]").strip(";").strip()
            if name:
                model.dropped_fields.append(name)

    statements = split_top_level_statements(cleaned)

    pending_chain: list[LoadBlock] = []  # accumulated preceding LOADs
    auto_counter = 0

    def make_table_name(label: str | None) -> str:
        nonlocal auto_counter
        if label:
            return label
        auto_counter += 1
        return f"AUTO_{auto_counter}"

    for stmt in statements:
        # Skip control-flow / metadata statements
        if SET_LET_RE.match(stmt) or DROP_RE.match(stmt) or LIB_CONNECT_RE.match(stmt) or SUB_CALL_RE.match(stmt):
            continue

        # Strip leading table label "Label:"
        label, body = _strip_label_prefix(stmt)

        # Strip leading CONCATENATE/JOIN/KEEP (TargetTable) prefix
        concat_target = None
        cm = JOIN_PREFIX_RE.match(body)
        if cm:
            concat_target = cm.group(1).strip()
            body = body[cm.end():]

        # Mapping Load?
        if MAPPING_LOAD_RE.match(body):
            mapping_body = re.sub(r"^\s*MAPPING\s+LOAD\b", "", body, count=1, flags=re.IGNORECASE)
            fields, source_path, source_format, where_text, resident_table = _parse_load_body(mapping_body)
            if source_path:
                # External mapping file (e.g. xlsx) — needs dummy data
                model.load_blocks.append(LoadBlock(
                    table_name=make_table_name(label),
                    block_kind=BLOCK_EXTERNAL_FILE,
                    source_path=source_path,
                    source_format=source_format,
                    fields=fields,
                    where_clause=where_text,
                    raw_text=stmt,
                ))
            else:
                # Inline / Resident mapping — no source to mock
                model.load_blocks.append(LoadBlock(
                    table_name=make_table_name(label),
                    block_kind=BLOCK_INTERNAL_MAPPING,
                    fields=fields,
                    raw_text=stmt,
                ))
            continue

        # SELECT?
        if SELECT_KW_RE.match(body):
            select_body = re.sub(r"^\s*SELECT\b", "", body, count=1, flags=re.IGNORECASE)
            fields, sql_table, where_text = _parse_select_body(select_body)
            block = LoadBlock(
                table_name=make_table_name(label or (pending_chain[-1].table_name if pending_chain else None)),
                block_kind=BLOCK_EXTERNAL_SQL,
                sql_table=sql_table,
                sql_select_text=stmt,
                fields=fields,
                where_clause=where_text,
                raw_text=stmt,
                concatenate_target=concat_target,
            )
            # If preceding LOADs are pending, merge their field references into this block's
            # field list so the dummy CSV will contain everything the chain needs.
            if pending_chain:
                # Use the first pending LOAD's table label if the SELECT didn't have one
                if pending_chain[0].table_name and not label:
                    block.table_name = pending_chain[0].table_name
                # The SELECT field list IS the source schema; preceding LOADs add computed fields
                # which derive from those, so we don't need to add anything here.
                pending_chain = []
            model.load_blocks.append(block)
            continue

        # LOAD?
        if LOAD_KW_RE.match(body):
            load_body = re.sub(r"^\s*LOAD\b", "", body, count=1, flags=re.IGNORECASE)
            fields, source_path, source_format, where_text, resident_table = _parse_load_body(load_body)

            if source_path:
                # External file source
                block = LoadBlock(
                    table_name=make_table_name(label or (pending_chain[-1].table_name if pending_chain else None)),
                    block_kind=BLOCK_EXTERNAL_FILE,
                    source_path=source_path,
                    source_format=source_format,
                    fields=fields,
                    where_clause=where_text,
                    raw_text=stmt,
                    concatenate_target=concat_target,
                )
                if pending_chain and not label:
                    block.table_name = pending_chain[0].table_name
                pending_chain = []
                model.load_blocks.append(block)
            elif source_format == "inline":
                model.load_blocks.append(LoadBlock(
                    table_name=make_table_name(label),
                    block_kind=BLOCK_INTERNAL_INLINE,
                    fields=fields,
                    raw_text=stmt,
                    concatenate_target=concat_target,
                ))
                pending_chain = []
            elif source_format == "resident" or resident_table:
                kind = BLOCK_INTERNAL_CONCAT if concat_target else BLOCK_INTERNAL_RESIDENT
                model.load_blocks.append(LoadBlock(
                    table_name=make_table_name(label or concat_target),
                    block_kind=kind,
                    fields=fields,
                    where_clause=where_text,
                    raw_text=stmt,
                    concatenate_target=concat_target,
                ))
                pending_chain = []
            elif source_format == "autogenerate":
                model.load_blocks.append(LoadBlock(
                    table_name=make_table_name(label),
                    block_kind=BLOCK_INTERNAL_INLINE,  # close enough — script-generated
                    fields=fields,
                    raw_text=stmt,
                ))
                pending_chain = []
            else:
                # No source, no resident, no inline → preceding LOAD; chain to next
                preceding = LoadBlock(
                    table_name=label or "",
                    block_kind=BLOCK_PRECEDING,
                    fields=fields,
                    raw_text=stmt,
                    concatenate_target=concat_target,
                )
                pending_chain.append(preceding)
            continue

        # Anything else — silently skip
        continue

    # Any leftover preceding chain that never found a source (rare) — record as unknown
    for p in pending_chain:
        model.load_blocks.append(p)

    return model


# ---------------------------------------------------------------------------
# Step 3: Type inference (heuristic, based on field name + filter context)
# ---------------------------------------------------------------------------

TYPE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^dt_", re.I), "date"),
    (re.compile(r"datetime$|datetimestamp$|datestamp$|timestamp$", re.I), "datetime"),
    (re.compile(r"date$", re.I), "date"),
    (re.compile(r"^ts_", re.I), "duration_ms"),
    (re.compile(r"counter$|count$", re.I), "integer"),
    (re.compile(r"^aging|days$|millisec$", re.I), "integer"),
    (re.compile(r"zipcode$|zip$", re.I), "zipcode"),
    (re.compile(r"id$|code$|^soeid", re.I), "id_string"),
    (re.compile(r"name$", re.I), "person_name"),
    (re.compile(r"notes$|description$|reason$", re.I), "long_text"),
    (re.compile(r"flag$|indicator$|controllable|alleged$", re.I), "yn_flag"),
    (re.compile(r"site$|region$|state$", re.I), "place_name"),
    (re.compile(r"vendor$|supplier", re.I), "company_name"),
    (re.compile(r"language", re.I), "language_code"),
]


def infer_type(field_name: str) -> str:
    for pat, t in TYPE_RULES:
        if pat.search(field_name):
            return t
    return "category_string"


def apply_type_inference(model: ScriptModel) -> None:
    for block in model.load_blocks:
        for f in block.fields:
            if f.inferred_type == "expression":
                continue
            f.inferred_type = infer_type(f.output_name)


# ---------------------------------------------------------------------------
# Step 4: Filter analysis  (so generated data passes the WHERE clauses)
# ---------------------------------------------------------------------------

@dataclass
class FilterHints:
    # Field name -> list of literal values that should appear with high frequency
    required_values: dict[str, list[str]] = field(default_factory=dict)
    # Field name -> True if value must be in the current year
    require_current_year: set[str] = field(default_factory=set)
    # (child_table_field, parent_field) — child must reference values from parent
    exists_constraints: list[tuple[str, str, str]] = field(default_factory=list)
    # field referenced in filter but maybe not in load list (still needed in source)
    extra_source_fields: dict[str, set[str]] = field(default_factory=dict)


# year(today()) and year(dt_x) = year(today()) patterns
YEAR_TODAY_RE = re.compile(r"Year\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*=\s*year\s*\(\s*today\s*\(\s*\)\s*\)", re.I)
EQ_LITERAL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'([^']+)'")
EXISTS_RE = re.compile(r"EXISTS\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", re.I)
ALL_FIELD_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]+)\b")


def _strip_string_literals(s: str) -> str:
    """Replace 'literal' / "literal" content with spaces so a field-name
    scan won't pick up tokens that are actually string contents."""
    return re.sub(r"'[^']*'|\"[^\"]*\"", lambda m: " " * len(m.group(0)), s)


def analyze_filters(model: ScriptModel) -> FilterHints:
    hints = FilterHints()

    field_to_sources: dict[str, list[tuple[LoadBlock, str]]] = {}
    for b in model.load_blocks:
        if not b.is_external:
            continue
        for f in b.fields:
            field_to_sources.setdefault(f.source_name, []).append((b, b.source_key))
            if f.alias:
                field_to_sources.setdefault(f.alias, []).append((b, b.source_key))

    for block in model.load_blocks:
        if not block.is_external:
            continue
        if not block.where_clause:
            continue
        w = block.where_clause
        w_no_strings = _strip_string_literals(w)

        for m in YEAR_TODAY_RE.finditer(w_no_strings):
            hints.require_current_year.add(m.group(1))

        # EQ_LITERAL_RE still operates on the original — it needs the literal value
        for m in EQ_LITERAL_RE.finditer(w):
            hints.required_values.setdefault(m.group(1), []).append(m.group(2))

        for m in EXISTS_RE.finditer(w_no_strings):
            child_field = m.group(1)
            for other_block in model.load_blocks:
                if other_block is block:
                    continue
                for f in other_block.fields:
                    if f.output_name == child_field or f.source_name == child_field:
                        hints.exists_constraints.append(
                            (block.source_key, child_field, other_block.source_key)
                        )
                        break

        # Any field referenced in WHERE that isn't in this LOAD's field list
        loaded_names = {f.source_name for f in block.fields} | {f.alias for f in block.fields if f.alias}
        sql_keywords = {
            "year", "today", "and", "or", "not", "match", "wildmatch", "exists",
            "true", "false", "null", "is", "in", "like", "between",
        }
        for m in ALL_FIELD_RE.finditer(w_no_strings):
            name = m.group(1)
            if name.lower() in sql_keywords:
                continue
            if name not in loaded_names and not name.isdigit():
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name) and len(name) > 2:
                    hints.extra_source_fields.setdefault(block.source_key, set()).add(name)

    return hints


# ---------------------------------------------------------------------------
# Step 5: Plan source files (one file per unique source path; union of fields)
# ---------------------------------------------------------------------------

@dataclass
class SourcePlan:
    source_key: str
    source_path: str | None
    fields: dict[str, str]  # name -> inferred type
    filter_hints: dict[str, Any] = field(default_factory=dict)


def build_source_plans(model: ScriptModel, hints: FilterHints) -> list[SourcePlan]:
    plans: dict[str, SourcePlan] = {}

    for block in model.load_blocks:
        # Only external blocks need dummy data — internal/resident/concat/mapping/preceding skip
        if not block.is_external:
            continue

        key = block.source_key
        plan = plans.get(key)
        if not plan:
            plan = SourcePlan(
                source_key=key,
                source_path=block.source_path or (f"sql://{block.sql_table}" if block.sql_table else None),
                fields={},
            )
            plans[key] = plan

        for f in block.fields:
            if f.source_name and f.inferred_type != "expression":
                if f.source_name not in plan.fields:
                    plan.fields[f.source_name] = infer_type(f.source_name)
            elif f.source_name and f.inferred_type == "expression":
                for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]+)\b", f.source_name):
                    nm = m.group(1)
                    if nm.lower() not in {"mid", "left", "right", "if", "match", "date", "num", "text"}:
                        if nm not in plan.fields:
                            plan.fields[nm] = infer_type(nm)

        for extra in hints.extra_source_fields.get(key, set()):
            if extra not in plan.fields:
                plan.fields[extra] = infer_type(extra)

    return list(plans.values())


# ---------------------------------------------------------------------------
# Step 6: Generate synthetic data
# ---------------------------------------------------------------------------

def safe_filename(s: str) -> str:
    # Turn lib://NAS_DATA/.../Primary Fact Table.QVD into something filesystem-friendly
    base = s.split("/")[-1]
    base = base.replace(".QVD", "").replace(".qvd", "")
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", base)
    return base.strip("_") or "source"


def random_id_string(length: int = 21) -> str:
    return "".join(random.choices(string.digits, k=length))


def generate_value(field_type: str, hints_for_field: list[str] | None, current_year: bool, row_idx: int) -> Any:
    if hints_for_field and random.random() < 0.7:
        return random.choice(hints_for_field)

    today = datetime.now()
    if field_type == "date":
        if current_year:
            start = datetime(today.year, 1, 1)
            end = today
        else:
            start = today - timedelta(days=365 * 3)
            end = today
        delta = (end - start).days or 1
        d = start + timedelta(days=random.randint(0, delta))
        return d.strftime("%m/%d/%Y")

    if field_type == "datetime":
        if current_year:
            start = datetime(today.year, 1, 1)
        else:
            start = today - timedelta(days=365 * 3)
        delta = (today - start).total_seconds() or 1
        d = start + timedelta(seconds=random.randint(0, int(delta)))
        return d.strftime("%m/%d/%Y %H:%M:%S")

    if field_type == "duration_ms":
        return random.randint(50, 5_000_000)

    if field_type == "integer":
        return random.randint(0, 365)

    if field_type == "zipcode":
        return f"{random.randint(10000, 99999)}"

    if field_type == "id_string":
        return random_id_string(random.choice([7, 10, 21]))

    if field_type == "person_name":
        return fake.name()

    if field_type == "company_name":
        return fake.company()

    if field_type == "place_name":
        return fake.state()

    if field_type == "language_code":
        return random.choice(["EN", "ES", "FR", "DE"])

    if field_type == "long_text":
        return fake.sentence(nb_words=random.randint(6, 18))

    if field_type == "yn_flag":
        return random.choice(["Y", "N"])

    if field_type == "category_string":
        return random.choice([f"Cat_{i:02d}" for i in range(1, 9)])

    return fake.word()


def generate_data(
    plans: list[SourcePlan],
    row_counts: dict[str, int],
    hints: FilterHints,
) -> dict[str, list[dict[str, Any]]]:
    """Generate rows per source plan, respecting EXISTS constraints by ordering."""
    data: dict[str, list[dict[str, Any]]] = {}

    # Resolve EXISTS dependency order: child -> parent. Generate parents first.
    deps = {plan.source_key: set() for plan in plans}
    for child_key, _, parent_key in hints.exists_constraints:
        if child_key in deps and parent_key in deps:
            deps[child_key].add(parent_key)

    # Topological sort
    ordered = []
    remaining = dict(deps)
    while remaining:
        ready = [k for k, v in remaining.items() if not v]
        if not ready:
            ordered.extend(remaining.keys())
            break
        for k in ready:
            ordered.append(k)
            del remaining[k]
        for v in remaining.values():
            v.difference_update(ready)

    plan_by_key = {p.source_key: p for p in plans}

    for key in ordered:
        plan = plan_by_key[key]
        n = row_counts.get(key, row_counts.get(plan.source_path or "", 1000))
        rows = []

        # Find EXISTS constraints where THIS plan is the child
        applicable_exists = [
            (cf, pk) for ck, cf, pk in hints.exists_constraints if ck == key
        ]

        for i in range(n):
            row = {}
            for fname, ftype in plan.fields.items():
                # Filter awareness
                hint_vals = hints.required_values.get(fname)
                in_current_year = fname in hints.require_current_year

                # EXISTS: pull this field's value from the parent's already-generated rows
                pulled = False
                for child_field, parent_key in applicable_exists:
                    if fname == child_field and parent_key in data and data[parent_key]:
                        parent_row = random.choice(data[parent_key])
                        if fname in parent_row:
                            row[fname] = parent_row[fname]
                            pulled = True
                            break
                if pulled:
                    continue

                row[fname] = generate_value(ftype, hint_vals, in_current_year, i)
            rows.append(row)

        data[key] = rows

    # Cross-source key alignment: if two source plans share a field name with id_string type,
    # have one borrow some keys from the other so joins actually produce hits
    keyish = {}
    for plan in plans:
        for fname, ftype in plan.fields.items():
            if ftype == "id_string":
                keyish.setdefault(fname, []).append(plan.source_key)

    for fname, source_keys in keyish.items():
        if len(source_keys) < 2:
            continue
        primary_key = source_keys[0]
        primary_values = [r[fname] for r in data[primary_key] if fname in r]
        if not primary_values:
            continue
        for sk in source_keys[1:]:
            for r in data[sk]:
                if random.random() < 0.7:  # 70% join hit rate
                    r[fname] = random.choice(primary_values)

    return data


# ---------------------------------------------------------------------------
# Step 7: Write CSVs and the include script
# ---------------------------------------------------------------------------

def write_patched_script(
    original_script_path: Path,
    plans: list[SourcePlan],
    out_dir: Path,
    space_name: str | None = None,
    subfolder: str = "DummyData",
    csv_format: str = "comma",
    model: ScriptModel | None = None,
) -> Path:
    """Produce a copy of the original script with external-source clauses
    rewritten to read the local CSVs we generated. Handles:
      - FROM [path] (qvd|ooxml|...) → CSV path
      - SELECT ... FROM <sql_table> [WHERE ...]; → LOAD * FROM csv;
    """
    text = original_script_path.read_text(encoding="utf-8")

    if space_name:
        lib_prefix = f"lib://{space_name}:DataFiles/{subfolder}"
    else:
        lib_prefix = f"lib://DataFiles/{subfolder}"

    if csv_format == "semicolon":
        csv_format_str = "(txt, utf8, embedded labels, delimiter is ';', msq)"
    else:
        csv_format_str = "(txt, utf8, embedded labels, delimiter is ',', msq)"

    # Map source_key → CSV filename
    key_to_csv: dict[str, str] = {}
    for plan in plans:
        if plan.source_path:
            key_to_csv[plan.source_key] = safe_filename(plan.source_path) + ".csv"

    # Pass 1 — rewrite FROM [path] (format) for QVD/ooxml/txt/etc
    # Match common file source formats (qvd, ooxml, csv, txt, biff, html, etc.)
    file_src_pattern = re.compile(
        r"\[([^\]]+)\]\s*\(\s*(qvd|ooxml|biff|html|xml|json|fix|dif|csv|txt)\b[^)]*\)",
        re.IGNORECASE,
    )

    def replace_file_src(match: re.Match) -> str:
        path = match.group(1)
        csv_name = key_to_csv.get(path)
        if not csv_name:
            return match.group(0)
        return f"[{lib_prefix}/{csv_name}] {csv_format_str}"

    patched = file_src_pattern.sub(replace_file_src, text)

    # Pass 2 — rewrite SELECT ... FROM ... ;  blocks
    # We use the model's external_sql blocks to find each SELECT's exact text and replace it.
    sql_replacements = 0
    if model is not None:
        for block in model.load_blocks:
            if block.block_kind != BLOCK_EXTERNAL_SQL or not block.sql_select_text:
                continue
            csv_name = key_to_csv.get(block.source_key)
            if not csv_name:
                continue
            replacement = f"LOAD *\nFROM [{lib_prefix}/{csv_name}] {csv_format_str}"
            # The stored sql_select_text doesn't have the trailing ; — we need to
            # find and replace the original verbatim. The original may have a
            # leading [Label]: prefix preserved separately.
            select_chunk = block.sql_select_text.strip()
            # Find the SELECT keyword and use that as the replacement boundary
            sel_idx = re.search(r"\bSELECT\b", select_chunk, re.IGNORECASE)
            if sel_idx:
                select_chunk = select_chunk[sel_idx.start():]

            # Search for this exact SELECT chunk in the (already-FROM-patched) text.
            # Whitespace differences between strip_comments output and original are
            # the main issue, so we match flexibly on the first ~200 chars.
            search_target = select_chunk[:200].strip()
            # Build a regex tolerant of whitespace
            esc = re.escape(search_target)
            esc = re.sub(r"\\\s+", r"\\s+", esc)
            m = re.search(esc, patched, re.IGNORECASE)
            if not m:
                # Fallback: locate by SQL table name
                if block.sql_table:
                    tbl_pat = re.escape(block.sql_table.split(".")[-1])
                    m_alt = re.search(
                        r"SELECT\b[\s\S]*?\b" + tbl_pat + r"\b[\s\S]*?(?=;)",
                        patched,
                        re.IGNORECASE,
                    )
                    if m_alt:
                        patched = patched[:m_alt.start()] + replacement + patched[m_alt.end():]
                        sql_replacements += 1
                continue
            # Find the end of the full SELECT statement (next top-level `;`)
            end_idx = patched.find(";", m.start())
            if end_idx == -1:
                continue
            patched = patched[:m.start()] + replacement + patched[end_idx:]
            sql_replacements += 1

    # Pass 3 — comment out LIB CONNECT TO statements. These reference data
    # connections (SQL, REST, etc.) that may not exist in the test tenant.
    # Since the SELECT statements they supported have been replaced with
    # CSV loads, the connections aren't needed for reload. The user can
    # un-comment any specific LIB CONNECT TO they still want.
    lib_connect_count = 0
    def comment_lib_connect(match: re.Match) -> str:
        nonlocal lib_connect_count
        lib_connect_count += 1
        # Comment every line of the original statement
        commented = "\n".join(
            f"// [PATCHED] {ln}" if ln.strip() else ln
            for ln in match.group(0).split("\n")
        )
        return commented

    patched = re.sub(
        r"\bLIB\s+CONNECT\s+TO\b[^;]*;",
        comment_lib_connect,
        patched,
        flags=re.IGNORECASE,
    )

    space_line = (
        f"//   Space:        {space_name}\n"
        if space_name
        else "//   Space:        (none — assumes personal space)\n"
    )
    header = (
        "// =====================================================================\n"
        "// PATCHED SCRIPT — auto-generated by qlik_data_generator.py\n"
        f"// Original script: {original_script_path.name}\n"
        f"// Generated:       {datetime.now().isoformat(timespec='seconds')}\n"
        "//\n"
        "// Target environment:\n"
        f"{space_line}"
        f"//   Subfolder:    DataFiles/{subfolder}\n"
        f"//   CSV format:   {csv_format}\n"
        f"//   SQL SELECT replacements:    {sql_replacements}\n"
        f"//   LIB CONNECT TO commented:   {lib_connect_count}\n"
        "//\n"
        "// To use:\n"
        f"//   1. Upload the CSVs from the 'data/' folder into the '{subfolder}'\n"
        "//      subfolder of DataFiles in"
        + (f" the '{space_name}' space.\n" if space_name else " your personal space.\n") +
        "//   2. Open this script in your Qlik app (or paste it in) and reload.\n"
        f"//   3. External-source FROM clauses and SELECT statements rewritten to point at:\n"
        f"//      {lib_prefix}/<filename>.csv\n"
        "// =====================================================================\n\n"
    )

    patched_path = out_dir / (original_script_path.stem + "_PATCHED.qvs")
    patched_path.write_text(header + patched, encoding="utf-8")
    return patched_path


def write_outputs(
    plans: list[SourcePlan],
    data: dict[str, list[dict[str, Any]]],
    out_dir: Path,
    csv_format: str = "comma",
) -> tuple[list[Path], Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_dir = out_dir / "data"
    csv_dir.mkdir(exist_ok=True)

    delimiter = "," if csv_format == "comma" else ";"
    # Always write the header row so the FROM clause can use 'embedded labels'.
    # 'no labels' (positional) breaks WHERE clauses that reference fields not in
    # the LOAD list, since with no labels only LOAD-named fields exist.

    csv_paths = []
    mapping_lines = []

    for plan in plans:
        if plan.source_path is None:
            continue
        rows = data.get(plan.source_key, [])
        fieldnames = list(plan.fields.keys())
        fname_safe = safe_filename(plan.source_path) + ".csv"
        csv_path = csv_dir / fname_safe

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, delimiter=delimiter)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fieldnames})
        csv_paths.append(csv_path)

        mapping_lines.append((plan.source_path, csv_path.name))

    include_path = out_dir / "_DummyData_Include.qvs"
    lines = [
        "// Auto-generated dummy-data include script",
        "// Generated by qlik_data_generator.py",
        f"// Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "//",
        "// To use: place this file alongside the original script and add",
        "// near the top of the original script:    $(Must_Include=_DummyData_Include.qvs);",
        "// Then comment out the original LOAD ... FROM [lib://...] (qvd) line OR",
        "// rely on the SET-variable redirection below.",
        "",
        "// --- Map original source paths to local CSV files ---",
    ]
    for orig, local in mapping_lines:
        var_safe = re.sub(r"[^A-Za-z0-9_]+", "_", orig).strip("_")
        lines.append(f"SET PATH_{var_safe} = '{local}';")
    lines.append("")
    lines.append("// --- Helper: a CONNECT statement to the data folder ---")
    lines.append("LIB CONNECT TO 'DummyDataFolder';  // create this DataConnection in your space pointing to ./data")
    lines.append("")
    lines.append("// NOTE: For each LOAD block in the original script that reads from a QVD,")
    lines.append("// replace:    FROM [lib://Real/Path/Source.QVD] (qvd)")
    lines.append("// with:       FROM [lib://DummyDataFolder/Source.csv] (txt, codepage is 65001, embedded labels, delimiter is ',', msq)")
    lines.append("")
    include_path.write_text("\n".join(lines), encoding="utf-8")

    return csv_paths, include_path


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_schema_report(model: ScriptModel, plans: list[SourcePlan], hints: FilterHints) -> None:
    print("=" * 78)
    print("PARSED SCHEMA REPORT")
    print("=" * 78)
    print(f"\nLOAD blocks detected: {len(model.load_blocks)}")

    # Group by classification for a clearer report
    kinds_summary: dict[str, list[LoadBlock]] = {}
    for b in model.load_blocks:
        kinds_summary.setdefault(b.block_kind, []).append(b)

    print("\nClassification summary:")
    for kind, blocks in sorted(kinds_summary.items()):
        marker = "  [needs dummy data]" if kind in (BLOCK_EXTERNAL_FILE, BLOCK_EXTERNAL_SQL) else ""
        print(f"  {kind:25s} {len(blocks):3d} block(s){marker}")

    print("\nExternal blocks (will be mocked):")
    has_external = False
    for b in model.load_blocks:
        if not b.is_external:
            continue
        has_external = True
        if b.block_kind == BLOCK_EXTERNAL_FILE:
            print(f"  - {b.table_name}  ({len(b.fields)} fields)  <- file: {b.source_path}  ({b.source_format})")
        else:
            print(f"  - {b.table_name}  ({len(b.fields)} fields)  <- SQL: {b.sql_table}")
        if b.where_clause:
            ww = " ".join(b.where_clause.split())
            print(f"      WHERE: {ww[:120]}{'...' if len(ww) > 120 else ''}")
    if not has_external:
        print("  (none — script appears to be entirely internal/inline)")

    print(f"\nUnique source files/tables to mock: {len(plans)}")
    for p in plans:
        print(f"  - {p.source_path}  ({len(p.fields)} fields total)")

    print(f"\nDropped fields: {model.dropped_fields or '(none)'}")

    print(f"\nFilter hints:")
    print(f"  Fields requiring current year: {sorted(hints.require_current_year) or '(none)'}")
    print(f"  Required literal values:")
    for fname, vals in hints.required_values.items():
        print(f"    {fname} = {vals[:5]}{'...' if len(vals) > 5 else ''}")
    print(f"  EXISTS constraints:")
    for c in hints.exists_constraints:
        print(f"    {c}")
    print(f"  Extra source fields needed:")
    for sk, fields_ in hints.extra_source_fields.items():
        print(f"    {sk}: {sorted(fields_)}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Generate dummy data for a Qlik load script.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Two input modes are supported:\n"
            "  Local file:   --script-file path/to/script.qvs\n"
            "  Qlik Cloud:   --tenant tenant.us.qlikcloud.com --app-id <appId>\n"
            "                (API key from $QLIK_API_KEY env var, or --api-key)\n\n"
            "Examples:\n"
            "  python qlik_data_generator.py --script-file CaseDetails.qvs \\\n"
            "      --space 'Customer App Dev' --subfolder Acme_Dummy --csv-format semicolon\n\n"
            "  export QLIK_API_KEY=eyJhbGc...\n"
            "  python qlik_data_generator.py --tenant my-tenant.us.qlikcloud.com \\\n"
            "      --app-id 12345678-aaaa-bbbb-cccc-1234567890ab \\\n"
            "      --space 'Customer App Dev' --subfolder Acme_Dummy\n"
        ),
    )

    # Input mode (mutually exclusive)
    src = ap.add_argument_group("Script input (choose one)")
    src.add_argument(
        "--script-file",
        help="Path to a .qvs script exported from a Qlik app.",
    )
    src.add_argument(
        "--tenant",
        help="Qlik Cloud tenant URL (e.g. 'my-tenant.us.qlikcloud.com'). Used with --app-id.",
    )
    src.add_argument(
        "--app-id",
        help="Qlik Cloud app GUID. Used with --tenant.",
    )
    src.add_argument(
        "--api-key",
        default=os.environ.get("QLIK_API_KEY"),
        help="Qlik Cloud API key. Defaults to the QLIK_API_KEY environment variable.",
    )

    ap.add_argument("--config", help="Path to row-count JSON config", default=None)
    ap.add_argument("--out", default="./qlik_dummy_out", help="Output directory")
    ap.add_argument("--default-rows", type=int, default=1000, help="Default rows per source")
    ap.add_argument(
        "--space",
        default=None,
        help=(
            "Qlik Cloud space name where the CSVs will live, e.g. 'Customer App Dev'. "
            "Produces lib://<Space>:DataFiles/<subfolder>/file.csv. "
            "Omit for personal space (then paths are lib://DataFiles/<subfolder>/file.csv)."
        ),
    )
    ap.add_argument(
        "--subfolder",
        default="DummyData",
        help=(
            "Subfolder under DataFiles where the generated CSVs will be uploaded. "
            "Default: DummyData. Use a customer-specific name (e.g. 'CustomerXYZ_Dummy') "
            "to keep mock data separated per engagement."
        ),
    )
    ap.add_argument(
        "--csv-format",
        choices=["comma", "semicolon"],
        default="comma",
        help=(
            "CSV format preset for both the generated files and the patched FROM clause:\n"
            "  comma    -> delimiter ',', embedded labels (header row in CSV) [default]\n"
            "  semicolon -> delimiter ';', no labels (no header row; field names come\n"
            "               from the LOAD statement). More robust against field names\n"
            "               with spaces or special characters."
        ),
    )
    args = ap.parse_args()

    # Validate input mode
    using_file = bool(args.script_file)
    using_cloud = bool(args.tenant or args.app_id)

    if using_file and using_cloud:
        ap.error("Specify either --script-file OR (--tenant + --app-id), not both.")
    if not using_file and not using_cloud:
        ap.error("You must provide either --script-file or (--tenant + --app-id).")
    if using_cloud:
        if not (args.tenant and args.app_id):
            ap.error("Cloud mode requires both --tenant and --app-id.")
        if not args.api_key:
            ap.error(
                "Cloud mode requires an API key. Set the QLIK_API_KEY environment "
                "variable or pass --api-key."
            )

    out_dir = Path(args.out)

    # Resolve the script source
    if using_cloud:
        print(f"Fetching script from Qlik Cloud: {args.tenant} / app {args.app_id}")
        try:
            script_path = fetch_script_from_cloud(
                args.tenant, args.app_id, args.api_key, out_dir
            )
        except QlikCloudError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(2)
        print(f"  -> Saved script to: {script_path}")
        print()
    else:
        script_path = Path(args.script_file)
        if not script_path.exists():
            ap.error(f"Script file not found: {script_path}")

    script_text = script_path.read_text(encoding="utf-8")
    model = parse_script(script_text)
    apply_type_inference(model)
    hints = analyze_filters(model)
    plans = build_source_plans(model, hints)

    print_schema_report(model, plans, hints)

    # Row count config
    if args.config and Path(args.config).exists():
        cfg = json.loads(Path(args.config).read_text())
        row_counts = cfg.get("row_counts", {})
    else:
        row_counts = {p.source_key: args.default_rows for p in plans}

    data = generate_data(plans, row_counts, hints)
    csv_paths, include_path = write_outputs(plans, data, out_dir, csv_format=args.csv_format)
    patched_path = write_patched_script(
        script_path,
        plans,
        out_dir,
        space_name=args.space,
        subfolder=args.subfolder,
        csv_format=args.csv_format,
        model=model,
    )

    # Save the row-count config for the user to edit and re-run
    template = {"row_counts": {p.source_key: row_counts.get(p.source_key, args.default_rows) for p in plans}}
    (out_dir / "row_counts.json").write_text(json.dumps(template, indent=2))

    print("=" * 78)
    print("OUTPUT")
    print("=" * 78)
    print(f"CSV files written to: {(out_dir / 'data').resolve()}")
    for c in csv_paths:
        row_count = sum(1 for _ in c.open()) - 1  # both formats have a header now
        print(f"  - {c.name}  ({row_count} rows)")

    # Make the upload destination unmistakable
    if args.space:
        upload_path = f"lib://{args.space}:DataFiles/{args.subfolder}/"
    else:
        upload_path = f"lib://DataFiles/{args.subfolder}/"
    print()
    print("=" * 78)
    print("NEXT STEPS — UPLOAD THESE FILES")
    print("=" * 78)
    print(f"In your Qlik Cloud tenant, upload the {len(csv_paths)} CSV file(s) above to:")
    print(f"    {upload_path}")
    print()
    if args.space:
        print(f"  1. Open the '{args.space}' space")
    print(f"  2. Create a folder named '{args.subfolder}' under DataFiles (if it doesn't exist)")
    print(f"  3. Upload each CSV listed above into that folder")
    print(f"  4. Open a new Qlik app in the same space and reload the patched script:")
    print(f"     {patched_path.name}")
    print()
    print(f"Patched script:    {patched_path.resolve()}")
    print(f"Row-count config:  {(out_dir / 'row_counts.json').resolve()}")
    print(f"Helper include:    {include_path.resolve()}")
    print()


if __name__ == "__main__":
    main()
