"""Shared logic for the payment review page and the holdout check.

Nothing here imports Streamlit. It reuses the batch scripts, so one pasted email
is treated exactly like a row in the batch pipeline:

    email_parser.extract / flatten_to_rows   LLM extraction through OpenRouter
    hold_release_check (join, validate...)   the six hold rules as a GE contract
    load_queue.map_rows                      the mapping to the queue row

The verdict comes only from the code rules, never from model text. Anything
that goes wrong ends as HELD with an error, never as READY.
"""
import os
import re
import tempfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI

import email_parser
import hold_release_check as hrc
import load_queue as lq

ACCOUNTS_PATH = hrc.DEFAULT_ACCOUNTS
PLACEHOLDER_ID = "EM-99999"  # only used inside the pipeline when no EMAIL_ID is given
SECRET_VARS = ["OPENROUTER_API_KEY", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"]
EMAIL_COLUMNS = ["email_id", "received_at", "client_id", "client_name",
                 "sender_email", "subject", "email_body", "is_forwarded_thread"]

REASON_LABELS = {
    "bank_detail_change": "Bank detail change",
    "missing_required_field": "Missing required field",
    "invalid_amount": "Invalid amount",
    "invalid_payment_date": "Invalid payment date",
    "invalid_account_number": "Invalid account number",
    "unusable_model_output": "Could not read the email",
    "no_payment_found": "No payment found in the email",
    "cannot_queue": "Cannot be queued automatically",
    "processing_error": "Could not be processed",
    "none": "All six checks passed",
}
REASON_TEXT = {
    "bank_detail_change": (
        "The payment is going to an account that is not the verified account on file, or the email "
        "asks to replace an earlier account. Nothing on file has been verified since September, so a "
        "person must confirm the new details with the client by call-back before any money moves."),
    "missing_required_field": (
        "The payee, amount, currency or account number could not be read from the email."),
    "invalid_amount": "The amount is not a positive number with at most two decimal places.",
    "invalid_payment_date": "The payment date is not a valid date inside the allowed window.",
    "invalid_account_number": "The account number is longer than the queue allows.",
    "unusable_model_output": "The extraction did not produce a usable payment from this email.",
    "no_payment_found": "No payment instruction could be found in this email, so there is nothing to release.",
    "cannot_queue": ("The payment is held, but it has no matching queue hold reason, so the queue row cannot "
                     "be built automatically. A person needs to review it."),
    "processing_error": "Something went wrong while processing, so the payment is held to be safe.",
    "none": "The payment goes to the verified account on file, in the right currency, with no account change.",
}

CODE_TEXT = {  # why a payment is held when the queue has no hold reason for it
    "CLIENT_NOT_ON_FILE": "The client is not in the accounts-on-file sheet, so the account cannot be matched.",
    "CURRENCY_MISMATCH": "The currency differs from the account currency on file.",
    "SORT_CODE_MISMATCH": "The separate sort code does not agree with the account on file.",
}


# --------------------------------------------------------------------------
# Environment, secrets, inputs
# --------------------------------------------------------------------------
def init_env() -> None:
    load_dotenv()


def scrub(text: str) -> str:
    """Remove secret values from any text before it is shown or logged."""
    for name in SECRET_VARS:
        val = os.environ.get(name)
        if val:
            text = text.replace(val, "***")
    return text


def get_llm():
    """Return (client, model). Raises RuntimeError naming missing variables only."""
    missing = [v for v in ("OPENROUTER_API_KEY", "OPENROUTER_MODEL") if not os.environ.get(v)]
    if missing:
        raise RuntimeError(f"Set {', '.join(missing)} in the environment or .env file.")
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])
    return client, os.environ["OPENROUTER_MODEL"]


def load_accounts_checked(path: Path = ACCOUNTS_PATH):
    """Return (accounts_df | None, error message | None)."""
    try:
        acc, _, _ = hrc.load_accounts(path)
        return acc, None
    except SystemExit as e:  # hrc exits with a message when the sheet is missing
        return None, str(e.code)
    except Exception as e:
        return None, scrub(f"{type(e).__name__}: {e}")


def find_holdout_file(folder: Path = Path("data")) -> Path | None:
    found = sorted(folder.glob("[Hh]oldout*.csv"))
    return found[0] if found else None


