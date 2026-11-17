# Client Payment Email Extractor

Extracts payment instructions from client emails (exported to CSV) into a structured payment table, using OpenRouter for the LLM call, Pydantic for validation, and pandas for I/O. Each row in the output is one payment.

The extracted account details are meant to be checked later against our own records, to see whether an account is new or recognised.

## Files

| File | Purpose |
|---|---|
| `email_parser.py` | The extractor |
| `payments_extracted.csv` | Output payment table, one row per payment |
| `raw_responses.jsonl` | Raw LLM response per email, for audit |
| `data/Client Payment Emails, Shared Operations Inbox Export (Sep–Nov 2026).csv` | Input emails |

## How to run

```bash
pip install openai pydantic pandas python-dotenv
```

Put your settings in a `.env` file next to the script (and keep `.env` out of git):

```
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=`~deepseek/deepseek-v4-flash-latest` due to free model rate-limits
```

Then:

```bash
python email_parser.py --limit 5            # trial run on the first 5 emails
python email_parser.py                      # all emails
python email_parser.py --delay 4            # pause 4s between calls (rate limits)
python email_parser.py --resume             # reuse good results, retry only failed emails
```

`--resume` reads `raw_responses.jsonl`, so use it with the same model as the original run.

## Output columns

The original email columns (`email_id`, `received_at`, `client_id`, `client_name`, `sender_email`, `subject`, `email_body`, `is_forwarded_thread`) are carried through unchanged, followed by:

| Column | Notes |
|---|---|
| `payment_seq` | 1, 2, ... for payments within one email |
| `amount` | Numeric only |
| `currency` | 3-letter ISO code, separate from amount |
| `payee_name` | The party being paid |
| `payment_date` | Optional; null if the email gives no date |
| `date_text` | Raw date phrasing, e.g. "ASAP" |
| `account_number` | Exactly as written; full IBAN where applicable |
| `sort_code` | Separate column; null if none |
| `prior_account_number`, `prior_sort_code` | The old account, when a new one replaces it |
| `reference` | Payment or invoice reference |
| `status` | `ok`, `no_payment_found`, or `extraction_failed` |

## Design decisions

- **One payment per row.** An email with several payments produces several rows, with the email-level columns repeated.
- **No invented dates.** `payment_date` is filled only when the email states a date, or a relative date that can be resolved from `received_at`. It is never defaulted to today or the next business day, because an analyst could read a made-up date as urgent. Vague wording goes in `date_text`.
- **Old and new accounts are both kept**, including an old account that appears only in a quoted or forwarded message, so analysts can verify with the client on a call-back.
- **Account details are never normalised** and are read and written as strings, so leading zeros and formatting survive.
- **Null means "not stated".** Extraction failures are recorded in `status`, not as nulls.
- **Failures are per email.** A bad response or validation error marks that email `extraction_failed` instead of crashing the run.
- **Retries.** Rate-limit errors (429) are retried with a growing wait, and empty model replies are retried automatically.

## The prompt

This is the prompt given to Claude Code to write and run the extractor:

````
Write `email_parser.py` to extract payment instructions from client emails, then run it on all 300 emails.

The emails are in `data\Client Payment Emails, Shared Operations Inbox Export (Sep–Nov 2026).csv` (note the spaces, comma, and en dash in the name). Read the file first to see the real data. Columns: email_id, received_at, client_id, client_name, sender_email, subject, email_body, is_forwarded_thread.

Use OpenRouter (openai SDK, base_url https://openrouter.ai/api/v1), Pydantic, and pandas. Model from the OPENROUTER_MODEL env var, key from OPENROUTER_API_KEY (never hardcode it).

For each email, extract from email_body: amount, currency, payee_name, payment_date, date_text, account_number, sort_code, prior_account_number, prior_sort_code, reference. Carry the original email columns through to the output, and add a status column (ok / no_payment_found / extraction_failed).

Rules:
- Amount and currency are separate columns. Amount is numeric only.
- Account number and sort code are always separate columns. Extract them exactly as written, with no digits dropped or normalised, because they'll be checked against our records. Read and write them as strings.
- payment_date is optional: use the date in the email, or a relative date resolved from received_at, otherwise null. Never default to today or the next business day, because an analyst could read a made-up date as urgent. Put vague phrasing like "ASAP" in date_text.
- If an email asks to pay to a new account, keep both the old and new accounts, including an old one that appears only in a quoted/forwarded message. Don't mistake an account in quoted text for the instructed one.
- If an email has more than one payment, output one row per payment, repeating the email-level columns. Never put multiple payments in one row.
- Anything not stated is null. Never guess. Failures go in status, not in nulls.

Implementation: one file, with functions for load, build_prompt, extract, flatten_to_rows, and save. Validate with Pydantic (amount > 0, 3-letter currency, parsed date). Ask the LLM for JSON only, temperature 0, and strip code fences. Handle failures per email by writing a row with status = extraction_failed instead of crashing. Also save each email's raw LLM response to a .jsonl file for audit.

Process:
1. Run it on the first 5 emails and show me the output. Fix any problems before continuing.
2. Then run it on all 300 emails and write the results to a CSV.
3. Report the counts of ok / no_payment_found / extraction_failed, and how many emails produced more than one row.
4. Spot-check the hard cases and tell me what you find: emails with no date, forwarded threads, emails with a new/changed account, and emails with multiple payments. Flag anything that looks wrong rather than silently fixing it.
````

## Results

Full run over all 300 emails:

| Metric | Count |
|---|---|
| Emails processed | 300 |
| `ok` | 300 |
| `no_payment_found` | 0 |
| `extraction_failed` | 0 |
| Emails producing more than one row | 2 |
| Total output rows | 302 |

The first pass left 6 emails failed because the model returned an empty reply. Automatic retries on empty replies and the `--resume` option were added, and the rerun cleared all of them.

### Spot-check findings

The run reports four hard-case categories: 3 emails with no payment date, 75 forwarded threads, 17 with a prior (replaced) account, and 2 with multiple payments. The script prints the first 8 rows of each category, so the notes below cover those rows only.

- **No date (3 emails).** EM-00015 states no fixed date, and the phrase is kept in `date_text` with `payment_date` left empty. In EM-00021 and EM-00291, the second payment (Dataform Labs) has neither a date nor date text. No dates were invented, as intended.
- **Forwarded threads (75 emails).** Some have a prior account populated (e.g. EM-00012, EM-00024). Others correctly have none, such as EM-00004, where the account quoted in the thread is the same IBAN as the instructed one.
- **Changed accounts (17 emails).** Both the new and prior accounts are kept. In several of the rows reviewed (EM-00024, EM-00036, EM-00048, EM-00072, EM-00088, EM-00104) the new account differs from the prior one by only one or two digits. Near-identical numbers can mean a typo or a spoofed change request, so these should go to call-back verification.
- **Shared accounts across payees.** EM-00012 (Crescent Linen Services) and EM-00025 (Willamette Hop Farms) give the same new account (6655-220-1193) and the same prior account (1120-443-8876) for two different payees. This is worth a manual check.
- **Multiple payments (2 emails).** EM-00021 and EM-00291 each contain two payments (Caleb Ruthven Consulting and Dataform Labs) and produce two rows each. The same two accounts appear in both emails.

