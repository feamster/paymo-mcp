---
name: paymo-monthly-billing
description: >-
  Run the end-of-month Paymo billing workflow: discover active projects with
  unbilled hours, create draft invoices, generate CSV timesheets to Dropbox,
  attach clickable share-link footers, reconcile invoice totals against CSV
  timesheet totals, audit recipient emails against prior sends per matter,
  and present a summary table for user approval before they hit Send in the
  Paymo web UI. Use when the user asks to "run monthly billing", "invoice
  last month", "do the Paymo billing", or similar end-of-month prompts.
metadata:
  version: 0.1.0
  requires:
    mcp_servers:
      - paymo
      - spark
    auth:
      - ~/.mcp-auth/paymo/auth.json
      - ~/.mcp-auth/dropbox/auth.json
---

# paymo-monthly-billing

Orchestrate the recurring monthly Paymo billing run end-to-end. Every step
is driven by MCP tools from the `paymo` server (plus `spark` for the
recipient audit) — this skill's job is to sequence them, apply defensive
checks, and stop at the natural human-in-the-loop gates.

Paymo's API cannot send invoices. The final Send is always a manual click
in the Paymo web UI. This skill takes the user right up to that gate with
a summary table they can eyeball.

## When to use

- "Run monthly billing" / "do the Paymo billing" / "invoice last month".
- User names a specific month: "invoice July", "bill 2026-07".
- User wants the recipient audit re-run against past sends (see step 6).

## Inputs

At most one positional argument: the target month, in any of the forms
`create_paymo_invoice` accepts (`last`, `current`, `YYYY-MM`, `June`,
`June 2026`). Default: `last`.

If the user names specific projects to include/exclude, honor that
override — otherwise use the full set from `get_projects_needing_invoicing`.

## Step-by-step

### 1. Discover projects needing invoicing

```
paymo.get_projects_needing_invoicing(month="<YYYY-MM of target>")
```

Returns `{projects_needing_invoicing: [...], total_unbilled}`. For each
project you'll see `project_id`, `project_name`, `client_name`, `rate`,
`unbilled_hours`, `unbilled_amount`, `last_invoice_date`.

Show the user this list as a table (columns: Client, Project, Unbilled hrs,
Unbilled $, Last invoice) and ask them to confirm the set to invoice.
Small hours (e.g. < 1.0) are often intentional non-billed carryover —
flag them but don't drop them silently.

### 2. Dry-run each invoice

For every confirmed project, run a dry-run first:

```
paymo.create_paymo_invoice(
    project_id=<id>,
    month="<target>",
    dry_run=True,
)
```

This returns the planned payload (line items, hours, total, template
source) without hitting Paymo. Show the user a compact per-project
summary. Stop if anything is obviously wrong (missing rate, zero hours,
weird template source).

### 3. Create real invoices

Once confirmed, re-run without `dry_run`:

```
paymo.create_paymo_invoice(
    project_id=<id>,
    month="<target>",
    dry_run=False,      # or omit
)
```

Capture the returned `invoice.number` (e.g. `INV-20260802-269`) and
`invoice.id` per project. Both are needed downstream.

### 4. Ensure `linked_projects` is set (web UI quirk)

Paymo has *two* project associations on an invoice: top-level `project_id`
and `options.linked_projects`. The web UI Project column renders from
`linked_projects`. **Verified 2026-09-01: `create_paymo_invoice` does NOT
actually set `linked_projects`** (despite earlier claims) — every new
invoice needs a post-create patch or the Project column shows empty.

If the MCP `update_paymo_invoice` tool in use has no `project_id` param,
patch via raw PUT — and **`linked_projects` items MUST be objects**:

```python
# ~/src/paymo-mcp: PaymoClient(api_key from ~/.mcp-auth/paymo/auth.json)
cur = c._request('GET', f'invoices/{inv_id}')['invoices'][0]
opts = cur.get('options') or {}
opts['linked_projects'] = [{'amount': invoice_total, 'project_id': proj_id}]
c._request('PUT', f'invoices/{inv_id}', json={'project_id': proj_id, 'options': opts})
```

**HARD-WON (2026-09-01):** writing bare int IDs (`linked_projects: [123]`)
is silently accepted by the API but **crashes the web UI invoices summary
page** — it will not render at all until the shape is fixed. Always copy
the object schema from a known-good invoice before writing. Related: Paymo
auto-escapes `=`/`&` inside footer hrefs to `&#61;`/`&amp;` on write, so
sending raw URLs in footers is safe.

### 5. Export timesheets to Dropbox and reconcile totals

For each newly-created invoice:

**5a. Export the CSV.** Use the strict validator — it already checks that
CSV `hours × rate` matches the invoice total within 5%:

