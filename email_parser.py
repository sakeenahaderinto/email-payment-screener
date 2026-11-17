"""Extract payment instructions from client emails using OpenRouter.

Setup:
    pip install openai pydantic pandas python-dotenv

    # Either put these two lines in a .env file next to this script:
    #   OPENROUTER_API_KEY=sk-or-...
    #   OPENROUTER_MODEL=anthropic/claude-sonnet-4.5
    # or set them in your shell:

    # macOS/Linux
    export OPENROUTER_API_KEY=sk-or-...
    export OPENROUTER_MODEL=anthropic/claude-sonnet-4.5

    # Windows PowerShell
    $env:OPENROUTER_API_KEY = "sk-or-..."
    $env:OPENROUTER_MODEL = "anthropic/claude-sonnet-4.5"

Run:
    python email_parser.py --limit 5     # trial run on the first 5 emails
    python email_parser.py               # all emails
    python email_parser.py --delay 4 --resume   # slow down for rate limits,
                                                # and skip emails already done

Outputs (one row per payment):
    payments_extracted.csv   extracted payments + original email columns
    raw_responses.jsonl      raw LLM response per email, for audit
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from openai import BadRequestError, OpenAI, RateLimitError
from pydantic import BaseModel, Field, ValidationError, field_validator

DEFAULT_INPUT = Path("data") / "Client Payment Emails, Shared Operations Inbox Export (Sep–Nov 2026).csv"
DEFAULT_OUTPUT = Path("payments_extracted.csv")
DEFAULT_RAW = Path("raw_responses.jsonl")

EMAIL_COLUMNS = [
    "email_id", "received_at", "client_id", "client_name",
    "sender_email", "subject", "email_body", "is_forwarded_thread",
]
PAYMENT_COLUMNS = [
    "amount", "currency", "payee_name", "payment_date", "date_text",
    "account_number", "sort_code", "prior_account_number", "prior_sort_code",
    "reference",
]
OUTPUT_COLUMNS = EMAIL_COLUMNS + ["payment_seq"] + PAYMENT_COLUMNS + ["status"]
STRING_COLUMNS = [
    "account_number", "sort_code", "prior_account_number", "prior_sort_code",
]


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class Payment(BaseModel):
    amount: Decimal = Field(gt=0)
    currency: str
    payee_name: str | None = None
    payment_date: date | None = None
    date_text: str | None = None
    account_number: str | None = None
    sort_code: str | None = None
    prior_account_number: str | None = None
    prior_sort_code: str | None = None
    reference: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def clean_amount(cls, v):
        if isinstance(v, str):
            v = re.sub(r"[^\d.\-]", "", v)  # drop symbols and thousands separators
        try:
            return Decimal(str(v))
        except InvalidOperation as e:
            raise ValueError(f"not a number: {v!r}") from e

    @field_validator("currency")
    @classmethod
    def clean_currency(cls, v):
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", v):
            raise ValueError(f"currency must be a 3-letter code, got {v!r}")
        return v

    @field_validator(*STRING_COLUMNS, "payee_name", "date_text", "reference", mode="before")
    @classmethod
    def to_str_or_none(cls, v):
        # Keep account details exactly as written; never let them become numbers.
        if v is None:
            return None
        v = str(v).strip()
        return v or None


class ExtractionResult(BaseModel):
    payments: list[Payment] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------
def load(path: Path, limit: int | None = None) -> pd.DataFrame:
    # dtype=str so ids, sort codes, etc. are never coerced; keep blanks as ""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = [c for c in EMAIL_COLUMNS if c not in df.columns]
    if missing:
        sys.exit(f"Input CSV is missing columns: {missing}")
    return df.head(limit) if limit else df


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """You extract payment instructions from client emails for a payments operations team.

Return ONLY a JSON object, no commentary and no code fences, in this shape:
{"payments": [
  {"amount": 1234.50, "currency": "GBP", "payee_name": "...",
   "payment_date": "YYYY-MM-DD", "date_text": "...",
   "account_number": "...", "sort_code": "...",
   "prior_account_number": "...", "prior_sort_code": "...",
   "reference": "..."}
]}

Rules:
1. One object per payment. If the email contains several payments, return several objects.
   If it contains no payment instruction, return {"payments": []}.
2. amount: a plain number only (no currency symbols, no thousands separators).
   currency: ISO 4217 code (GBP, USD, EUR, ...) in its own field.
