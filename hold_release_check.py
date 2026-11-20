"""Mark each extracted payment row as `ready` or `held`.

A row is `ready` only if its account matches the verified account on file for
that client, with the right currency, and nothing about it signals an account
change. Nothing on file has been verified since September 2026, so an email can
never verify a bank change: any differing account is held for a call-back.

Setup:
    pip install pandas openpyxl great_expectations

Run:
    python hold_release_check.py --diagnose-only   # check the join key first
    python hold_release_check.py                   # full run

Outputs:
    payments_decisions.csv       every payment row + decision + hold_reasons
    ge_validation_result.json    the Great Expectations validation result
"""
import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("GX_ANALYTICS_ENABLED", "False")  # keep payment data local

import great_expectations as gx
import great_expectations.expectations as gxe
import pandas as pd
from openpyxl import load_workbook

DEFAULT_PAYMENTS = Path("payments_extracted.csv")
DEFAULT_ACCOUNTS = Path("data") / "Client Settlement Accounts on File.xlsx"
DEFAULT_OUTPUT = Path("payments_decisions.csv")
DEFAULT_GE_OUTPUT = Path("ge_validation_result.json")

ACCOUNT_COLUMNS = {
    "Client ID": "onfile_client_id",
    "Client Name": "onfile_client_name",
    "Account Number On File": "onfile_account_number",
    "Account Bank": "onfile_bank",
    "Account Currency": "onfile_currency",
    "Last Verified On": "onfile_last_verified",
    "Callback Contact": "callback_contact",
}
PAYMENT_REQUIRED = [
    "email_id", "client_id", "client_name", "status", "currency",
    "account_number", "sort_code", "prior_account_number", "prior_sort_code",
]
REASON_ORDER = [
    "NOT_OK_STATUS", "MISSING_ACCOUNT", "CLIENT_NOT_ON_FILE", "ACCOUNT_MISMATCH",
    "CURRENCY_MISMATCH", "SORT_CODE_MISMATCH", "ACCOUNT_CHANGE_FLAGGED",
]


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def load_payments(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)  # strings only: keeps leading zeros
    missing = [c for c in PAYMENT_REQUIRED if c not in df.columns]
    if missing:
        sys.exit(f"{path} is missing columns: {missing}")
    for c in df.columns:
        df[c] = df[c].str.strip().replace("", pd.NA)
    return df.reset_index(drop=True)