```
paymo.export_invoice_timesheet(invoice_number="<INV-...>", strict=True)
```

Save the returned CSV under
`~/Dropbox/Invoices/<YYYY-MM>/<client-slug>_<invoice-number>_timesheet.csv`
(or whatever convention the user's past invoices follow — look at prior
files in `~/Dropbox/Invoices/` before picking a name).

**5b. Triple-check totals — DO NOT SKIP.** The single most costly
billing bug is a mismatch between what the invoice charges and what the
attached CSV documents. Reconcile from three sides:

1. **CSV header total.** The first non-empty lines of the CSV include a
   `Total Due` cell. Parse it.
2. **Sum of CSV rows.** Sum every row's `Duration` (decimal hours or
   `HH:MM`), multiply by the project rate, add any Expenses line from the
   footer.
3. **Paymo authoritative total.** Call
   `paymo.get_paymo_invoice_financials(invoice_number="<INV-...>")` and
   read `total`, `fees`, `expenses`.

All three must agree to the cent (allow ≤ $0.01 rounding). Any deviation
is a hard stop — surface it to the user with the three numbers side by
side and pause. Common causes: an entry with a non-standard rate, an
unbilled expense that slipped into the invoice, a `mark_billed` race that
double-counted an entry. Do not silently proceed with `strict=False`.

If totals check clean, print a one-line confirmation per invoice:
`✓ <INV-...>  $X,XXX.XX  (CSV hdr $X,XXX.XX / CSV rows $X,XXX.XX)`.

### 6. Audit recipients — but know the Paymo Send dialog only auto-populates ONE

**Critical Paymo constraint (verified 2026-08-02):** The web UI Send
Invoice dialog's "Email addresses" field is populated from a **single**
field on the client record: `clients/{client_id}.email`. It does NOT
read from:

- `invoice.bill_to` (the address block)
- `invoice.options.notification.to`
- `clientcontacts` records

`client.email` validates as a single address only — comma-separated,
semicolon, and JSON arrays all fail with `400 "'…' is not a valid email
address"`. There is no API path to have multiple recipients auto-appear
in the Send dialog. Extras must be typed in at Send time (autocomplete
may surface stored `clientcontacts` after the first letter).

**What this means for the workflow:**

1. **Get `client.email` right per active matter.** For clients with
   multiple concurrent matters (e.g. Daignault Iyer has Avatier and
   Mesh Dynamics with different lead billing contacts), swap
   `client.email` to the correct primary before the user opens the
   Send dialog. Use:
   ```python
   PaymoClient(...).\_request(
       'PUT', f'clients/{client_id}',
       json={'email': 'primary@example.com'},
   )
   ```
2. **Store extras as `clientcontacts`** so they appear in autocomplete
   when the user types in the Send dialog:
   ```python
   PaymoClient(...).\_request(
       'POST', 'clientcontacts',
       json={'client_id': cid, 'name': 'Full Name', 'email': 'x@y.com'},
   )
   ```
   Check existing first: `GET clientcontacts?where=client_id=<cid>`.
3. **Include a "Recipients to add manually" column** in the final
   summary table (see step 8) listing the extras the user needs to type
   into the Send dialog for each invoice.

**Audit source of truth for who to send to:**

- **Prior sends via email** (Spark) — authoritative for "who has
  actually received invoices for this matter":
  ```
  spark.search_emails(sender_email="feamster", query="<matter>", limit=10)
  ```
- **Prior `preview_paymo_invoice_send`** on recent same-client invoices
  shows what `notification_to` and `bill_to_emails` were set to — but
  those did NOT drive who actually got the email, so treat them as a
  weak signal only.

Known primary/extras per matter (verify each time — these drift):

