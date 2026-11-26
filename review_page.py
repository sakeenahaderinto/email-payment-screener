"""Payment review page: paste one client email, see HELD or READY and why.

Run:
    pip install streamlit
    streamlit run review_page.py

Needs OPENROUTER_API_KEY and OPENROUTER_MODEL for extraction. SNOWFLAKE_ACCOUNT,
SNOWFLAKE_USER and SNOWFLAKE_PASSWORD are optional: they only let the page show the
email's row from the queue (read-only). Put them in the environment or a .env file.
The verdict is made by the code rules, never by the model, and never depends on Snowflake.
"""
from datetime import date, datetime, time

import pandas as pd
import streamlit as st

import load_queue as lq
import review_pipeline as rp

rp.init_env()
st.set_page_config(page_title="Payment review", page_icon=":money_with_wings:", layout="wide")

FORM_DEFAULTS = {
    "f_client_id": "", "f_client_name": "", "f_received_date": datetime.now().date(),
    "f_received_time": time(9, 0), "f_sender": "", "f_subject": "", "f_body": "",
    "f_email_id": "", "f_forwarded": "Auto-detect",
}
for k, v in FORM_DEFAULTS.items():
    st.session_state.setdefault(k, v)


# --------------------------------------------------------------------------
# Cached inputs
# --------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def cached_accounts():
    return rp.load_accounts_checked()


@st.cache_data(show_spinner=False)
def cached_holdout():
    path = rp.find_holdout_file()
    if path is None:
        return None, None, "No holdout CSV found in the data folder."
    try:
        return rp.load_holdout(path), path.name, None
    except Exception as e:
        return None, path.name, rp.scrub(f"{type(e).__name__}: {e}")


def fill_example() -> None:
    df, _, _ = cached_holdout()
    choice = st.session_state.get("example_choice")
    if df is None or not choice:
        return
    row = df[df["email_id"] == choice.split(" | ")[0]].iloc[0]
    try:
        received = datetime.fromisoformat(row["received_at"])
    except ValueError:
        received = datetime.now()
    st.session_state.update({
        "f_client_id": row["client_id"], "f_client_name": row["client_name"],
        "f_received_date": received.date(), "f_received_time": received.time().replace(microsecond=0),
        "f_sender": row["sender_email"], "f_subject": row["subject"], "f_body": row["email_body"],
        "f_email_id": row["email_id"], "f_forwarded": "Auto-detect",
    })
    st.session_state.pop("analysis", None)


# --------------------------------------------------------------------------
# Rendering (email-derived text only ever goes to plain-text widgets)
# --------------------------------------------------------------------------
def show(value) -> str:
    return "not stated" if value in (None, "") else str(value)


def render_payment(p: rp.PaymentView) -> None:
    st.caption(f"This payment: {p.disposition.upper()} ({rp.REASON_LABELS.get(p.hold_reason, p.hold_reason)})")
    e = p.extracted

    st.subheader("Extracted payment")
    date_text = f" (email said: {e['date_text']})" if e.get("date_text") else ""
    st.dataframe(pd.DataFrame({
        "Field": ["Payee", "Amount", "Currency", "Payment date", "Account number", "Sort code", "Reference"],
        "Value": [show(e["payee_name"]), show(e["amount"]), show(e["currency"]),
                  show(e["payment_date"]) + (date_text if e.get("payment_date") is None else ""),
                  show(e["account_number"]), show(e["sort_code"]), show(e["reference"])],
    }), hide_index=True)

    st.subheader("Account comparison")
    new, prior = e.get("account_number"), e.get("prior_account_number")
    lines = [f"instructed  {show(new)}"]
    if new and prior:
        m_new, m_prior = rp.diff_markers(new, prior)
        lines += [f"            {m_new}", f"prior       {prior}", f"            {m_prior}"]
    elif prior:
        lines.append(f"prior       {prior}")
    else:
        lines.append("prior       (no replaced account in the email)")
    st.code("\n".join(lines), language=None)
    if new and prior:
        st.caption("^ marks the characters where the instructed and prior accounts differ.")
    if p.onfile:
        st.code(f"on file     {show(p.onfile['Account on file'])}", language=None)
        st.dataframe(pd.DataFrame({"Field": list(p.onfile), "Value": [show(v) for v in p.onfile.values()]}),
                     hide_index=True)
    else:
        st.warning("This client is not in the accounts-on-file sheet, so nothing can be matched.")

    st.subheader("The six rules")
    st.dataframe(pd.DataFrame(p.rules), hide_index=True)


def render_queue(a: rp.Analysis) -> None:
    st.subheader("Queue row")
    for w in a.queue_warnings:
        st.warning(w)
    if a.queue_fatal:
        st.error("The queue row cannot be built for this email:")
        st.text("\n".join(a.queue_fatal))
    proposed = [p.queue_row for p in a.payments if p.queue_row]

    if a.queue_in_table:
        st.success(f"In the queue: {len(a.queue_in_table)} row(s) found for {a.email_id}.")
        st.dataframe(pd.DataFrame(a.queue_in_table), hide_index=True)
        return
    if a.queue_lookup_message:
        st.info(a.queue_lookup_message + " The verdict above does not depend on it.")
    elif a.email_id and a.queue_in_table is not None:
        st.info(f"{a.email_id} is not in the queue yet.")
    elif not a.email_id:
        st.info("No email ID was given, so the queue was not checked.")
    if proposed:
        st.caption("Not in the queue. This is the row the loader would write:")
        st.dataframe(pd.DataFrame(proposed), hide_index=True)


