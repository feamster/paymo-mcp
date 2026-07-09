---
name: file-amex-expenses
description: >-
  Ingest a credit-card activity Excel export (Amex Business Platinum or
  similar), propose per-matter expense filings against Paymo projects using
  time-entry activity as the matter-attribution signal, iterate on user
  corrections, and after approval file one bundled Paymo expense per matter
  with a supporting xlsx attached via the Paymo API. Use when the user says
  "file expenses from my Amex statement," "process activity(N).xlsx and
  propose expenses," "ingest my credit card statement into Paymo," or names
  a credit-card statement file and asks to turn it into Paymo expenses.
  Skip: original itemized receipt collection (paid-out-of-band step handled
  separately by the user) and mid-flow correction of prior filings.
metadata:
  version: 0.1.0
  requires:
    bins:
      - python3
    python_modules:
      - openpyxl
      - requests
---

# file-amex-expenses

Turn a credit-card statement Excel export into Paymo expenses, one bundled
filing per matter, with a supporting xlsx attached to each. Mirrors the
workflow used successfully in July 2026 to file April–June 2026 Amex Business
Platinum expenses across Mesh Dynamics, OpenAI Copyright, and US v. Huawei.

## When to use

- User points at an Amex/credit-card statement export (typically
  `~/Downloads/activity*.xlsx`) and asks to turn it into Paymo expenses.
- User asks "what expenses do I need to file this month?" and there's a
  fresh statement in Downloads.
- User says "propose expenses from this statement — I want to review
  before you file."

## When NOT to use

- User wants to attach original itemized receipts (Southwest confirmations,
  Lyft receipts, etc.). That's a separate follow-up step and is not part
  of this skill — do it manually or via a receipt-fetching skill after
  filings land.
- User wants to correct an already-filed expense (wrong matter, wrong
  amount). That's a bespoke fix that needs `update_expense` on PaymoClient,
  not this batch flow.

## Inputs

- **Required:** path to the statement `.xlsx` (defaults to the most recent
  `~/Downloads/activity*.xlsx` if the user says "the latest statement").
- **Optional workspace root:** defaults to
  `~/Dropbox/Consulting/Expenses/<YYYY-MM-MM>-Amex-Statement/` where the
  date range is derived from the statement's actual charge dates. Ask if
  ambiguous.

## Prerequisites

- Paymo API key configured at `~/.mcp-auth/paymo/auth.json` (used by
  `paymo_timesheet.PaymoClient`).
- `paymo_timesheet.py` importable from the environment. Standard runs use
  `sys.path.insert(0, '/Users/feamster/src/paymo-mcp')`.

## Step-by-step

### 1. Load and clean the statement

Read the `.xlsx` with `openpyxl`. Amex Business Platinum places the header at
row 7, data starts at row 8. Columns of interest: Date (1), Description (3),
Amount (6), Extended Details (7), City/State (10), Reference (13),
Category (14).

- Drop rows with `amount <= 0` (payments, credits, refunds).
- Drop rows whose description contains `AUTOPAY` or `PAYMENT` (Amex own
  entries).
- Normalize dates to `date` objects; empty amounts get filtered.

Report the total: how many charges, sum, date range.

### 2. Pull Paymo context (cache!)

The audit tool + heavy Paymo queries will hit rate limits without pacing.
Fetch these once and cache to `/tmp/paymo_context.pkl`:

- `client.get_entries(start, end)` covering the statement window +/- 15 days
- `client.get_projects(active_only=False)`
- `client.get_tasks()` (to build `task_id -> project_id` map)
- `client.get_expenses(start_date, end_date)` for the statement window to
  see what's **already filed** (skip the duplicate-of-existing-filing
  charges — a very real hazard).

### 3. Cluster charges into trips

Group contiguous charges by destination city, treating home-airport
transfers (Chicago HS Car, ParkChicago, Midway parking) as "home" and
attaching them to the adjacent trip. Merge clusters that share the same
non-home destination and are within ~3 days.

**Watch for the airline-HQ-city trap:** Southwest merchant strings show
"DALLAS" as the city, United shows "HOUSTON", Delta shows "ATLANTA",
regardless of actual route. Lyft shows "SAN FRANCISCO" (HQ) even for rides
in other cities. Do not use those as destination signals — use the lodging
merchant's city.

### 4. Attribute each trip to a Paymo matter

For each trip window (start-1d to end+1d), tally hours by `project_id`
using the cached time entries. Report the top 3 matters and the top 5 task
names (task names are more diagnostic than raw hours — "Source Code Review"
vs "Rebuttal Drafting" tells you what the trip was for).