| Client / matter | Primary (`client.email`) | Extras (manual add / autocomplete) |
|-----------------|--------------------------|-------------------------------------|
| Daignault Iyer / Mesh Dynamics | `dporter@daignaultiyer.com` | `cpampinella@`, `jcharkow@daignaultiyer.com` |
| Daignault Iyer / Avatier | `avatierlit@daignaultiyer.com` | (matter-specific — check) |
| Expert Connect / US v. Huawei | `scresswell@expertconnectlegal.com` | `schung@expertconnectlegal.com` |
| Covington / X Corp v. Apple | `hliu@cov.com` | `lzehmer@`, `bsaunders@cov.com` |
| Keystone Strategy (any matter) | `keystone.ap@keystone.com` | *(none — user's confirmed policy is AP-only)* |
| Aguilar Bentley / Avaya v. Edify | `lbentley@aguilarbentley.com` | *(none)* |

**Do NOT** waste effort updating `invoice.bill_to` or
`options.notification.to` to "fix" recipient lists — neither field
drives the Send dialog. This mistake has been made and reproduced with a
screenshot; don't repeat it.

### 7. Attach clickable Dropbox share-link footer

For each invoice, splice the timesheet share link(s) into the footer:

```
paymo.generate_invoice_footer_with_share_links(
    invoice_number="<INV-...>",
    timesheet_paths=[
        "/Users/feamster/Dropbox/Invoices/<YYYY-MM>/<...>_timesheet.csv",
    ],
    # keystone_csv_paths=[...]  # if this is a Keystone invoice with extra CSVs
    apply=True,
)
```

Idempotent — safe to re-run. The tool mints (or reuses) a Dropbox share
URL and inserts it between marker comments, preserving any bank-routing
block already in the footer.

Verify the returned `links` dict maps every path to a `https://…dropbox…`
URL (not a `file://` or local path — the memory `feedback_invoice_footer_format`
exists specifically because local paths went out on invoices once).

### 8. Present the summary table

Before handing back to the user, print one table covering every invoice
in the batch. **Split recipients into "Primary (auto)" and "Add
manually"** — the Send dialog only auto-populates the primary:

| Invoice # | Client / Matter | Hours | Total | Primary (auto) | Add manually | Timesheet link | Status |
|-----------|-----------------|-------|-------|---------------|--------------|----------------|--------|
| INV-…-269 | Aguilar Bentley / Avaya v. Edify | 4.5 | $2,362.50 | `lbentley@aguilarbentley.com` | — | `dropbox.com/…` | draft ✓ |
| INV-…-268 | Covington / X v. Apple | 59.5 | $49,104 | `hliu@cov.com` | `lzehmer@cov.com`, `bsaunders@cov.com` | `dropbox.com/…` | draft ✓ |

Include an explicit line: **"Totals reconciled: ✓ N/N invoices match CSV
timesheet totals."** If any didn't reconcile, list them separately with
the three totals shown.

### 9. Hand off to the user for send

The Paymo API cannot send. Tell the user:

> Draft invoices are ready in Paymo. Please open each in the Paymo web
> UI and click **Send**. The **Primary** column above is what
> auto-populates in the "Email addresses" field. Copy-paste anything
> from the **Add manually** column into that same field before hitting
> Send Invoice. Extras are saved as `clientcontacts` so autocomplete
> should surface them after the first letter.

Do **not** offer to click Send yourself; there is no API for it.

## Things to get right

- **Never claim invoices were "sent" — only "drafted".** Paymo has no
  send endpoint (verified 2026-07-22).
- **Reconcile totals from three sides in step 5b.** A mismatch is a hard
  stop, not a warning. The user has been burned by CSVs that don't match
  invoice charges.
- **Never put local file paths in an invoice footer.** Always Dropbox
  share URLs. `generate_invoice_footer_with_share_links` handles this
  correctly; only worry if you're constructing footers by hand.
- **Cross-check recipients per matter, not per client.** Template
  inheritance can leak the wrong DL when a client has multiple matters.
- **Send dialog is single-recipient.** `client.email` drives it; don't
  waste time editing `bill_to` or `notification_to` to "fix" recipients.
  Set `client.email` to the correct primary and surface extras in the
  summary for the user to paste in.
- **Preserve `options.linked_projects` on any raw PUT.** Blowing it away
  makes the web UI Project column look empty even though `project_id`
  is set.
- **Auth files are secrets.** `~/.mcp-auth/paymo/auth.json` and
  `~/.mcp-auth/dropbox/auth.json` — never paste their contents into
  chat, commits, or issue comments.

## Troubleshooting

- **`export_invoice_timesheet` raises "totals don't match within 5%"** —
  strict mode caught a mismatch. Do NOT retry with `strict=False` to
  paper over it. Instead diagnose: dump the invoice's line items with
  `get_paymo_invoice_financials`, and list entries linked to the invoice
  via `list_paymo_entries` to find the outlier.
- **Dropbox share-link tool raises `DropboxAuthError`** — refresh token
  in `~/.mcp-auth/dropbox/auth.json` is expired or missing. See the
  README's Dropbox OAuth setup section for the re-auth curl.
- **Web UI shows empty Project column even though `project_id` is set** —
  `options.linked_projects` was cleared. Re-run
  `update_paymo_invoice(invoice_number=..., project_id=...)` to restore.
- **`get_projects_needing_invoicing` misses a project** — the tool
  filters on `active=True` and `unbilled_hours >= min_unbilled_hours`.
  If the project was recently deactivated or all entries are already
  marked billed, it won't appear. Cross-check with `list_paymo_projects`
  and `list_paymo_entries` for that project.
