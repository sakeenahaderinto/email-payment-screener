"""Load the checked payment rows into the Snowflake payment instruction queue.

Target (already provisioned, empty, never created or altered here):
    PAYMENTS_OPS.PAYMENT_QUEUE.PAYMENT_INSTRUCTION_QUEUE

Setup:
    pip install snowflake-connector-python pandas python-dotenv

    Put these in your environment or a .env file (and keep .env out of git):
        SNOWFLAKE_ACCOUNT=...
        SNOWFLAKE_USER=...
        SNOWFLAKE_PASSWORD=...

Run:
    python load_queue.py --dry-run     # validate + map, touch nothing, write a preview
    python load_queue.py               # the real load (all or nothing, once)
    python load_queue.py --checks-only # run queue_checks.sql against the table

The connection uses role PAYMENTS_OPS_LOADER, which can only INSERT and SELECT
on the queue table. Credentials are never printed or written anywhere.
"""
import argparse
import json
import os
import re
import secrets
import sys
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

TABLE = "PAYMENTS_OPS.PAYMENT_QUEUE.PAYMENT_INSTRUCTION_QUEUE"
ROLE, WAREHOUSE, DATABASE, SCHEMA = "PAYMENTS_OPS_LOADER", "PAYMENTS_OPS_WH", "PAYMENTS_OPS", "PAYMENT_QUEUE"
ENV_VARS = ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"]

# LOADED_AT is left out on purpose: Snowflake fills it from its DEFAULT.
QUEUE_COLUMNS = [
    "EMAIL_ID", "CLIENT_ID", "PAYEE_NAME", "PAYMENT_DATE", "AMOUNT", "CURRENCY",
    "ACCOUNT_NUMBER", "BANK_DETAIL_CHANGE", "IS_FORWARDED_THREAD", "EXTRACTION_STATUS",
    "DISPOSITION", "HOLD_REASON", "GE_VALIDATION_PASSED", "LOAD_RUN_ID",
]
CSV_REQUIRED = [
    "email_id", "client_id", "payee_name", "payment_date", "amount", "currency",
    "account_number", "is_forwarded_thread", "status", "decision", "hold_reasons",
]

EXPECTED_ROWS, EXPECTED_DISTINCT = 302, 300
EXPECTED_REPEATS = {"EM-00021", "EM-00291"}  # two payments each

ALLOWED_CURRENCIES = {"USD", "EUR", "GBP", "CAD", "CHF", "JPY"}
DATE_MIN, DATE_MAX = date(2026, 9, 1), date(2026, 12, 18)

# Our hold codes -> the queue's single HOLD_REASON values.
OUR_TO_QUEUE = {
    "NOT_OK_STATUS": "unusable_model_output",
    "MISSING_ACCOUNT": "missing_required_field",
    "ACCOUNT_MISMATCH": "bank_detail_change",
    "ACCOUNT_CHANGE_FLAGGED": "bank_detail_change",
}
NO_QUEUE_EQUIVALENT = {"CURRENCY_MISMATCH", "SORT_CODE_MISMATCH", "CLIENT_NOT_ON_FILE"}
BANK_CHANGE_CODES = {"ACCOUNT_MISMATCH", "ACCOUNT_CHANGE_FLAGGED"}
# One reason per row; the first one that applies wins.
PRIORITY = [
    "unusable_model_output", "missing_required_field", "invalid_amount",
    "invalid_payment_date", "invalid_account_number", "bank_detail_change",
]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def clean(v):
    """Empty or whitespace-only becomes a real None; nothing else is altered."""
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    v = str(v).strip()
    return v or None


def scrub(text: str) -> str:
    """Remove credential values from any text before it is printed."""
    for name in ENV_VARS:
        val = os.environ.get(name)
        if val:
            text = text.replace(val, "***")
    return text


def env_is_gitignored() -> bool:
    gi = Path(".gitignore")
    if not gi.exists():
        return False
    patterns = {".env", "/.env", ".env*", "*.env", "**/.env"}
    return any(line.strip() in patterns for line in gi.read_text(encoding="utf-8").splitlines())