Bear in mind that **hours dominance alone is misleading** — a matter you
worked on remotely all week will dominate even if the trip was for a
different matter. Task-name pattern (e.g. "Deposition Prep") + destination
city ("Sidley Chicago" = Huawei's counsel) are stronger signals.

Write a `proposal.md` under the workspace root with:

- Summary counts, roll-up by matter, per-trip totals
- Proposed matter attribution for each trip (mark ambiguous ones ⚠️)
- Overlap warnings with already-filed expenses
- Personal / orphan / credit exclusions
- Numbered questions for the user

Open the file. **Do not create Paymo expenses yet.**

### 5. Iterate with the user

Expect corrections. Common ones:
- Trips get reassigned to different matters ("that SF trip was actually
  Mesh, not Meta")
- Trips get bundled or split
- Some charges get pulled out entirely as personal
- Existing filings get identified as covering some of the proposed charges

Update `proposal.md` in place with a version bump (v2, v3, ...) and a
"What changed in this revision" table at the top so the user can review
diffs quickly. **Do not renumber trips mid-flow unless the user asks** —
renumbering while they're referencing "trip #4" is chaos.

### 6. Bundle by matter and build supporting xlsx

Once the user approves, bundle multiple trips for the same matter into one
Paymo expense (matches how attorneys prefer to see expenses grouped by
matter). Use naming pattern matching prior filings, e.g. "May and June
Code Review" (2-month bucket) or "April - June NYC Trips" (per-matter
period). Pick the expense date as the last charge date in the bundle.

Under `<workspace>/<Matter>/`, generate `<Matter>-<period>-Expenses.xlsx`
with two sheets:

- **Summary**: Matter, project_id, period, total, per-trip breakdown, notes
- **Charges**: Trip | Date | Merchant | City | Category | Amount, with
  per-trip subtotals and a grand total row

Currency formatting: `'"$"#,##0.00'`. Header row with `PatternFill
"D9E1F2"`. Thin borders on data cells.

### 7. File each matter's expense in Paymo

For each matter, call `create_paymo_expense` (from the MCP) or the
underlying `PaymoClient.create_expense` with:

- `project_id`
- `client_id` — auto-lookup from the project if the tool supports it
  (`create_paymo_expense` does; direct `create_expense` does not — pass
  explicitly from `get_projects(...)[N]['client_id']`)
- `name` — user-facing label
- `date` — YYYY-MM-DD, last charge date of the bundle
- `amount` and `price` (Paymo does not persist `quantity`; on unit
  expenses `price == amount`)
- `billable=True`, `currency="USD"`
- `notes` — describe what's bundled, reference the attached xlsx

Then attach the xlsx via `client.upload_expense_file(expense_id, xlsx_path)`.
That helper handles the multipart Content-Type dance correctly (the session's
`Content-Type: application/json` is stripped so the file boundary sets
properly) and retries on 400/429 with exponential backoff.

**Sequence writes with at least ~5s spacing.** Paymo's write rate limits
are per-second-ish and back-to-back POST /expenses → POST /files can 400
with "Decoding failed: Syntax error" (misleading; it's rate-limit).
`_request` and `upload_expense_file` both call `_handle_rate_limit` which
sleeps when the reported budget hits zero, but a manual `time.sleep(3-5)`
between operations keeps things reliable.

### 8. Write per-matter summary and update the proposal

Under `<workspace>/<Matter>/summary.md`, capture:

- What was filed (Paymo expense_id, project_id, client_id, name, date,
  amount, billable, invoiced, attachment)
- Trips bundled (subtable with dates and per-trip totals)
- Attribution basis (dominant matter/task-names in window + user
  confirmation)
- Related prior filings on the same project (context table)
- Follow-up items (e.g., "attach original receipts", "add to invoice #N")

Update `<workspace>/proposal.md` marking each filing done with the Paymo
expense id.

## Pitfalls learned the hard way

- **Paymo requires `client_id` on POST /expenses**, not just `project_id`.
  Auto-lookup from the project record.
- **Multipart file uploads must not carry `Content-Type: application/json`
  from the session.** Use a fresh `requests.post()` (not
  `self.session.post()`) for uploads, or the boundary is clobbered and
  Paymo returns 400 "Decoding failed: Syntax error" — which looks like
  rate-limit but isn't.
- **`quantity` is not persisted** on Paymo expenses. Send `price == amount`
  on unit expenses. If the caller passes `quantity != 1`, derive
  `price = amount / quantity` but don't include `quantity` in the payload.
- **`invoiced` field is the truth, not `flag_billed`.** Any code checking
  billing state should use `invoiced` (bool) and `invoice_item_id`.
- **`?where=date in ("start","end")` is set membership, not a range.**
  Filter dates in Python. `project_id` server-side filter is fine.
- **Airline/Lyft merchant strings show HQ city, not destination.** Use
  lodging or actual local Lyft address details as the destination signal.

## Handy code paths

- Client: `paymo_timesheet.PaymoClient` (in `/Users/feamster/src/paymo-mcp/`)
- Helpers: `create_expense`, `update_expense`, `upload_expense_file`,
  `delete_file`, `get_expenses`, `get_projects`, `get_entries`, `get_tasks`
- MCP tools (via FastMCP): `create_paymo_expense`,
  `list_paymo_expenses`, `audit_paymo_expenses`, `delete_paymo_expense`

## Prior working example

`~/Dropbox/Consulting/Expenses/2026-04-Jun-Amex-Statement/` — the full
run this skill was extracted from, including source statement, proposal
iterations, and per-matter xlsx + summary. Read it first when in doubt
about output format.
