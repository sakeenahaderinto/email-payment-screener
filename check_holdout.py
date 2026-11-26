"""Run the review pipeline over every holdout email and compare it with the gold_* answer key.

It uses the same functions as the review page (extraction, hold rules, queue mapping).
Only the email columns are given to the model. The gold_* columns are read here, after the
pipeline has run, purely for the comparison. Disagreements are reported, never fixed, and
the extraction prompt is not tuned to the gold answers.

Run:
    python check_holdout.py --limit 5     # trial run
    python check_holdout.py               # all holdout emails

Writes holdout_check.csv (one row per email, a match flag per field).
Needs OPENROUTER_API_KEY and OPENROUTER_MODEL (environment or .env). It does not use Snowflake.
"""
import argparse
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

import review_pipeline as rp

# (name, key in the extracted values, gold column)
FIELDS = ["payee_name", "payment_date", "amount", "currency", "account_number",
          "bank_detail_change", "disposition"]
GOLD = {f: f"gold_{f}" for f in FIELDS[:-1]} | {"disposition": "gold_expected_disposition"}


def blank(v) -> str | None:
    v = None if v is None else str(v).strip()
    return v or None


def same(field: str, got, gold) -> bool:
    got, gold = blank(got), blank(gold)
    if got is None or gold is None:
        return got is None and gold is None
    if field == "amount":
        try:
            return Decimal(got) == Decimal(gold)
        except InvalidOperation:
            return False
    if field == "payee_name":
        return " ".join(got.split()).casefold() == " ".join(gold.split()).casefold()
    if field in ("bank_detail_change", "disposition"):
        return got.lower() == gold.lower()
    if field == "currency":
        return got.upper() == gold.upper()
    return got == gold  # dates and account numbers: exactly as written


def values_for(payment: rp.PaymentView) -> dict:
    q, e = payment.queue_row or {}, payment.extracted
    return {
        "payee_name": e.get("payee_name"), "payment_date": e.get("payment_date"),
        "amount": e.get("amount"), "currency": e.get("currency"),
        "account_number": e.get("account_number"),
        "bank_detail_change": (q.get("BANK_DETAIL_CHANGE") or "").lower() or None,
        "disposition": q.get("DISPOSITION") or payment.disposition,
        # extra, informational: did the email itself name a replaced account?
        "prior_account_present": "true" if e.get("prior_account_number") else "false",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, default=None, help="holdout CSV (default: data/Holdout*.csv)")
    ap.add_argument("--output", type=Path, default=Path("holdout_check.csv"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--delay", type=float, default=0.0, help="seconds between emails (rate limits)")
    args = ap.parse_args()

    rp.init_env()
    path = args.input or rp.find_holdout_file()
    if path is None:
        sys.exit("No holdout CSV found in the data folder. Pass --input.")
    df = rp.load_holdout(path, include_gold=True)
    missing = [c for c in GOLD.values() if c not in df.columns]
    if missing:
        sys.exit(f"{path} is missing answer-key columns: {missing}")
    if args.limit:
        df = df.head(args.limit)
    print(f"Holdout file: {path}\nRows: {len(df)}\nColumns: {list(df.columns)}\n")

    accounts, err = rp.load_accounts_checked()
    if accounts is None:
        sys.exit(f"Accounts file could not be loaded: {err}")
    try:
        client, model = rp.get_llm()
    except RuntimeError as e:
        sys.exit(str(e))

    results, disagreements = [], []
    for n, (_, row) in enumerate(df.iterrows(), start=1):
        # Only the email columns go in. gold_* never reaches the model or the pipeline.
        form = {k: row[k] for k in ("client_id", "client_name", "received_at", "sender_email",
                                    "subject", "email_body", "email_id")}
        mode = "yes" if row["is_forwarded_thread"].strip().lower() == "true" else "no"
        a = rp.analyse_email(form, accounts, client, model, forwarded=mode)

        got = {f: None for f in FIELDS} | {"prior_account_present": None}
        if a.payments:
            # an email can hold two payments: compare the one that agrees best with the key
            best = max((values_for(p) for p in a.payments),
                       key=lambda v: sum(same(f, v[f], row[GOLD[f]]) for f in FIELDS))
            got = best
        rec = {"email_id": row["email_id"], "client_id": row["client_id"], "n_payments": len(a.payments),
               "verdict": a.verdict, "hold_reason": a.hold_reason,
               "error": a.error or a.extraction_error or ""}
        for f in FIELDS:
            ok = same(f, got[f], row[GOLD[f]])
            rec[f"{f}_extracted"], rec[f"{f}_gold"], rec[f"{f}_match"] = got[f], row[GOLD[f]], ok
            if not ok:
                disagreements.append((row["email_id"], f, got[f], row[GOLD[f]]))
        rec["prior_account_present"] = got["prior_account_present"]
        rec["prior_account_present_match"] = same("bank_detail_change", got["prior_account_present"],
                                                  row[GOLD["bank_detail_change"]])
        results.append(rec)
        print(f"[{n}/{len(df)}] {row['email_id']}: {a.verdict} ({a.hold_reason})"
              + (f"  ERROR: {a.error}" if a.error else ""))
        if args.delay:
            time.sleep(args.delay)

    out = pd.DataFrame(results)
    out.to_csv(args.output, index=False)
    print(f"\nWrote {args.output}\n\n=== Agreement with the answer key ({len(out)} emails) ===")
    for f in FIELDS + ["prior_account_present"]:
        col = out[f"{f}_match"]
        label = f if f != "prior_account_present" else "prior_account_present vs gold_bank_detail_change"
        print(f"  {label:<52}{int(col.sum()):>4}/{len(col)}  ({100 * col.mean():.1f}%)")
    print("\nNote: bank_detail_change is the queue column (the email's account differs from the file). "
          "prior_account_present asks only whether the email itself names a replaced account.")
    print(f"\n=== Disagreements ({len(disagreements)}) ===")
    for email_id, f, got_v, gold_v in disagreements:
        print(f"  {email_id}  {f}: pipeline={got_v!r}  gold={gold_v!r}")
    if not disagreements:
        print("  none")


if __name__ == "__main__":
    main()