def parse_amount(raw: str):
    """Exact two-decimal Decimal, or None if it cannot be stored exactly."""
    try:
        d = Decimal(raw)
        if not d.is_finite() or d <= 0:
            return None
        q = d.quantize(Decimal("0.01"))
        if q != d or len(q.as_tuple().digits) > 18:
            return None
        return q
    except InvalidOperation:
        return None


def parse_date(raw: str):
    """Strict ISO date inside the allowed window, or None."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return None
    try:
        d = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None
    return d if DATE_MIN <= d <= DATE_MAX else None


# --------------------------------------------------------------------------
# Mapping and validation
# --------------------------------------------------------------------------
def map_rows(df: pd.DataFrame, run_id: str):
    """Return (rows, findings, fatal, rows_with_unmapped_codes)."""
    rows, findings, fatal = [], [], []
    unmapped_rows = 0

    for i, rec in enumerate(df.to_dict("records")):
        where = f"CSV line {i + 2}"
        email_id = clean(rec["email_id"])
        client_id = clean(rec["client_id"])
        seq = clean(rec.get("payment_seq"))

        m = re.fullmatch(r"EM-(\d{5})", email_id or "")
        if not (m and 1 <= int(m.group(1)) <= 300):
            fatal.append(f"{where}: EMAIL_ID {email_id!r} is not EM-00001..EM-00300")
            continue
        m = re.fullmatch(r"CL-(\d{4})", client_id or "")
        if not (m and 1001 <= int(m.group(1)) <= 1030):
            fatal.append(f"{where} ({email_id}): CLIENT_ID {client_id!r} is not CL-1001..CL-1030")
            continue
        where = f"{where} ({email_id})"

        # decision and hold codes from the checked table
        decision = (clean(rec["decision"]) or "").lower()
        codes = [c for c in (clean(rec["hold_reasons"]) or "").split("|") if c]
        unknown = [c for c in codes if c not in OUR_TO_QUEUE and c not in NO_QUEUE_EQUIVALENT]
        if decision not in ("ready", "held"):
            fatal.append(f"{where}: decision {decision!r} is not ready/held")
            continue
        if unknown:
            fatal.append(f"{where}: unknown hold code(s) {unknown}")
            continue
        if (decision == "ready") != (len(codes) == 0):
            fatal.append(f"{where}: decision '{decision}' contradicts hold_reasons {codes}")
            continue
        if any(c in NO_QUEUE_EQUIVALENT for c in codes):
            unmapped_rows += 1

        # booleans and status
        fwd = (clean(rec["is_forwarded_thread"]) or "").lower()
        if fwd not in ("true", "false"):
            fatal.append(f"{where}: is_forwarded_thread {rec['is_forwarded_thread']!r} is not true/false")
            continue
        status = clean(rec["status"])
        if status not in ("ok", "extraction_failed"):
            fatal.append(f"{where}: unexpected extraction status {status!r}")
            continue

        # raw values
        payee, currency = clean(rec["payee_name"]), clean(rec["currency"])
        acct, amount_raw, date_raw = clean(rec["account_number"]), clean(rec["amount"]), clean(rec["payment_date"])

        if payee is not None and len(payee) > 200:
            fatal.append(f"{where}: PAYEE_NAME is {len(payee)} chars (limit 200) and no hold reason fits")
            continue
        if currency is not None and currency not in ALLOWED_CURRENCIES:
            fatal.append(f"{where}: CURRENCY {currency!r} is not one of {sorted(ALLOWED_CURRENCIES)}")
            continue

        candidates = {OUR_TO_QUEUE[c] for c in codes if c in OUR_TO_QUEUE}
        if status != "ok":
            candidates.add("unusable_model_output")

        found = []  # validation findings for this row: (field, original, issue)
        for field, v in (("PAYEE_NAME", payee), ("AMOUNT", amount_raw),
                         ("CURRENCY", currency), ("ACCOUNT_NUMBER", acct)):
            if v is None and status == "ok":
                candidates.add("missing_required_field")
                found.append((field, "(empty)", "required field missing"))

        amount = None
        if amount_raw is not None:
            amount = parse_amount(amount_raw)
            if amount is None:
                candidates.add("invalid_amount")
                found.append(("AMOUNT", amount_raw, "not a positive exact 2-decimal amount; stored NULL"))

        pdate = None
        if date_raw is not None:
            pdate = parse_date(date_raw)
            if pdate is None:
                candidates.add("invalid_payment_date")
                found.append(("PAYMENT_DATE", date_raw,
                              f"not a valid ISO date in {DATE_MIN}..{DATE_MAX}; stored NULL"))

        if acct is not None and len(acct) > 34:
            candidates.add("invalid_account_number")
            found.append(("ACCOUNT_NUMBER", acct, f"{len(acct)} chars (limit 34); stored NULL"))
            acct = None

        held = decision == "held" or bool(candidates)
        if held and not candidates:
            fatal.append(f"{where}: held only for {codes}, which have no queue HOLD_REASON value")
            continue
        reason = next(r for r in PRIORITY if r in candidates) if held else "none"

        for field, original, issue in found:
            findings.append({
                "email_id": email_id, "payment_seq": seq, "field": field, "original": original,
                "issue": issue,
                "effect": "ready -> held" if decision == "ready" else "already held",
            })

        rows.append({
            "EMAIL_ID": email_id, "CLIENT_ID": client_id, "PAYEE_NAME": payee,
            "PAYMENT_DATE": pdate, "AMOUNT": amount, "CURRENCY": currency,
            "ACCOUNT_NUMBER": acct,
            "BANK_DETAIL_CHANGE": any(c in BANK_CHANGE_CODES for c in codes),
            "IS_FORWARDED_THREAD": fwd == "true",
            "EXTRACTION_STATUS": "ok" if status == "ok" else "unusable_output",
            "DISPOSITION": "held" if held else "ready",
            "HOLD_REASON": reason,
            "GE_VALIDATION_PASSED": len(codes) == 0,
            "LOAD_RUN_ID": run_id,
        })
    return rows, findings, fatal, unmapped_rows


def check_repeats(rows: list[dict]) -> list[str]:
    problems = []
    counts = Counter(r["EMAIL_ID"] for r in rows)
    if len(rows) != EXPECTED_ROWS or len(counts) != EXPECTED_DISTINCT:
        problems.append(f"expected {EXPECTED_ROWS} rows / {EXPECTED_DISTINCT} distinct EMAIL_IDs, "
                        f"got {len(rows)} / {len(counts)}")
    repeated = {e: n for e, n in counts.items() if n > 1}
    if set(repeated) != EXPECTED_REPEATS or any(n != 2 for n in repeated.values()):
        problems.append(f"repeated EMAIL_IDs should be exactly {sorted(EXPECTED_REPEATS)} with 2 rows each, "
                        f"got {repeated}")
    return problems


# --------------------------------------------------------------------------
# Cross-check against the Great Expectations result
# --------------------------------------------------------------------------
def ge_cross_check(df: pd.DataFrame, path: Path):
    """Return (status, message). status: agree | disagree | unavailable."""
    if not path.exists():
        return "unavailable", f"{path} not found, so GE_VALIDATION_PASSED could not be cross-checked."
    data = json.loads(path.read_text(encoding="utf-8"))
    failed, any_index_list, unindexed_failure = set(), False, False
    for r in data.get("results", []):
        res = r.get("result") or {}
        idx = res.get("unexpected_index_list")
        if idx is not None:
            any_index_list = True
            failed.update(int(i) for i in idx)
        elif r.get("success") is False:
            unindexed_failure = True
    if unindexed_failure or not any_index_list and data.get("success") is False:
        return "unavailable", "The GE result has failures without row indexes, so it cannot be cross-checked."
    ours = {i for i, h in enumerate(df["hold_reasons"]) if clean(h)}
    if failed == ours:
        return "agree", f"GE failing rows ({len(failed)}) match the rows with hold_reasons ({len(ours)})."
    only_ge, only_ours = sorted(failed - ours), sorted(ours - failed)
    return "disagree", (f"GE and hold_reasons disagree: {len(only_ge)} rows fail in GE only "
                        f"(CSV row positions {only_ge[:10]}), {len(only_ours)} rows have hold_reasons only "
                        f"({only_ours[:10]}).")


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def report(rows, findings, unmapped_rows, ge_msg, dry_run: bool) -> None:
    n = len(rows)
    counts = Counter(r["EMAIL_ID"] for r in rows)
    repeated = {e: c for e, c in counts.items() if c > 1}
    verb = "would load" if dry_run else "to load"
    print("\n=== Load report ===")
    print(f"Rows read: {n}   rows {verb}: {n}   distinct EMAIL_IDs: {len(counts)}")
    for label, key in (("DISPOSITION", "DISPOSITION"), ("HOLD_REASON", "HOLD_REASON"),
                       ("BANK_DETAIL_CHANGE", "BANK_DETAIL_CHANGE"),
                       ("GE_VALIDATION_PASSED", "GE_VALIDATION_PASSED")):
        print(f"\n{label}:")
        for k, v in sorted(Counter(r[key] for r in rows).items(), key=lambda kv: str(kv[0])):
            print(f"  {str(k):<26}{v}")

    print(f"\nRepeated EMAIL_IDs (primary key is NOT enforced; both rows are loaded): {repeated or 'none'}")
    print(f"\nRows held or changed by the validation checks: {len(findings)}")
    for f in findings:
        print(f"  {f['email_id']} seq={f['payment_seq']} {f['field']}: original {f['original']!r}: "
              f"{f['issue']} [{f['effect']}]")
    print("\nThings to be aware of:")
    print(f"  - {unmapped_rows} rows carry hold codes the queue has no value for "
          f"({', '.join(sorted(NO_QUEUE_EQUIVALENT))}). They are held for another reason, "
          "but those codes are not stored in the queue.")
    print(f"  - GE cross-check: {ge_msg}")


def print_counts_compare(cur, rows, run_id) -> None:
    for col in ("DISPOSITION", "HOLD_REASON"):
        cur.execute(f"SELECT {col}, COUNT(*) FROM {TABLE} WHERE LOAD_RUN_ID = %s GROUP BY {col}", (run_id,))
        in_sf = dict(cur.fetchall())
        expected = dict(Counter(r[col] for r in rows))
        status = "match" if in_sf == expected else "MISMATCH"
        print(f"  {col}: Snowflake {in_sf} vs dry-run mapping {expected} -> {status}")


# --------------------------------------------------------------------------
# Snowflake
# --------------------------------------------------------------------------
def connect():
    missing = [v for v in ENV_VARS if not os.environ.get(v)]
    if missing:
        sys.exit(f"Missing environment variable(s): {', '.join(missing)}. "
                 "Set them in your environment or .env file.")
    import snowflake.connector  # imported here so --dry-run needs no Snowflake package
    try:
        return snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            password=os.environ["SNOWFLAKE_PASSWORD"],
            role=ROLE, warehouse=WAREHOUSE, database=DATABASE, schema=SCHEMA,
            autocommit=False,
        )
    except Exception as e:  # never echo credentials back
        sys.exit(f"Could not connect to Snowflake ({type(e).__name__}): {scrub(str(e))}")


def run_checks_file(cur, path: Path) -> None:
    """Run each read-only statement in queue_checks.sql and print its result."""
    for chunk in path.read_text(encoding="utf-8").split(";"):
        lines = chunk.strip().splitlines()
        comments = [l.strip().lstrip("-").strip() for l in lines if l.strip().startswith("--")]
        sql = "\n".join(l for l in lines if not l.strip().startswith("--")).strip()
        if not sql:
            continue
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        data = cur.fetchall()
        print(f"\n[{comments[-1] if comments else sql[:60]}]")
        print(pd.DataFrame(data, columns=cols).to_string(index=False) if data else "(no rows)")


def load(rows: list[dict], run_id: str, checks_sql: Path) -> None:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT DISTINCT EMAIL_ID FROM {TABLE}")
        existing = {r[0] for r in cur.fetchall()}
        clash = sorted({r["EMAIL_ID"] for r in rows} & existing)
        if clash:
            sys.exit(f"{len(clash)} EMAIL_IDs from this batch are already in the queue "
                     f"(e.g. {clash[:5]}). Nothing was inserted. The loader role cannot delete, "
                     "so ask the table owner before loading again.")

        placeholders = ", ".join(["%s"] * len(QUEUE_COLUMNS))
        insert_sql = f"INSERT INTO {TABLE} ({', '.join(QUEUE_COLUMNS)}) VALUES ({placeholders})"
        params = [tuple(r[c] for c in QUEUE_COLUMNS) for r in rows]
        try:
            cur.executemany(insert_sql, params)
            cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT EMAIL_ID) FROM {TABLE} WHERE LOAD_RUN_ID = %s",
                        (run_id,))
            total, distinct = cur.fetchone()
            if (total, distinct) != (len(rows), EXPECTED_DISTINCT):
                raise RuntimeError(f"after insert saw {total} rows / {distinct} distinct EMAIL_IDs, "
                                   f"expected {len(rows)} / {EXPECTED_DISTINCT}")
            conn.commit()
        except BaseException as e:
            conn.rollback()
            sys.exit(f"Load failed and was rolled back, nothing was written ({type(e).__name__}): "
                     f"{scrub(str(e))}")

        print(f"\nCommitted run {run_id}.")
        cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT EMAIL_ID) FROM {TABLE} WHERE LOAD_RUN_ID = %s", (run_id,))
        total, distinct = cur.fetchone()
        print(f"After commit: {total} rows from this run, {distinct} distinct EMAIL_IDs "
              f"(expected {len(rows)} and {EXPECTED_DISTINCT}).")
        print_counts_compare(cur, rows, run_id)
        if checks_sql.exists():
            print("\n=== queue_checks.sql ===")
            run_checks_file(cur, checks_sql)
    finally:
        conn.close()


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, default=Path("payments_decisions.csv"))
    ap.add_argument("--ge-result", type=Path, default=Path("ge_validation_result.json"))
    ap.add_argument("--preview", type=Path, default=Path("queue_rows_preview.csv"))
    ap.add_argument("--checks-sql", type=Path, default=Path("queue_checks.sql"))
    ap.add_argument("--dry-run", action="store_true", help="validate and map only; connect to nothing")
    ap.add_argument("--checks-only", action="store_true", help="run queue_checks.sql and stop")
    args = ap.parse_args()

    if not env_is_gitignored():
        print("WARNING: .env is not listed in .gitignore. Add it before committing anything.\n")

    if args.checks_only:
        from dotenv import load_dotenv
        load_dotenv()
        conn = connect()
        try:
            run_checks_file(conn.cursor(), args.checks_sql)
        finally:
            conn.close()
        return

    df = pd.read_csv(args.input, dtype=str, keep_default_na=False)
    print(f"Read {len(df)} rows from {args.input}")
    print(f"Columns: {list(df.columns)}")
    missing = [c for c in CSV_REQUIRED if c not in df.columns]
    if missing:
        sys.exit(f"{args.input} is missing columns: {missing}")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
    rows, findings, fatal, unmapped_rows = map_rows(df, run_id)
    fatal += check_repeats(rows) if not fatal else []
    if fatal:
        print("\nSTOPPING. These need a human decision, nothing was loaded:")
        for f in fatal:
            print(f"  - {f}")
        sys.exit(1)

    ge_status, ge_msg = ge_cross_check(df, args.ge_result)
    report(rows, findings, unmapped_rows, ge_msg, args.dry_run)

    if args.dry_run:
        pd.DataFrame(rows)[QUEUE_COLUMNS].to_csv(args.preview, index=False)
        print(f"\nWrote {args.preview}. First 5 mapped rows:")
        print(pd.DataFrame(rows)[QUEUE_COLUMNS].head(5).to_string(index=False))
        print("\nDry run only: Snowflake was not contacted.")
        return

    if ge_status != "agree":
        sys.exit("\nNot loading: the Great Expectations cross-check did not agree. "
                 "Run --dry-run and resolve it first.")
    from dotenv import load_dotenv
    load_dotenv()
    load(rows, run_id, args.checks_sql)


if __name__ == "__main__":
    main()