def render(a: rp.Analysis) -> None:
    st.divider()
    label = rp.REASON_LABELS.get(a.hold_reason, a.hold_reason)
    if a.verdict == "READY":
        st.success(f"**READY**: {label}")
    else:
        st.error(f"**HELD**: {label}")
    st.write(rp.REASON_TEXT.get(a.hold_reason, ""))
    if a.reason_detail:
        st.write(a.reason_detail)

    if a.error:
        st.error("This email could not be processed, so it is held to be safe.")
        st.text(a.error)
        return

    other = sorted({c for p in a.payments for c in p.codes})
    if other:
        st.caption("Rules failed: " + ", ".join(other))
    st.caption(f"Quoted earlier message: detected = {a.quoted_detected}, used = {a.quoted_used}")

    if len(a.payments) > 1:
        st.info(f"This email contains {len(a.payments)} payments. Each is checked and queued separately.")
        for tab, p in zip(st.tabs([f"Payment {i + 1}" for i in range(len(a.payments))]), a.payments):
            with tab:
                render_payment(p)
    elif a.payments:
        render_payment(a.payments[0])

    render_queue(a)

    if a.signals:
        st.subheader("Signals (information only, they never change the verdict)")
        for s in a.signals:
            (st.warning if s["kind"] == "warning" else st.info)(s["text"])

    with st.expander("Raw model output (audit)"):
        if a.extraction_error:
            st.text(f"Extraction error: {a.extraction_error}")
        st.code(a.raw_output or "(empty)", language="json")


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
st.title("Payment review")
st.caption("Paste one client email. The page runs it through the same extraction, hold rules and "
           "queue mapping as the batch pipeline.")
if not lq.env_is_gitignored():
    st.warning(".env is not listed in .gitignore. Add it before committing anything.")

accounts, accounts_error = cached_accounts()
if accounts_error:
    st.error("The accounts-on-file sheet could not be loaded, so every email will be held.")
    st.text(accounts_error)

holdout, holdout_name, holdout_error = cached_holdout()
if holdout is not None:
    with st.container(border=True):
        st.markdown("**Load an example** from the holdout emails")
        labels = [f"{r.email_id} | {r.client_name} | {r.subject}" for r in holdout.itertuples()]
        st.selectbox("Holdout email", labels, key="example_choice", label_visibility="collapsed")
        st.button("Load example", on_click=fill_example)
        st.caption(f"Source: {holdout_name}. Answer-key columns are never read by this page.")
elif holdout_error:
    st.caption(f"Examples unavailable: {holdout_error}")

with st.form("review_form"):
    c1, c2, c3 = st.columns(3)
    c1.text_input("Client ID", key="f_client_id", placeholder="CL-1001")
    c2.text_input("Client name (optional)", key="f_client_name")
    c3.text_input("Email ID (optional)", key="f_email_id", placeholder="EM-00311")
    c4, c5 = st.columns(2)
    c4.date_input("Received on", key="f_received_date")
    c5.time_input("Received at", key="f_received_time")
    st.text_input("Sender email", key="f_sender")
    st.text_input("Subject", key="f_subject")
    st.text_area("Email body", key="f_body", height=220)
    st.radio("Contains a quoted earlier message?", ["Auto-detect", "Yes", "No"], key="f_forwarded",
             horizontal=True)
    submitted = st.form_submit_button("Review payment", type="primary")

if submitted:
    form = {
        "client_id": st.session_state["f_client_id"], "client_name": st.session_state["f_client_name"],
        "received_at": datetime.combine(st.session_state["f_received_date"],
                                        st.session_state["f_received_time"]).isoformat(timespec="seconds"),
        "sender_email": st.session_state["f_sender"], "subject": st.session_state["f_subject"],
        "email_body": st.session_state["f_body"], "email_id": st.session_state["f_email_id"],
    }
    mode = {"Auto-detect": "auto", "Yes": "yes", "No": "no"}[st.session_state["f_forwarded"]]
    if accounts is None:
        st.session_state["analysis"] = rp.Analysis(error=accounts_error or "Accounts file unavailable.")
    else:
        try:
            llm_client, model = rp.get_llm()
        except RuntimeError as e:
            st.session_state["analysis"] = rp.Analysis(error=str(e))
        else:
            with st.spinner("Reading the email and running the checks..."):
                st.session_state["analysis"] = rp.analyse_email(
                    form, accounts, llm_client, model, forwarded=mode, queue_lookup=rp.fetch_queue_rows)

if st.session_state.get("analysis") is not None:
    render(st.session_state["analysis"])