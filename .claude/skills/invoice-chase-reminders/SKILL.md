# invoice-chase-reminders

Draft brief, cordial payment-reminder emails for outstanding invoices and
hand them back via `pbcopy`. Critically, reconcile Paymo's "sent/viewed"
status against *actual* payment evidence (Melio notifications, mailed-check
mentions, prior reply threads) BEFORE chasing — Paymo status is lossy and
chasing already-paid invoices burns credibility.

The user emails chases, not the API; this skill never sends. Output is
always `pbcopy` + a "where to send" line (reply-to-thread vs. new email,
with the recipient list already resolved).

## When to use

- "Let's do reminders for <client>" / "chase <client>" / "any update email for X?"
- User asks about outstanding invoices for a specific matter.
- The user names a known-paid signal they want cross-checked (e.g. "a check
  came in by mail for the recent one, but not the old one — chase that one").
- A monthly billing run just completed and the user wants to pivot into
  collecting on older open invoices.

Do NOT use this to send; Paymo's API cannot send and neither should this
skill reach into Gmail/Spark to send. Always stop at `pbcopy` + instructions.

## Step-by-step

### 1. Pull Paymo's outstanding list for the client

```
paymo.list_paymo_invoices(client_id=<id>)
```

Filter to `status in ("sent", "viewed")`. (`paid` and `void` are done.)
Note each invoice's number, date, due_date, total. If multiple matters
share one Paymo client (e.g. Daignault Iyer = Mesh + Avatier), you'll
need to attribute each invoice to a matter — see step 1b.

**1b. Attribute invoice → matter when the client has multiple matters.**
Fastest routes, in order:

1. Check the Dropbox timesheet folder — invoices Nick exports land under
   `~/Dropbox/Consulting/<Matter>/Timesheets/` with the INV number in the
   filename. If `INV-20260731-267_*.csv` is in `Mesh Dynamics/Timesheets/`,
   that invoice is Mesh.
2. If the folder route is empty, read the CSV header — the first row is
   `Matter,<matter name>`.
3. Last resort: `paymo.list_paymo_entries` for a date range just before
   the invoice date, grouped by project — the matching project is the
   invoice's matter.

Avoid guessing from amount or rate alone. Different matters can carry the
same rate.

### 2. Reconcile against actual payment evidence (DO NOT SKIP)

Paymo's "sent/viewed" is lossy — invoices paid by check or Melio ACH do
NOT auto-flip to `paid` in Paymo unless Nick manually marks them. Chasing
an already-paid invoice looks sloppy; always reconcile first.

Evidence sources, by payer:

| Payer / client | Payment signal | How to find it |
|-----------------|-----------------|-----------------|
| Daignault Iyer (Mesh, Avatier) | **Melio ACH** | `spark.search_emails(sender_email="service@meliopayments.com", query="<client name>")` — look for `Get paid instantly by X` (initiated) and `Your payment from X` (delivered). Reference the `invoice number` field in the email body. |
| Keystone Strategy | **Check by mail** | `spark.search_emails(query="payment received <matter>")` or search for prior "Payment received" threads Nick has written confirming receipt. |
| Covington / Cornerstone | **ACH** | `spark.search_emails(query="<INV-number>")` — Chanel/Rui typically confirm when payment went out. Monthly chase thread tracks what's cleared. |
| Aguilar Bentley | **Check by mail** (Jennifer Patete processes) | Nick usually tells you directly which invoice was paid; cross-check with recent "paid" markings in Paymo for the client. |
| Kellogg Hansen (Evan Leo) | **Wire** | Prior chase thread replies from Evan name which specific invoices cleared. |

**Melio-specific pattern:** Melio sends TWO emails per payment:
- `Get paid instantly by X` → sent when payer schedules the transfer (ETA 3–6 days)
- `Your payment from X` → sent when funds actually arrive

A `Get paid instantly` email means **in flight, not paid yet** — exclude
it from the chase but note the ETA in the reconciliation report. A `Your
payment from X` means **delivered** — exclude from the chase (and the
Paymo entry should ideally be marked paid, though that's not this skill's
job).

**Rule:** every Paymo-outstanding invoice gets explicitly classified as
one of {genuinely unpaid, in flight, delivered-but-not-marked, just-sent
too-new-to-chase}. Only the first group becomes chase targets.

### 3. Find the prior chase thread (preferred send route)

Nearly every matter has a recurring chase thread Nick replies to each
month. **Reply to that thread** — the recipient list auto-populates and
the thread's history gives the AP contact more context than a fresh
cold ask.

```
spark.search_emails(query="<INV-number from a past chase>", limit=5)
# or
spark.search_emails(query="outstanding invoice <matter>", sort_by="date", limit=5)
# or search for Nick's prior reminder thread subject
spark.search_emails(query="Reminder — Outstanding Invoices, <matter>")
```

Pull the most recent thread, open with `spark.get_email`, note the exact
`recipients` and `cc` fields. Reuse them verbatim. The thread subject
line is the one to reply to — don't invent a new one if a working thread
exists.

If no prior chase thread exists for the matter, fall back to the known
channel map below.

### 4. Known chase channels (verify each run — these drift)

**This is the channel map for CHASING, which is different from the Paymo
Send-dialog destination for NEW invoices.** The `client.email` field in
Paymo drives who gets the invoice when Nick clicks Send; chase mail
usually goes to an escalation contact or AP coordinator at the same firm,
NOT the Send-dialog address.

| Matter | Chase: To | Chase: Cc | Notes |
|--------|-----------|-----------|-------|
| NYT v. OpenAI (Keystone) | `mwhitmore@keystone.com`, `amammadova@keystone.com`, `jchoi@keystone.com` | — | NOT `keystone.ap@keystone.com` (that's the Send-dialog AP box, not the chase channel) |
| X Corp. v. Apple (Covington) | `CONeill@cornerstone.com` | `rchen@cornerstone.com` | Nick coordinates through Cornerstone (consulting firm), who talks to Covington counsel. NOT `hliu@cov.com`. |
| MDL v. Meta (Kellogg Hansen) | `eleo@kellogghansen.com` (Evan Leo) | — | Evan is prompt; keep tone cordial. |
| Avaya v. Edify (Aguilar Bentley) | `accounting@aguilarbentley.com` (Jennifer Patete) | `lbentley@aguilarbentley.com` | Jennifer handles AP; Lisa is the attorney. |
| Mesh v. Cisco (Daignault Iyer) | `dporter@daignaultiyer.com` (Devon Porter) | `cpampinella@daignaultiyer.com`, `jcharkow@daignaultiyer.com` | Same client as Avatier — different lead. |
| Avatier v. Microsoft (Daignault Iyer) | `avatierlit@daignaultiyer.com` | matter-specific — check prior thread |  |

If a matter isn't in this table, derive the chase channel from the most
recent prior reminder thread; failing that, ask Nick.

### 5. Match prior tone

Pull the user's last chase email for this matter and mirror its tone,
length, and format conventions. Common patterns worth preserving:

- **Fixed-width invoice table.** Nick's chases use aligned columns:
  ```
    INV-20260602-256    06/02/2026    $62,356.50
    INV-20260901-279    09/01/2026     $6,972.75

    Total:                            $69,329.25
  ```
  Two spaces of leading indent; dates in `MM/DD/YYYY`; totals right-aligned
  under the amount column.
- **Acknowledge payments received since the last chase.** "Thanks for
  getting X settled since my note last month" — this is the single
  highest-signal element because it tells the AP contact Nick is paying
  attention and the reconciliation is accurate.
- **Offer to resend.** "Happy to resend the invoice or timesheet if
  useful." Low cost, high goodwill.
- **No boilerplate dunning language.** Nick doesn't use phrases like
  "please remit payment promptly" or "this is a reminder that…". Keep it
  conversational.
- **Length target: 5–8 lines of body.** More than that reads impatient.
- **Specific to the contact's recent behavior:**
  - If they've been prompt (Evan Leo, Devon Porter): warm and brief.
  - If a specific invoice was skipped in a prior payment run (OpenAI 251):
    call that out explicitly so AP doesn't miss it again.
  - If multiple invoices are outstanding and the matter has settled, frame
    the chase as "closing out the account" rather than dunning.

### 6. Draft + `pbcopy`

Draft the full email including Subject line. Use HEREDOC piped to `pbcopy`:

```
cat <<'EOF' | pbcopy
Subject: <subject — reply-to-thread uses "Re: <existing subject>">

Hi <name>,

<one-line opener acknowledging any payments received>

<fixed-width invoice table>

<one-line soft ask + resend offer>

Thanks,
Nick
EOF
echo "Copied to clipboard."
```

The `echo` confirms to Nick the clipboard is populated.

### 7. Hand-off report

Immediately after `pbcopy`, print:

- **Send mode:** "Reply to the existing thread '<subject>'" OR "New email"
- **To:** <addresses>
- **Cc:** <addresses or "—">
- **What's being chased:** the N invoices, with totals
- **What was excluded and why:** any in-flight (Melio ETA), already-paid,
  or too-new-to-chase invoices, each with its reason

The exclusion list is not optional — it's the proof that the
reconciliation step actually happened and gives Nick a chance to catch
anything miscategorized before he sends.

## Things to get right

- **Always reconcile before chasing.** Paymo status alone is not evidence
  of what's actually unpaid. The two most common mistakes are (a) chasing
  an invoice that paid by check weeks ago, and (b) chasing an invoice
  that's currently in flight via Melio. Both make Nick look like he's not
  tracking his own books.
- **Reply to the existing chase thread, not a new email.** The thread
  carries the full history of what AP has already told Nick ("payment
  going out this week", "we're coordinating with the client", etc.).
  Starting fresh forces AP to re-explain.
- **Chase channel ≠ Send-dialog address.** Especially for Keystone
  (`keystone.ap` is the Send AP box; actual humans at the matter are
  Max/Arzu/Junsu) and X v. Apple (Send goes to `hliu@cov.com`; chase
  goes through Chanel at Cornerstone). Mixing these up delays payment
  because the Send-address AP boxes don't own escalations.
- **Attribute invoices to matters correctly** when a client has multiple
  matters. Daignault Iyer bills two concurrent matters with different
  leads; attributing a Mesh chase to Avatier's contacts (or vice versa)
  routes to the wrong AP queue.
- **Don't dun.** Nick's chase tone is "quick accounting housekeeping, no
  pressure" — not "this is overdue, please remit." Match it. Dunning
  language damages relationships with repeat clients who pay fine, just
  slowly.
- **Preserve dollar precision.** Use Paymo's authoritative total, not the
  CSV subtotal (they can differ by a few cents or dollars due to
  minute-level billing rounding — see paymo-time's "phantom Expenses"
  note).
- **Include the newly-just-sent invoice IFF the matter has settled or
  the user explicitly asks for a full accounting.** Otherwise, excluding
  not-yet-due invoices reads cleaner.
- **Never fabricate a payment acknowledgment.** If you say "thanks for
  getting X settled since my note last month" that payment MUST be in
  the Melio inbox or Paymo paid list. Fabricating it embarrasses Nick
  and tells AP he's not actually tracking.

## Troubleshooting

- **Paymo client has invoices across multiple matters and you can't tell
  which is which** — check Dropbox folder structure first
  (`~/Dropbox/Consulting/<matter>/Timesheets/`); the CSV filename includes
  the INV number. If still ambiguous, read the CSV header's `Matter` row.
- **No prior chase thread exists for the matter** — fall back to the
  channel map (step 4). If the matter isn't in the table and never had a
  chase, ask Nick who to send to; don't guess at AP boxes.
- **Melio "Get paid instantly" vs. "Your payment"** — the first means
  initiated (not delivered); the second means delivered. Only exclude an
  invoice from the chase for the first category if ETA is within a few
  days; if ETA is weeks out, the chase can still mention it as "noted,
  arriving <date>".
- **Spark search returns nothing for an invoice number** — the invoice
  number may contain a leading `#` in some sends; try both `"INV-..."`
  and `"#INV-..."`. Also: Spark FTS tokenizes digits oddly, so quote the
  full `"INV-YYYYMMDD-###"` string.
- **Multiple matters share the same client's Melio stream** — the Melio
  email body's `invoice number` field disambiguates. Don't rely on the
  dollar amount alone (two matters can hit the same number by coincidence).

## Tools

- **Paymo:** `paymo.list_paymo_invoices`, `paymo.list_paymo_entries`,
  `paymo.list_paymo_projects` (project→matter name mapping).
- **Spark (payment reconciliation + prior-thread discovery):**
  `spark.search_emails` (filter `sender_email` for Melio;
  `query` for INV number or matter name), `spark.get_email` (pull
  recipients/cc from the prior chase thread).
- **System:** `pbcopy` via Bash — HEREDOC piping.
- **Dropbox folder listing** via Bash (`ls ~/Dropbox/Consulting/*/Timesheets/`)
  for invoice→matter attribution.
