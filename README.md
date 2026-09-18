# Email Toolkit (Streamlit)

Two tools in one app, as separate tabs:

## 1. Email Validator
Upload an Excel file of email addresses and get back:
- A **Valid** list (.xlsx)
- An **Invalid / Inactive** list (.xlsx)
- A full report with an "Unknown/Risky" tab for anything inconclusive

## 2. Duplicate Remover
Upload an Excel file of email addresses and get back:
- A **de-duplicated** list (first occurrence of each address kept)
- A report of just the **removed duplicate rows**
- A combined report with both as separate tabs

Duplicate matching is case-insensitive and whitespace-trimmed by default, with an
optional toggle to also treat Gmail dot-variants (`john.doe@gmail.com` vs.
`johndoe@gmail.com`) as duplicates, since Gmail ignores dots and both deliver to
the same inbox.

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

This opens the app in your browser (usually `http://localhost:8501`).

## Usage

1. Upload an `.xlsx`/`.xls` file — any column with "email" in its name is
   auto-detected, or pick the column manually.
2. Optionally turn on:
   - **Disposable domain flagging** (mailinator.com, etc.) — on by default.
   - **SMTP mailbox check** — off by default, see note below.
3. Click **Run validation**, then download the Valid / Invalid / Full report
   files.

## About the SMTP check

The deepest check — actually asking the destination mail server "does this
mailbox exist?" — needs an outbound connection on port 25. That port is
blocked on most cloud platforms and many home/office networks. When it's
blocked, the app marks those addresses **Unknown** rather than guessing
Valid or Invalid.

Without the SMTP check, the app still does two strong checks for every
address:
- **Syntax** — catches typos and malformed addresses.
- **MX records** — confirms the domain actually has a mail server set up
  to receive email at all (this alone catches most dead/fake domains).

If you need mailbox-level confirmation reliably, run this from a machine
with unrestricted outbound port 25 (many home ISPs block it too — a cheap
VPS is usually the most reliable option), or plug in a paid API such as
ZeroBounce, Hunter, or NeverBounce, which run their checks from
infrastructure built for this.

## Notes

- Large lists (thousands of rows) with SMTP checking on can take a while —
  each address needs a live network round-trip. MX-only mode is much faster
  since MX lookups per domain are cached.
- No emails are ever sent — verification stops right before the "DATA"
  step of the SMTP conversation.