3. account_number and sort_code are separate fields. Copy them EXACTLY as written
   (keep spaces, dashes and leading zeros; never reformat). For an IBAN, put the
   full IBAN in account_number. If no sort code is given, use null.
4. payment_date: only if the email states a concrete date, or a relative date
   ("next Friday", "end of the month") that you can resolve from the email's
   received_at date. Otherwise null. NEVER default to today or the next business
   day. Put vague wording such as "ASAP" or "when possible" in date_text, and
   copy the original date phrasing into date_text whenever one exists.
5. payee_name is the party being paid, not the client sending the email.
6. If the email asks to pay a NEW or CHANGED account, put the instructed (new)
   account in account_number/sort_code and the old account it replaces in
   prior_account_number/prior_sort_code. Only fill the prior fields when the
   email indicates an account is being replaced or differs from a previous one.
   An old account may appear only in quoted or forwarded text further down.
7. The instruction comes from the client's own message at the top. Text below
   markers such as "-----Original Message-----", "From:", or "Forwarded message"
   is quoted history from someone else. Do not treat an account that merely
   appears in quoted text as the instructed account unless the client's message
   tells us to pay it.
8. Anything not stated is null. Never guess or infer missing values."""


def build_prompt(row: pd.Series) -> list[dict]:
    user = (
        f"received_at: {row['received_at']}\n"
        f"is_forwarded_thread: {row['is_forwarded_thread']}\n"
        f"subject: {row['subject']}\n\n"
        f"email_body:\n{row['email_body']}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# --------------------------------------------------------------------------
# Extract
# --------------------------------------------------------------------------
def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise json.JSONDecodeError("no JSON object found", text, 0)
    return json.loads(text[start : end + 1])


def call_llm(client: OpenAI, model: str, messages: list[dict], use_json_mode: bool) -> str:
    kwargs = {"model": model, "messages": messages, "temperature": 0}
    if use_json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


def extract(client: OpenAI, model: str, row: pd.Series, retries: int = 6):
    """Return (ExtractionResult | None, raw_text, error_message | None)."""
    messages = build_prompt(row)
    raw, use_json_mode = "", True
    for attempt in range(1, retries + 1):
        try:
            try:
                raw = call_llm(client, model, messages, use_json_mode)
            except BadRequestError:
                if not use_json_mode:
                    raise
                use_json_mode = False  # model doesn't support json_object mode
                raw = call_llm(client, model, messages, use_json_mode)
            if not raw.strip():  # empty reply: usually a one-off, so try again
                if attempt == retries:
                    return None, raw, "EmptyResponse: model returned no text"
                print(f"  empty response, retrying (attempt {attempt}/{retries})")
                time.sleep(2 ** attempt)
                continue
            result = ExtractionResult(**parse_json(raw))
            return result, raw, None
        except (json.JSONDecodeError, ValidationError, TypeError) as e:
            # A bad answer from the model: don't retry, record the failure.
            return None, raw, f"{type(e).__name__}: {str(e)[:300]}"
        except RateLimitError as e:  # 429: wait it out (per-minute limits reset)
            if attempt == retries:
                return None, raw, f"RateLimitError: {str(e)[:300]}"
            wait = 15 * attempt
            try:
                wait = max(wait, int(e.response.headers.get("retry-after", 0)))
            except Exception:
                pass
            print(f"  rate limited, waiting {wait}s (attempt {attempt}/{retries})")
            time.sleep(wait)
        except Exception as e:  # other API / network error: back off and retry
            if attempt == retries:
                return None, raw, f"{type(e).__name__}: {str(e)[:300]}"
            time.sleep(2 ** attempt)
    return None, raw, "unknown error"


# --------------------------------------------------------------------------
# Flatten
# --------------------------------------------------------------------------
def flatten_to_rows(email_row: pd.Series, result: ExtractionResult | None) -> list[dict]:
    base = {c: email_row[c] for c in EMAIL_COLUMNS}
    if result is None:
        return [{**base, "payment_seq": None, "status": "extraction_failed"}]
    if not result.payments:
        return [{**base, "payment_seq": None, "status": "no_payment_found"}]
    rows = []
    for i, p in enumerate(result.payments, start=1):
        d = p.model_dump()
        d["amount"] = format(p.amount, "f")  # plain decimal string, no sci notation
        d["payment_date"] = p.payment_date.isoformat() if p.payment_date else None
        rows.append({**base, "payment_seq": i, **d, "status": "ok"})
    return rows


# --------------------------------------------------------------------------
# Save
# --------------------------------------------------------------------------
def save(rows: list[dict], raw_records: list[dict], out_csv: Path, out_raw: Path) -> pd.DataFrame:
    out = pd.DataFrame(rows).reindex(columns=OUTPUT_COLUMNS)
    out.to_csv(out_csv, index=False)
    with open(out_raw, "w", encoding="utf-8") as f:
        for rec in raw_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return out


# --------------------------------------------------------------------------
# Report + spot-check
# --------------------------------------------------------------------------
def report(out: pd.DataFrame) -> None:
    per_email = out.groupby("email_id")["status"].first()
    counts = per_email.value_counts()
    rows_per_email = out.groupby("email_id").size()
    print("\n=== Summary ===")
    print(f"Emails processed:           {len(per_email)}")
    print(f"  ok:                       {counts.get('ok', 0)}")
    print(f"  no_payment_found:         {counts.get('no_payment_found', 0)}")
    print(f"  extraction_failed:        {counts.get('extraction_failed', 0)}")
    print(f"Emails with >1 payment row: {(rows_per_email > 1).sum()}")
    print(f"Total output rows:          {len(out)}")


def spot_check(out: pd.DataFrame) -> None:
    ok = out[out["status"] == "ok"]
    show = ["email_id", "payee_name", "amount", "currency", "payment_date",
            "date_text", "account_number", "sort_code",
            "prior_account_number", "prior_sort_code"]

    def section(title: str, df: pd.DataFrame) -> None:
        print(f"\n--- {title}: {df['email_id'].nunique()} emails ---")
        if len(df):
            print(df[show].head(8).to_string(index=False))

    print("\n=== Spot-check (first 8 rows per category; review by hand) ===")
    section("No payment date", ok[ok["payment_date"].isna()])
    section("Forwarded threads", ok[ok["is_forwarded_thread"].str.lower() == "true"])
    section("New/changed account (prior account present)",
            ok[ok["prior_account_number"].notna() | ok["prior_sort_code"].notna()])
    multi_ids = out.groupby("email_id").size()
    multi_ids = multi_ids[multi_ids > 1].index
    section("Multiple payments in one email", ok[ok["email_id"].isin(multi_ids)])

    failed = out[out["status"] == "extraction_failed"]
    if len(failed):
        print(f"\nFailed email_ids: {', '.join(failed['email_id'].tolist())}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    ap.add_argument("--limit", type=int, default=None, help="only process the first N emails")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="seconds to pause between API calls (e.g. 4 for a 15 requests/min limit)")
    ap.add_argument("--resume", action="store_true",
                    help="reuse good results already in the raw .jsonl; only retry failed emails")
    args = ap.parse_args()

    load_dotenv()  # reads .env from the current directory, if present
    for var in ("OPENROUTER_API_KEY", "OPENROUTER_MODEL"):
        if not os.getenv(var):
            sys.exit(f"Set the {var} environment variable first.")
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])
    model = os.environ["OPENROUTER_MODEL"]

    df = load(args.input, args.limit)
    print(f"Loaded {len(df)} emails from {args.input}; model = {model}")

    cache = {}
    if args.resume and args.raw.exists():
        with open(args.raw, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                if not rec["error"]:
                    cache[rec["email_id"]] = rec["raw_response"]
        print(f"Resuming: {len(cache)} emails already done will be reused")

    rows, raw_records = [], []
    try:
        for n, (_, row) in enumerate(df.iterrows(), start=1):
            result, raw, error, reused = None, "", None, False
            if row["email_id"] in cache:
                try:
                    raw = cache[row["email_id"]]
                    result, reused = ExtractionResult(**parse_json(raw)), True
                except (json.JSONDecodeError, ValidationError, TypeError):
                    result = None  # cached answer unusable; call the API again
            if result is None:
                result, raw, error = extract(client, model, row)
                if args.delay:
                    time.sleep(args.delay)
            rows.extend(flatten_to_rows(row, result))
            raw_records.append({"email_id": row["email_id"], "raw_response": raw, "error": error})
            status = "failed" if error else f"{len(result.payments)} payment(s)"
            tag = " (cached)" if reused else ""
            print(f"[{n}/{len(df)}] {row['email_id']}: {status}{tag}" + (f" ({error})" if error else ""))
    except KeyboardInterrupt:
        print("\nInterrupted: saving what has been processed so far.")

    out = save(rows, raw_records, args.output, args.raw)
    print(f"\nWrote {args.output} and {args.raw}")
    report(out)
    spot_check(out)


if __name__ == "__main__":
    main()