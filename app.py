"""
Bulk Email Validator — Streamlit App
-------------------------------------
Upload an Excel file of email addresses, check each one, and download
separate "Valid" and "Inactive/Invalid" (plus "Unknown/Risky") lists.

Checks performed (in order, each stage only runs if the previous passes):
  1. Syntax check          — is it a well-formed email address?
  2. Disposable check       — is the domain a known temp-mail provider? (optional)
  3. MX record check        — does the domain actually accept mail?
  4. SMTP handshake check   — does the mail server accept the specific mailbox? (optional)

IMPORTANT — read this before you trust "SMTP handshake":
Many networks (including most cloud hosts and some ISPs) block outbound
port 25, which the SMTP check needs. If that happens, results for that
stage come back "Unknown" instead of a false Valid/Invalid — the app
never guesses. Syntax + MX checks work virtually everywhere and already
catch the vast majority of typos, fake domains, and dead addresses.
"""

import concurrent.futures
import re
import smtplib
import socket
import time
from dataclasses import dataclass, field
from io import BytesIO

import dns.resolver
import pandas as pd
import streamlit as st

# --------------------------------------------------------------------------
# Config / constants
# --------------------------------------------------------------------------

EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+\/=?^_`{|}~-]+"
    r"@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)

# A small built-in list of common disposable/temp-mail domains.
# Not exhaustive, but catches the most common ones seen in signup forms.
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "throwawaymail.com", "yopmail.com", "fakeinbox.com",
    "trashmail.com", "getnada.com", "maildrop.cc", "sharklasers.com",
    "dispostable.com", "mintemail.com", "mailnesia.com", "spam4.me",
    "emailondeck.com", "moakt.com", "tempinbox.com", "burnermail.io",
}

STATUS_VALID = "Valid"
STATUS_INVALID = "Invalid / Inactive"
STATUS_UNKNOWN = "Unknown / Risky"

DEFAULT_SMTP_TIMEOUT = 8
DEFAULT_MAX_WORKERS = 15


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class CheckResult:
    email: str
    status: str
    reason: str
    mx_host: str = ""
    checks: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Core validation logic
# --------------------------------------------------------------------------

def check_syntax(email: str) -> bool:
    return bool(EMAIL_REGEX.match(email.strip()))


def get_domain(email: str) -> str:
    return email.strip().split("@")[-1].lower()


def is_disposable(domain: str) -> bool:
    return domain in DISPOSABLE_DOMAINS


_mx_cache: dict[str, tuple] = {}


def get_mx_records(domain: str, timeout: float = 5.0):
    """Return (best_mx_host, all_hosts) or (None, []) if none found. Cached per domain."""
    if domain in _mx_cache:
        return _mx_cache[domain]
    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = timeout
        resolver.lifetime = timeout
        answers = resolver.resolve(domain, "MX")
        records = sorted(answers, key=lambda r: r.preference)
        hosts = [str(r.exchange).rstrip(".") for r in records]
        result = (hosts[0] if hosts else None, hosts)
    except Exception:
        result = (None, [])
    _mx_cache[domain] = result
    return result


def smtp_probe(email: str, mx_host: str, sender: str, timeout: float):
    """
    Attempt an SMTP handshake up to RCPT TO without sending a message.
    Returns one of: "accepted", "rejected", "unknown"
    """
    try:
        server = smtplib.SMTP(timeout=timeout)
        server.connect(mx_host)
        server.helo(server.local_hostname or "checker.local")
        server.mail(sender)
        code, _ = server.rcpt(email)
        server.quit()
        if code in (250, 251):
            return "accepted"
        if code in (550, 551, 553, 554):
            return "rejected"
        return "unknown"
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
            socket.timeout, socket.gaierror, ConnectionRefusedError, OSError):
        return "unknown"
    except Exception:
        return "unknown"


def verify_one(email: str, do_smtp: bool, do_disposable: bool,
                smtp_sender: str, smtp_timeout: float) -> CheckResult:
    email = str(email).strip()
    checks = {}

    if not email or email.lower() == "nan":
        return CheckResult(email, STATUS_INVALID, "Empty value", checks=checks)

    # 1. Syntax
    if not check_syntax(email):
        checks["syntax"] = "fail"
        return CheckResult(email, STATUS_INVALID, "Invalid syntax", checks=checks)
    checks["syntax"] = "pass"

    domain = get_domain(email)

    # 2. Disposable (optional)
    if do_disposable and is_disposable(domain):
        checks["disposable"] = "yes"
        return CheckResult(email, STATUS_INVALID, "Disposable / temp-mail domain", checks=checks)

    # 3. MX records
    mx_host, all_hosts = get_mx_records(domain)
    if not mx_host:
        checks["mx"] = "fail"
        return CheckResult(email, STATUS_INVALID, "No MX records — domain can't receive mail", checks=checks)
    checks["mx"] = "pass"

    # 4. SMTP handshake (optional)
    if do_smtp:
        outcome = smtp_probe(email, mx_host, smtp_sender, smtp_timeout)
        checks["smtp"] = outcome
        if outcome == "accepted":
            return CheckResult(email, STATUS_VALID, "Mailbox accepted by server", mx_host, checks)
        if outcome == "rejected":
            return CheckResult(email, STATUS_INVALID, "Mailbox rejected by server", mx_host, checks)
        return CheckResult(email, STATUS_UNKNOWN,
                            "SMTP check blocked/inconclusive (server didn't respond clearly — "
                            "common when port 25 is filtered)", mx_host, checks)

    # No SMTP check requested — MX-valid is as far as we go
    return CheckResult(email, STATUS_VALID, "Valid syntax + domain accepts mail (MX only, no mailbox check)",
                        mx_host, checks)


def run_batch(emails: list[str], do_smtp: bool, do_disposable: bool,
              smtp_sender: str, smtp_timeout: float, max_workers: int,
              progress_callback=None) -> list[CheckResult]:
    results = [None] * len(emails)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(verify_one, e, do_smtp, do_disposable, smtp_sender, smtp_timeout): i
            for i, e in enumerate(emails)
        }
        done = 0
        total = len(emails)
        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                results[idx] = CheckResult(emails[idx], STATUS_UNKNOWN, f"Checker error: {exc}")
            done += 1
            if progress_callback:
                progress_callback(done, total)
    return results


# --------------------------------------------------------------------------
# Excel helpers
# --------------------------------------------------------------------------

def to_excel_bytes(df: pd.DataFrame, sheet_name: str = "Sheet1") -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
        worksheet = writer.sheets[sheet_name]
        for i, col in enumerate(df.columns):
            width = max(df[col].astype(str).map(len).max() if len(df) else 0, len(col)) + 2
            worksheet.set_column(i, i, min(width, 60))
    return buffer.getvalue()


def combined_excel_bytes(full_df: pd.DataFrame, valid_df: pd.DataFrame,
                          invalid_df: pd.DataFrame, unknown_df: pd.DataFrame) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        for name, d in [("All Results", full_df), ("Valid", valid_df),
                        ("Invalid", invalid_df), ("Unknown", unknown_df)]:
            d.to_excel(writer, index=False, sheet_name=name)
            ws = writer.sheets[name]
            for i, col in enumerate(d.columns):
                width = max(d[col].astype(str).map(len).max() if len(d) else 0, len(col)) + 2
                ws.set_column(i, i, min(width, 60))
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------

st.set_page_config(page_title="Email Toolkit", page_icon="📧", layout="centered")

st.title("📧 Email Toolkit")
st.caption("Two tools: validate a list of emails, or strip duplicates out of one.")

tab_validate, tab_dedupe = st.tabs(["✅ Email Validator", "🧹 Duplicate Remover"])

# ==========================================================================
# TAB 1 — Email Validator
# ==========================================================================
with tab_validate:
    st.subheader("Bulk Email Validator")
    st.caption("Upload an Excel file of email addresses → get back a Valid list and an Inactive/Invalid list.")

    with st.expander("How this works / what each check means", expanded=False):
        st.markdown(
            """
- **Syntax check** — catches typos and malformed addresses (always on).
- **Disposable check** — flags known temp-mail domains like `mailinator.com` (optional).
- **MX record check** — confirms the domain actually has a mail server set up to receive email (always on).
- **SMTP handshake check** — connects to the real mail server and asks "does this mailbox exist?" without
  sending anything (optional, off by default).

⚠️ **About the SMTP check:** many networks — including most cloud platforms — block outbound
port 25, which this check needs. When that happens, the app reports the address as **Unknown**
rather than guessing. Syntax + MX checks alone already catch typos, dead domains, and most fake
addresses reliably in any environment.
            """
        )

    uploaded_file = st.file_uploader("Upload Excel file (.xlsx or .xls)", type=["xlsx", "xls"], key="validator_upload")

    if uploaded_file:
        try:
            df_raw = pd.read_excel(uploaded_file)
        except Exception as e:
            st.error(f"Couldn't read that file: {e}")
            st.stop()

        if df_raw.empty:
            st.warning("The uploaded file has no rows.")
            st.stop()

        st.success(f"Loaded {len(df_raw)} rows with columns: {', '.join(map(str, df_raw.columns))}")

        # Guess the email column
        guessed_col = None
        for col in df_raw.columns:
            if "email" in str(col).lower():
                guessed_col = col
                break
        if guessed_col is None:
            guessed_col = df_raw.columns[0]

        email_col = st.selectbox(
            "Which column contains the email addresses?",
            options=list(df_raw.columns),
            index=list(df_raw.columns).index(guessed_col),
            key="validator_col",
        )

        st.subheader("Settings")
        col1, col2 = st.columns(2)
        with col1:
            do_disposable = st.checkbox("Flag disposable / temp-mail domains", value=True)
            do_smtp = st.checkbox("Run SMTP mailbox check (slower, may be blocked on some networks)", value=False)
        with col2:
            max_workers = st.slider("Parallel workers", min_value=1, max_value=40, value=DEFAULT_MAX_WORKERS)
            smtp_timeout = st.slider("SMTP timeout (seconds)", min_value=3, max_value=20, value=DEFAULT_SMTP_TIMEOUT,
                                      disabled=not do_smtp)

        smtp_sender = "verify@example.com"
        if do_smtp:
            smtp_sender = st.text_input(
                "Sender address to use for the SMTP handshake (any address, doesn't need to be real)",
                value="verify@example.com",
            )

        run = st.button("Run validation", type="primary")

        if run:
            emails = df_raw[email_col].astype(str).tolist()
            total = len(emails)

            progress_bar = st.progress(0, text=f"Checking 0 / {total}...")
            status_text = st.empty()
            start_time = time.time()

            def update_progress(done, total):
                progress_bar.progress(done / total, text=f"Checking {done} / {total}...")

            results = run_batch(
                emails, do_smtp, do_disposable, smtp_sender, smtp_timeout, max_workers,
                progress_callback=update_progress,
            )

            elapsed = time.time() - start_time
            progress_bar.progress(1.0, text=f"Done — checked {total} emails in {elapsed:.1f}s")

            # Build result dataframe, preserving all original columns
            result_df = df_raw.copy()
            result_df["Status"] = [r.status for r in results]
            result_df["Reason"] = [r.reason for r in results]
            result_df["MX Host"] = [r.mx_host for r in results]

            valid_df = result_df[result_df["Status"] == STATUS_VALID].reset_index(drop=True)
            invalid_df = result_df[result_df["Status"] == STATUS_INVALID].reset_index(drop=True)
            unknown_df = result_df[result_df["Status"] == STATUS_UNKNOWN].reset_index(drop=True)

            st.subheader("Results")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total", total)
            m2.metric("✅ Valid", len(valid_df))
            m3.metric("❌ Invalid / Inactive", len(invalid_df))
            m4.metric("❓ Unknown", len(unknown_df))

            st.dataframe(result_df, use_container_width=True, height=350)

            st.subheader("Download")
            d1, d2, d3 = st.columns(3)
            with d1:
                st.download_button(
                    "⬇️ Valid emails (.xlsx)",
                    data=to_excel_bytes(valid_df, "Valid"),
                    file_name="valid_emails.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            with d2:
                st.download_button(
                    "⬇️ Invalid / Inactive emails (.xlsx)",
                    data=to_excel_bytes(invalid_df, "Invalid"),
                    file_name="invalid_emails.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            with d3:
                st.download_button(
                    "⬇️ Full report, all tabs (.xlsx)",
                    data=combined_excel_bytes(result_df, valid_df, invalid_df, unknown_df),
                    file_name="email_validation_report.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )

            if len(unknown_df):
                st.info(
                    f"{len(unknown_df)} address(es) came back Unknown — usually because the SMTP check "
                    "was blocked or the mail server didn't give a clear answer. These are in the "
                    "'Unknown' tab of the full report; treat them as risky-but-not-confirmed-dead."
                )
    else:
        st.info("Upload an Excel file to get started. It should have a column of email addresses "
                "(any column name containing 'email' will be auto-detected).")

# ==========================================================================
# TAB 2 — Duplicate Remover
# ==========================================================================
with tab_dedupe:
    st.subheader("Duplicate Email Remover")
    st.caption("Upload a list of emails → get back a de-duplicated list, plus a report of what was removed.")

    with st.expander("How this works", expanded=False):
        st.markdown(
            """
- Compares addresses **case-insensitively** and trims stray whitespace by default
  (`John@Gmail.com` and `john@gmail.com ` count as the same address) — this is a toggle.
- Keeps the **first occurrence** of each address (in the order it appears in your file) and
  drops the rest.
- Every other column in your row is preserved for the kept record.
- You get back three things: the de-duplicated list, a report of just the removed duplicate
  rows, and a full report with both as separate tabs.
            """
        )

    dedupe_file = st.file_uploader("Upload Excel file (.xlsx or .xls)", type=["xlsx", "xls"], key="dedupe_upload")

    if dedupe_file:
        try:
            dedupe_raw = pd.read_excel(dedupe_file)
        except Exception as e:
            st.error(f"Couldn't read that file: {e}")
            st.stop()

        if dedupe_raw.empty:
            st.warning("The uploaded file has no rows.")
            st.stop()

        st.success(f"Loaded {len(dedupe_raw)} rows with columns: {', '.join(map(str, dedupe_raw.columns))}")

        guessed_col = None
        for col in dedupe_raw.columns:
            if "email" in str(col).lower():
                guessed_col = col
                break
        if guessed_col is None:
            guessed_col = dedupe_raw.columns[0]

        dedupe_col = st.selectbox(
            "Which column contains the email addresses?",
            options=list(dedupe_raw.columns),
            index=list(dedupe_raw.columns).index(guessed_col),
            key="dedupe_col",
        )

        case_insensitive = st.checkbox("Treat different capitalization as the same address (recommended)", value=True)
        strip_dots_gmail = st.checkbox(
            "Also treat Gmail dot-variants as duplicates (e.g. john.doe@gmail.com = johndoe@gmail.com)",
            value=False,
            help="Gmail ignores dots in the local part of the address, so these actually deliver to the same inbox. "
                 "Off by default since it's Gmail-specific behavior, not a universal email rule.",
        )

        run_dedupe = st.button("Remove duplicates", type="primary")

        if run_dedupe:
            work_df = dedupe_raw.copy()
            raw_emails = work_df[dedupe_col].astype(str)

            def normalize(addr: str) -> str:
                addr = addr.strip()
                if case_insensitive:
                    addr = addr.lower()
                if strip_dots_gmail and "@" in addr:
                    local, _, domain = addr.partition("@")
                    if domain in ("gmail.com", "googlemail.com"):
                        local = local.split("+")[0].replace(".", "")
                        addr = f"{local}@{domain}"
                return addr

            work_df["_normalized_key"] = raw_emails.map(normalize)

            is_dup = work_df.duplicated(subset="_normalized_key", keep="first")
            unique_df = work_df[~is_dup].drop(columns="_normalized_key").reset_index(drop=True)
            duplicates_df = work_df[is_dup].drop(columns="_normalized_key").reset_index(drop=True)

            st.subheader("Results")
            r1, r2, r3 = st.columns(3)
            r1.metric("Total rows", len(work_df))
            r2.metric("✅ Unique (kept)", len(unique_df))
            r3.metric("🗑️ Duplicates (removed)", len(duplicates_df))

            st.markdown("**Unique list preview**")
            st.dataframe(unique_df, use_container_width=True, height=250)

            if len(duplicates_df):
                st.markdown("**Removed duplicates preview**")
                st.dataframe(duplicates_df, use_container_width=True, height=200)

            st.subheader("Download")
            dd1, dd2, dd3 = st.columns(3)
            with dd1:
                st.download_button(
                    "⬇️ Unique emails (.xlsx)",
                    data=to_excel_bytes(unique_df, "Unique"),
                    file_name="unique_emails.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            with dd2:
                st.download_button(
                    "⬇️ Removed duplicates (.xlsx)",
                    data=to_excel_bytes(duplicates_df, "Duplicates"),
                    file_name="removed_duplicates.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                    disabled=len(duplicates_df) == 0,
                )
            with dd3:
                dedupe_buffer = BytesIO()
                with pd.ExcelWriter(dedupe_buffer, engine="xlsxwriter") as writer:
                    for name, d in [("Unique", unique_df), ("Removed Duplicates", duplicates_df)]:
                        d.to_excel(writer, index=False, sheet_name=name)
                        ws = writer.sheets[name]
                        for i, col in enumerate(d.columns):
                            width = max(d[col].astype(str).map(len).max() if len(d) else 0, len(col)) + 2
                            ws.set_column(i, i, min(width, 60))
                st.download_button(
                    "⬇️ Full report, both tabs (.xlsx)",
                    data=dedupe_buffer.getvalue(),
                    file_name="deduplication_report.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )

            if len(duplicates_df) == 0:
                st.info("No duplicates found — your list was already clean.")
    else:
        st.info("Upload an Excel file to get started. It should have a column of email addresses "
                "(any column name containing 'email' will be auto-detected).")