def load_accounts(path: Path):
    """Read the accounts sheet as text. Returns (df, numeric_account_cells, sheet)."""
    wb = load_workbook(path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        rows = ws.iter_rows()
        header_cells = next(rows, None)
        if not header_cells:
            continue
        header = [str(c.value).strip() if c.value is not None else "" for c in header_cells]
        if not all(col in header for col in ACCOUNT_COLUMNS):
            continue
        idx = {col: header.index(col) for col in ACCOUNT_COLUMNS}
        records, numeric_cells = [], 0
        for cells in rows:
            rec = {}
            for col, i in idx.items():
                v = cells[i].value if i < len(cells) else None
                if isinstance(v, (datetime.datetime, datetime.date)):
                    v = v.date().isoformat() if hasattr(v, "date") else v.isoformat()
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    if col == "Account Number On File":
                        numeric_cells += 1
                    v = str(int(v)) if float(v).is_integer() else str(v)
                elif v is not None:
                    v = str(v).strip()
                rec[ACCOUNT_COLUMNS[col]] = v or None
            if any(val is not None for val in rec.values()):
                records.append(rec)
        return pd.DataFrame(records), numeric_cells, ws.title
    sys.exit(f"No sheet in {path} has the columns {list(ACCOUNT_COLUMNS)}")


# --------------------------------------------------------------------------
# Normalising
# --------------------------------------------------------------------------
def norm_account(s: pd.Series) -> pd.Series:
    """Same cleaning for both sides: strip spaces/dashes, uppercase, keep as text."""
    return s.str.upper().str.replace(r"[\s\-]+", "", regex=True)


def norm_name(v):
    return None if pd.isna(v) else re.sub(r"[\W_]+", "", str(v).lower())


def py(series: pd.Series) -> list:
    return [None if pd.isna(v) else v for v in series]


# --------------------------------------------------------------------------
# Join-key diagnostics
# --------------------------------------------------------------------------
def diagnose(pay: pd.DataFrame, acc: pd.DataFrame) -> dict:
    n = len(pay)
    pay_name = pay["client_name"].map(norm_name)
    id_to_name = (acc.dropna(subset=["onfile_client_id"])
                  .drop_duplicates("onfile_client_id")
                  .set_index("onfile_client_id")["onfile_client_name"].map(norm_name))
    on_file_name = pay["client_id"].map(id_to_name)
    id_hit = on_file_name.notna()
    id_conflict = id_hit & (on_file_name != pay_name)
    name_hit = pay_name.isin(set(acc["onfile_client_name"].map(norm_name).dropna()))
    conflicts = (pd.DataFrame({
        "client_id": pay["client_id"], "payment_client_name": pay["client_name"],
        "on_file_name_for_that_id": pay["client_id"].map(
            acc.drop_duplicates("onfile_client_id").set_index("onfile_client_id")["onfile_client_name"]),
    })[id_conflict].drop_duplicates())
    return {"rows": n, "id_hit": int(id_hit.sum()), "id_conflict": int(id_conflict.sum()),
            "name_hit": int(name_hit.sum()), "conflicts": conflicts,
            "payment_clients": pay["client_id"].nunique(),
            "accounts_clients": acc["onfile_client_id"].nunique()}


def print_diagnostics(d: dict, acc: pd.DataFrame, numeric_cells: int, sheet: str) -> None:
    print("=== Join-key diagnostics ===")
    print(f"Payment rows: {d['rows']} ({d['payment_clients']} distinct client IDs); "
          f"on-file accounts: {len(acc)} (sheet '{sheet}')")
    print(f"Rows whose client ID exists on file:                {d['id_hit']}")
    print(f"  ...but the on-file name for that ID is different: {d['id_conflict']}")
    print(f"Rows whose client NAME exists on file:              {d['name_hit']}")
    if d["id_conflict"]:
        print("Examples of the same ID belonging to different companies:")
        print(d["conflicts"].head(5).to_string(index=False))

    digits = acc["onfile_account_number"].dropna()
    zero_lead = int(digits.str.match(r"^0\d+$").sum())
    print("\n=== Leading-zero check on the on-file accounts ===")
    print(f"Account cells stored as numbers in Excel: {numeric_cells} of {len(acc)}")
    print(f"Account numbers that start with 0:        {zero_lead}")
    if numeric_cells:
        print("WARNING: numeric cells cannot hold leading zeros, so some may already be lost. "
              "Values were NOT padded or altered. Affected clients can show as ACCOUNT_MISMATCH.")


def print_account_overlap(pay: pd.DataFrame, acc: pd.DataFrame) -> None:
    """Which on-file client does each emailed account actually belong to?
    This tests the join key by evidence instead of by names."""
    e = norm_account(pay["account_number"])
    o = norm_account(acc["onfile_account_number"])
    owner = {a: (i, n) for a, i, n in zip(o, acc["onfile_client_id"], acc["onfile_client_name"]) if pd.notna(a)}
    owner_nz = {a.lstrip("0"): (i, n) for a, (i, n) in owner.items()}
    has_acct = e.notna()
    exact = e.map(lambda a: owner.get(a) if pd.notna(a) else None)
    same_id = [x is not None and x[0] == cid for x, cid in zip(exact, pay["client_id"])]
    other_id = [x is not None and x[0] != cid for x, cid in zip(exact, pay["client_id"])]
    zero_only = [
        pd.notna(a) and a not in owner and a.lstrip("0") in owner_nz and a.isdigit()
        for a in e
    ]
    print("\n=== Account overlap (is the emailed account on file at all?) ===")
    print(f"Rows with an account number:                              {int(has_acct.sum())}")
    print(f"  account on file under the SAME client ID:               {sum(same_id)}")
    print(f"  account on file under a DIFFERENT client ID:            {sum(other_id)}")
    print(f"  account on file only if leading zeros are ignored:      {sum(zero_only)}")
    shown = pay.loc[other_id, ["client_id", "client_name", "account_number"]].head(5).copy()
    if len(shown):
        shown["on_file_client_id"] = [x[0] for x in exact[other_id].head(5)]
        shown["on_file_client_name"] = [x[1] for x in exact[other_id].head(5)]
        print("Examples where the account belongs to a different on-file client:")
        print(shown.to_string(index=False))
    print("(Direct account-number matches only; UK sort code + 8-digit forms are not included here.)")


def choose_join_key(d: dict, requested: str) -> str | None:
    if requested != "auto":
        return requested
    if d["id_hit"] > 0 and d["id_conflict"] == 0:
        return "id"
    if d["name_hit"] >= 0.5 * d["rows"]:
        return "name"
    return None


# --------------------------------------------------------------------------
# Join
# --------------------------------------------------------------------------
def join(pay: pd.DataFrame, acc: pd.DataFrame, key: str) -> pd.DataFrame:
    acc = acc.copy()
    pay = pay.copy()
    if key == "id":
        pay["_key"], acc["_key"] = pay["client_id"], acc["onfile_client_id"]
    else:
        pay["_key"], acc["_key"] = pay["client_name"].map(norm_name), acc["onfile_client_name"].map(norm_name)
    acc = acc.dropna(subset=["_key"])
    if acc["_key"].duplicated().any():
        dupes = acc.loc[acc["_key"].duplicated(keep=False), "_key"].unique()[:5]
        sys.exit(f"Join key '{key}' is not unique on the accounts file, e.g. {list(dupes)}. Stopping.")
    out = pay.merge(acc, on="_key", how="left", validate="m:1").drop(columns="_key")
    out["join_key"] = key
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------
# Row-level comparisons (feed the Great Expectations contract)
# --------------------------------------------------------------------------
IBAN_LIKE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")


def is_gb_iban(a: str) -> bool:
    return a.startswith("GB") and len(a) == 22 and bool(IBAN_LIKE.match(a))


def account_and_sort(e_acct, o_acct, e_sort, on_file: bool):
    """Return (account_ok, sort_ok). Not-applicable cases return True; the
    rules for missing account / client not on file report those separately."""
    if e_acct is None or not on_file:
        return True, True
    if o_acct is None:
        return False, True  # client is on file but has no account recorded
    if e_sort is None:
        return e_acct == o_acct, True
    if is_gb_iban(o_acct):
        # GB IBAN ends with 6-digit sort code + 8-digit account number
        sort_ok = o_acct[-14:-8] == e_sort
        acct_ok = e_acct == o_acct if IBAN_LIKE.match(e_acct) else o_acct[-8:] == e_acct
        return acct_ok, sort_ok
    return e_acct == o_acct, False  # sort code sent but nothing on file to check it against


def add_check_columns(df: pd.DataFrame) -> pd.DataFrame:
    e_acct = py(norm_account(df["account_number"]))
    o_acct = py(norm_account(df["onfile_account_number"]))
    e_sort = py(norm_account(df["sort_code"]))
    on_file = [v is not None for v in py(df["onfile_client_id"])]
    pairs = [account_and_sort(*args) for args in zip(e_acct, o_acct, e_sort, on_file)]
    df["chk_account_match"] = [p[0] for p in pairs]
    df["chk_sort_code_ok"] = [p[1] for p in pairs]

    cur_e = [None if v is None else v.upper() for v in py(df["currency"])]
    cur_o = [None if v is None else v.upper() for v in py(df["onfile_currency"])]
    status_ok = [v == "ok" for v in py(df["status"])]  # failed rows already carry NOT_OK_STATUS
    df["chk_currency_match"] = [
        (not (f and ok)) or (e is not None and e == o)
        for e, o, f, ok in zip(cur_e, cur_o, on_file, status_ok)
    ]
    return df


# --------------------------------------------------------------------------
# Great Expectations contract
# --------------------------------------------------------------------------
def build_suite() -> gx.ExpectationSuite:
    suite = gx.ExpectationSuite(name="payment_release_contract")

    def add(exp):
        suite.add_expectation(exp)

    add(gxe.ExpectColumnValuesToBeInSet(
        column="status", value_set=["ok"], meta={"hold_code": "NOT_OK_STATUS"}))
    add(gxe.ExpectColumnValuesToNotBeNull(
        column="account_number", meta={"hold_code": "MISSING_ACCOUNT"}))
    add(gxe.ExpectColumnValuesToNotBeNull(
        column="onfile_client_id", meta={"hold_code": "CLIENT_NOT_ON_FILE"}))
    add(gxe.ExpectColumnValuesToBeInSet(
        column="chk_account_match", value_set=[True], meta={"hold_code": "ACCOUNT_MISMATCH"}))
    add(gxe.ExpectColumnValuesToBeInSet(
        column="chk_currency_match", value_set=[True], meta={"hold_code": "CURRENCY_MISMATCH"}))
    add(gxe.ExpectColumnValuesToBeInSet(
        column="chk_sort_code_ok", value_set=[True], meta={"hold_code": "SORT_CODE_MISMATCH"}))
    add(gxe.ExpectColumnValuesToBeNull(
        column="prior_account_number", meta={"hold_code": "ACCOUNT_CHANGE_FLAGGED"}))
    add(gxe.ExpectColumnValuesToBeNull(
        column="prior_sort_code", meta={"hold_code": "ACCOUNT_CHANGE_FLAGGED"}))
    return suite


def validate(df: pd.DataFrame):
    if df["status"].isna().any():
        sys.exit("Some rows have no status, so the contract cannot judge them. Stopping.")
    context = gx.get_context(mode="ephemeral")
    asset = context.data_sources.add_pandas("payments_source").add_dataframe_asset("payments")
    batch = asset.add_batch_definition_whole_dataframe("whole").get_batch(
        batch_parameters={"dataframe": df})
    return batch.validate(build_suite(), result_format={"result_format": "COMPLETE"})


def apply_decisions(df: pd.DataFrame, result) -> pd.DataFrame:
    reasons = [set() for _ in range(len(df))]
    for r in result.results:
        code = r.expectation_config.meta["hold_code"]
        for i in r.result.get("unexpected_index_list") or []:
            reasons[int(i)].add(code)
    df["hold_reasons"] = ["|".join(c for c in REASON_ORDER if c in s) for s in reasons]
    df["decision"] = ["held" if s else "ready" for s in reasons]
    return df


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def report(df: pd.DataFrame) -> None:
    print("\n=== Decisions ===")
    counts = df["decision"].value_counts()
    print(f"Total rows: {len(df)}   ready: {counts.get('ready', 0)}   held: {counts.get('held', 0)}")
    print("\nRows per hold reason (a row can have more than one):")
    for code in REASON_ORDER:
        n = df["hold_reasons"].str.split("|").map(lambda x: code in x).sum()
        print(f"  {code:<24}{n}")

    show = ["email_id", "client_id", "client_name", "account_number", "onfile_account_number",
            "currency", "onfile_currency", "sort_code", "prior_account_number", "hold_reasons"]
    print("\n=== Examples: 3 rows per hold reason ===")
    for code in REASON_ORDER:
        sub = df[df["hold_reasons"].str.split("|").map(lambda x: code in x)]
        if len(sub):
            print(f"\n[{code}]")
            print(sub[show].head(3).to_string(index=False))
    print("\n=== Examples: 3 rows marked ready ===")
    ready = df[df["decision"] == "ready"]
    print(ready[show].head(3).to_string(index=False) if len(ready) else "(none)")


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--payments", type=Path, default=DEFAULT_PAYMENTS)
    ap.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--ge-output", type=Path, default=DEFAULT_GE_OUTPUT)
    ap.add_argument("--join-key", choices=["auto", "id", "name"], default="auto")
    ap.add_argument("--diagnose-only", action="store_true",
                    help="print the join-key and leading-zero checks, then stop")
    args = ap.parse_args()

    pay = load_payments(args.payments)
    acc, numeric_cells, sheet = load_accounts(args.accounts)
    diag = diagnose(pay, acc)
    print_diagnostics(diag, acc, numeric_cells, sheet)
    print_account_overlap(pay, acc)
    if args.diagnose_only:
        return

    key = choose_join_key(diag, args.join_key)
    if key is None:
        sys.exit("\nNeither client ID nor client name reliably links the two files. "
                 "Stopping instead of guessing. Fix the keys or pass --join-key id|name.")
    print(f"\nJoining on client {key}.")

    df = add_check_columns(join(pay, acc, key))
    result = validate(df)
    df = apply_decisions(df, result)

    out_cols = (list(pay.columns)
                + ["onfile_client_name", "onfile_account_number", "onfile_currency",
                   "onfile_last_verified", "callback_contact", "join_key",
                   "decision", "hold_reasons"])
    df[out_cols].to_csv(args.output, index=False)
    with open(args.ge_output, "w", encoding="utf-8") as f:
        json.dump(result.to_json_dict(), f, indent=2, default=str)
    print(f"Wrote {args.output} and {args.ge_output}")
    report(df)


if __name__ == "__main__":
    main()