def load_holdout(path: Path, include_gold: bool = False) -> pd.DataFrame:
    """The page never reads gold_* columns. Only the holdout check asks for them."""
    usecols = None if include_gold else (lambda c: not c.startswith("gold_"))
    df = pd.read_csv(path, dtype=str, keep_default_na=False, usecols=usecols)
    missing = [c for c in EMAIL_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{path} is missing columns: {missing}")
    return df


# --------------------------------------------------------------------------
# Display helpers
# --------------------------------------------------------------------------
def detect_quoted_thread(body: str) -> bool:
    patterns = [
        r"-{2,}\s*original message\s*-{2,}",
        r"-{2,}\s*forwarded message\s*-{2,}",
        r"begin forwarded message",
        r"^\s*>",
        r"\bon .{5,80}\bwrote:",
        r"\bfrom:\s*\S+@\S+.{0,120}?\b(sent|date|subject):",
    ]
    return any(re.search(p, body, re.I | re.M | re.S) for p in patterns)


def _counted_positions(s: str) -> list[int]:
    return [i for i, ch in enumerate(s) if ch not in " -"]


def diff_markers(a: str, b: str) -> tuple[str, str]:
    """Caret lines that sit under the characters where two accounts differ
    (spaces and dashes are ignored for the comparison)."""
    pa, pb = _counted_positions(a), _counted_positions(b)
    na, nb = "".join(a[i].upper() for i in pa), "".join(b[i].upper() for i in pb)
    ma, mb = [" "] * len(a), [" "] * len(b)
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, na, nb, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for k in range(i1, i2):
            ma[pa[k]] = "^"
        for k in range(j1, j2):
            mb[pb[k]] = "^"
    return "".join(ma).rstrip(), "".join(mb).rstrip()


def _clean_fatal(msg: str) -> str:
    """Drop the batch-file prefix ('CSV line 2 (EM-00312): ') that means nothing on this page."""
    return re.sub(r"^CSV line \d+( \([^)]*\))?: ", "", msg)


def md_safe(text: str) -> str:
    """Escape characters that Markdown would treat as formatting."""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|<>~$:])", r"\\\1", str(text))


def sender_signals(sender_email, body, client_name, onfile_name) -> list[dict]:
    """Display-only signals. They never change HELD or READY."""
    signals = []
    sender = (sender_email or "").strip().lower()
    if "@" in sender:
        sender_domain = sender.rsplit("@", 1)[1]
        quoted_domains = {m.group(2).lower() for m in re.finditer(
            r"from:\s*<?([A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,}))", body or "", re.I)}
        for d in sorted(quoted_domains - {sender_domain}):
            signals.append({"kind": "warning", "text": (
                f"Sender domain {sender_domain} differs from {d}, the domain on the From: line of the quoted "
                "earlier message. A change of domain on an email that asks for new bank details is a common "
                "sign of spoofing. Verify by phone with a known contact, not by replying.")})
    if client_name and onfile_name and hrc.norm_name(client_name) != hrc.norm_name(onfile_name):
        signals.append({"kind": "info", "text": (
            f"The client name on this email ({md_safe(client_name)}) differs from the on-file name for the "
            f"same Client ID ({md_safe(onfile_name)}). The two files are joined on Client ID only.")})
    return signals


# --------------------------------------------------------------------------
# Result objects
# --------------------------------------------------------------------------
@dataclass
class PaymentView:
    seq: str | None
    status: str
    extracted: dict
    onfile: dict | None
    codes: list
    rules: list
    queue_row: dict | None
    disposition: str
    hold_reason: str


@dataclass
class Analysis:
    error: str | None = None
    verdict: str = "HELD"
    hold_reason: str = "processing_error"
    email_id: str | None = None
    quoted_detected: bool = False
    quoted_used: bool = False
    raw_output: str = ""
    extraction_error: str | None = None
    payments: list = field(default_factory=list)
    queue_fatal: list = field(default_factory=list)
    queue_warnings: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    reason_detail: str = ""
    queue_in_table: list | None = None
    queue_lookup_message: str | None = None


# --------------------------------------------------------------------------
# The six rules as a checklist (derived from the codes the contract returned)
# --------------------------------------------------------------------------
def build_rules(r: dict, codes: list) -> list:
    status_ok = r.get("status") == "ok"
    has_acct = bool(r.get("account_number"))
    on_file = bool(r.get("onfile_client_id"))

    def item(n, title, fail_codes, pass_msg, fail_msg, applicable=True, na_msg=""):
        if not applicable:
            return {"#": n, "Rule": title, "Result": "- not checked", "Detail": na_msg}
        if any(c in codes for c in fail_codes):
            return {"#": n, "Rule": title, "Result": "FAIL", "Detail": fail_msg}
        return {"#": n, "Rule": title, "Result": "pass", "Detail": pass_msg}

    needs_account = "Needs a client on file and an extracted account."
    return [
        item(1, "Email was read and an account number is present", ["NOT_OK_STATUS", "MISSING_ACCOUNT"],
             "Extraction status ok and an account number was found.",
             (f"Extraction status is '{r.get('status')}', not 'ok'." if not status_ok
              else "No account number was found in the email.")),
        item(2, "Client is on file", ["CLIENT_NOT_ON_FILE"],
             f"Client {r.get('client_id')} found in the accounts sheet.",
             f"Client {r.get('client_id')} is not in the accounts sheet."),
        item(3, "Instructed account equals the account on file", ["ACCOUNT_MISMATCH"],
             "Matches the on-file account after cleaning both sides the same way.",
             f"Instructed {r.get('account_number')} does not equal the on-file {r.get('onfile_account_number')}.",
             applicable=on_file and has_acct and status_ok, na_msg=needs_account),
        item(4, "Currency matches the account currency on file", ["CURRENCY_MISMATCH"],
             f"{r.get('currency')} matches the on-file currency.",
             f"The email says {r.get('currency')}, the account on file is in {r.get('onfile_currency')}.",
             applicable=on_file and status_ok, na_msg="Needs a client on file."),
        item(5, "Separate sort code agrees with the IBAN on file", ["SORT_CODE_MISMATCH"],
             "The sort code agrees with the on-file IBAN.",
             f"Sort code {r.get('sort_code')} does not agree with the on-file account "
             "(or there is no UK IBAN on file to check it against).",
             applicable=on_file and has_acct and status_ok and bool(r.get("sort_code")),
             na_msg="No separate sort code in the email."),
        item(6, "No replacement account in the email", ["ACCOUNT_CHANGE_FLAGGED"],
             "The email does not replace an earlier account.",
             f"The email names a prior account ({r.get('prior_account_number')}) that this instruction replaces."),
    ]


# --------------------------------------------------------------------------
# Running the batch logic on one email
# --------------------------------------------------------------------------
def _hold_rules(rows: list[dict], accounts: pd.DataFrame) -> pd.DataFrame:
    # Same columns as the batch output, so a failed or empty extraction still has every column.
    df = pd.DataFrame(rows).reindex(columns=email_parser.OUTPUT_COLUMNS)
    for c in hrc.PAYMENT_REQUIRED:
        if c not in df.columns:
            df[c] = None
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "payments.csv"
            df.to_csv(p, index=False)
            pay = hrc.load_payments(p)  # same typing and cleaning as the batch run
        joined = hrc.join(pay, accounts, "id")  # joined on Client ID, as in the batch run
        joined = hrc.add_check_columns(joined)
        result = hrc.validate(joined)
        return hrc.apply_decisions(joined, result)
    except SystemExit as e:  # the batch helpers exit with a message
        raise RuntimeError(str(e.code))


def _queue_rows(joined: pd.DataFrame, email_id: str | None) -> list[dict]:
    """One entry per payment: {'row', 'fatal', 'findings'}. Mapped one at a time so each
    payment keeps its own result."""
    cols = ["email_id", "client_id", "payee_name", "payment_date", "amount", "currency",
            "account_number", "is_forwarded_thread", "status", "payment_seq", "decision", "hold_reasons"]
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "decisions.csv"
        joined[cols].to_csv(p, index=False)
        decisions = pd.read_csv(p, dtype=str, keep_default_na=False)
    out = []
    for i in range(len(decisions)):
        rows, findings, fatal, _ = lq.map_rows(decisions.iloc[[i]], "(assigned at load)",
                                               strict_email_id_range=False)
        row = rows[0] if rows else None
        if row is not None:
            row = {k: ("" if v is None else str(v).lower() if isinstance(v, bool) else str(v))
                   for k, v in row.items()}
            row["LOAD_RUN_ID"] = "(assigned at load)"
            if not email_id:
                row["EMAIL_ID"] = "(none given)"
        out.append({"row": row, "fatal": fatal, "findings": findings})
    return out


def analyse_email(form: dict, accounts: pd.DataFrame, llm_client, model: str,
                  forwarded: str = "auto", queue_lookup=None) -> Analysis:
    """form: client_id, client_name, received_at (ISO), sender_email, subject, email_body, email_id.
    forwarded: 'auto' | 'yes' | 'no'. Any failure returns an Analysis that is HELD."""
    a = Analysis()
    try:
        _analyse(a, form, accounts, llm_client, model, forwarded, queue_lookup)
    except BaseException as e:  # fail closed, whatever went wrong
        if isinstance(e, (KeyboardInterrupt,)):
            raise
        a.error = scrub(f"{type(e).__name__}: {e}")
        a.verdict, a.hold_reason, a.payments = "HELD", "processing_error", []
    return a


def _analyse(a, form, accounts, llm_client, model, forwarded, queue_lookup):
    body = (form.get("email_body") or "").strip()
    client_id = (form.get("client_id") or "").strip().upper()
    received_at = (form.get("received_at") or "").strip()
    if not body or not client_id or not received_at:
        raise ValueError("Client ID, received-at time and the email body are all required.")
    email_id = (form.get("email_id") or "").strip().upper() or None
    a.email_id = email_id

    a.quoted_detected = detect_quoted_thread(body)
    a.quoted_used = {"auto": a.quoted_detected, "yes": True, "no": False}[forwarded]

    row = pd.Series({
        "email_id": email_id or PLACEHOLDER_ID, "received_at": received_at, "client_id": client_id,
        "client_name": (form.get("client_name") or "").strip(),
        "sender_email": (form.get("sender_email") or "").strip(),
        "subject": (form.get("subject") or "").strip(), "email_body": body,
        "is_forwarded_thread": "true" if a.quoted_used else "false",
    })

    result, raw, err = email_parser.extract(llm_client, model, row)
    a.raw_output, a.extraction_error = raw, err
    joined = _hold_rules(email_parser.flatten_to_rows(row, result), accounts)
    queue = _queue_rows(joined, email_id)

    if email_id and re.fullmatch(r"EM-\d{5}", email_id) and not 1 <= int(email_id[3:]) <= 300:
        a.queue_warnings.append(
            f"{email_id} is outside EM-00001..EM-00300, the range the queue table documents. "
            "load_queue.py would refuse this ID. The row below is shown as it would be written.")

    for i, rec in enumerate(joined.to_dict("records")):
        rec = {k: (None if pd.isna(v) else v) for k, v in rec.items()}
        codes = [c for c in (rec.get("hold_reasons") or "").split("|") if c]
        q = queue[i]
        a.queue_fatal += [m for m in map(_clean_fatal, q["fatal"]) if m not in a.queue_fatal]
        if q["row"] is not None:
            disposition, reason = q["row"]["DISPOSITION"], q["row"]["HOLD_REASON"]
        else:
            disposition = "held"
            reason = "no_payment_found" if rec["status"] == "no_payment_found" else "cannot_queue"
            if reason == "cannot_queue":
                details = [CODE_TEXT[c] for c in codes if c in CODE_TEXT]
                a.reason_detail = " ".join(dict.fromkeys(details + ([a.reason_detail] if a.reason_detail else [])))
        onfile = None
        if rec.get("onfile_client_id"):
            onfile = {"Client ID": rec["onfile_client_id"], "Client name": rec.get("onfile_client_name"),
                      "Account on file": rec.get("onfile_account_number"), "Bank": rec.get("onfile_bank"),
                      "Currency": rec.get("onfile_currency"), "Last verified": rec.get("onfile_last_verified"),
                      "Call-back contact": rec.get("callback_contact")}
        extracted = {k: rec.get(k) for k in ("payee_name", "amount", "currency", "payment_date", "date_text",
                                             "account_number", "sort_code", "prior_account_number",
                                             "prior_sort_code", "reference")}
        a.payments.append(PaymentView(
            seq=rec.get("payment_seq"), status=rec["status"], extracted=extracted, onfile=onfile,
            codes=codes, rules=build_rules(rec, codes), queue_row=q["row"],
            disposition=disposition, hold_reason=reason))

    # READY only if every payment passes all six rules AND its queue row says ready.
    all_ready = bool(a.payments) and all(
        p.disposition == "ready" and not p.codes and p.queue_row is not None for p in a.payments)
    a.verdict = "READY" if all_ready and not a.queue_fatal else "HELD"
    held = [p.hold_reason for p in a.payments if p.hold_reason != "none"]
    a.hold_reason = "none" if a.verdict == "READY" else next(
        (r for r in lq.PRIORITY + ["no_payment_found", "cannot_queue"] if r in held), "cannot_queue")

    first_onfile = next((p.onfile for p in a.payments if p.onfile), None)
    a.signals = sender_signals(form.get("sender_email"), body, form.get("client_name"),
                               first_onfile["Client name"] if first_onfile else None)

    if queue_lookup and email_id:
        a.queue_in_table, a.queue_lookup_message = queue_lookup(email_id)


# --------------------------------------------------------------------------
# Read-only queue lookup
# --------------------------------------------------------------------------
def fetch_queue_rows(email_id: str):
    """SELECT only. Returns (rows | None, message). Never raises, and the verdict
    never depends on it."""
    try:
        conn = lq.connect()
    except SystemExit as e:
        return None, scrub(f"Snowflake is not available: {e.code}")
    except Exception as e:
        return None, scrub(f"Snowflake is not available ({type(e).__name__}).")
    try:
        cur = conn.cursor()
        cols = lq.QUEUE_COLUMNS + ["LOADED_AT"]
        cur.execute(f"SELECT {', '.join(cols)} FROM {lq.TABLE} WHERE EMAIL_ID = %s ORDER BY LOADED_AT",
                    (email_id,))
        return [dict(zip(cols, ["" if v is None else str(v) for v in r])) for r in cur.fetchall()], None
    except Exception as e:
        return None, scrub(f"Could not read the queue ({type(e).__name__}).")
    finally:
        conn.close()