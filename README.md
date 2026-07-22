# Paymo MCP Server

A Model Context Protocol (MCP) server for [Paymo](https://www.paymoapp.com/) time tracking and invoicing. Enables Claude Code to manage time entries, projects, tasks, and generate invoice timesheets.

## Features

- ✅ **Time Entry Management**: Create and manage time entries via natural language
- ✅ **Project & Task Discovery**: List and search projects/tasks by name
- ✅ **Invoice Timesheet Export**: Generate CSV timesheets for specific invoices
- ✅ **Invoice Creation** (`create_paymo_invoice`): Roll a project's unbilled entries into a Paymo invoice, grouped by task, with a `dry_run` preview and automatic entry↔invoice-item linkage
- ✅ **Glimpse / Keystone CSV Export** (`export_glimpse_timesheet`): Emit the exact `Date, Duration Hours, Comment, Project Code, Billable` template that Keystone matters submit through the Glimpse portal
- ✅ **Unbilled Time Analysis**: Track unbilled hours and revenue
- ✅ **Batch Operations**: Submit multiple entries from YAML format
- ✅ **Smart Filtering**: Filter entries by project, date range, billing status
- ✅ **Chronological Sorting**: All exports automatically sorted by date

## Installation

```bash
git clone https://github.com/feamster/paymo-mcp.git
cd paymo-mcp
pip install -r requirements.txt
```

### Requirements

- Python 3.8+
- Paymo account with API access
- [fastmcp](https://github.com/jlowin/fastmcp) for MCP server functionality

## Configuration

Configuration is split between non-sensitive settings and auth:

**`~/.mcp-config/paymo/config.json`** (non-sensitive, can be in dotfiles):
```json
{
  "timezone": "America/Chicago",
  "projects": {
    "Client Matter Name": {
      "project_id": 12345,
      "task_id": 67890
    }
  }
}
```

**`~/.mcp-auth/paymo/auth.json`** (sensitive, sync separately):
```json
{
  "api_key": "your-paymo-api-key-here"
}
```

### Getting Your API Key

1. Log into Paymo
2. Go to Settings → API
3. Generate a new API key
4. Copy the key to your config file

## Usage

### As a CLI Tool

```bash
# List projects
python3 paymo_timesheet.py list-projects

# List tasks for a project
python3 paymo_timesheet.py list-tasks --project-id 12345

# Create a single entry
python3 paymo_timesheet.py create-entry \
  --task-id 67890 \
  --date 2025-12-10 \
  --hours 3.5 \
  --description "Document review and analysis"

# Export invoice timesheet by invoice number
python3 paymo_timesheet.py export-invoice-timesheets \
  --invoice-number INV-20260331-241 \
  --output-dir ./invoices

# List unbilled entries
python3 paymo_timesheet.py list-entries \
  --start 2025-11-01 \
  --end 2025-11-30 \
  --unbilled
```

### As an MCP Server

#### 1. Start the Server

```bash
python3 paymo_timesheet.py mcp
```

#### 2. Configure Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "paymo": {
      "command": "python3",
      "args": ["/path/to/paymo-mcp/paymo_timesheet.py", "mcp"]
    }
  }
}
```

#### 3. Restart Claude Desktop

The Paymo tools will now be available in Claude Desktop.

## MCP Tools Reference

### Project & Task Management

#### `list_paymo_projects()`
List all active Paymo projects.

**Returns:** List of projects with IDs, names, and client information.

#### `list_paymo_tasks(project_id: int)`
List all tasks for a specific project.

**Args:**
- `project_id`: The Paymo project ID

**Returns:** List of tasks with IDs, names, and billing information.

### Time Entry Management

#### `create_paymo_entry(task_id, date, duration_hours, description)`
Create a single time entry.

**Args:**
- `task_id` (int): Task ID to log time against
- `date` (str): Date in YYYY-MM-DD format
- `duration_hours` (float): Hours worked (e.g., 3.5)
- `description` (str): Description of work performed

**Returns:** Created entry details.

**Example:**
```python
create_paymo_entry(
    task_id=31450618,
    date="2025-12-10",
    duration_hours=6.0,
    description="Expert report drafting and analysis"
)
```

#### `submit_paymo_timesheet(yaml_content: str)`
Submit multiple entries from YAML format.

**Args:**
- `yaml_content`: YAML string with timesheet entries

**Returns:** Summary of created entries.

**Example YAML:**
```yaml
matter: "Patent Litigation Matter"
client: "Law Firm Client"
rate: 650

entries:
  - date: "2025-12-02"
    start_time: "09:00"
    end_time: "12:30"
    timezone: "America/Chicago"
    task_id: 31450618
    description: "Case strategy meeting"

  - date: "2025-12-03"
    duration_hours: 5.0
    task_id: 31450740
    description: "Expert witness report preparation"
```

#### `list_paymo_entries(start_date, end_date, project_id=None, billed=None)`
List time entries with optional filters.

**Args:**
- `start_date` (str): Start date (YYYY-MM-DD)
- `end_date` (str): End date (YYYY-MM-DD)
- `project_id` (int, optional): Filter by project
- `billed` (bool, optional): Filter by billing status (True=billed, False=unbilled, None=all)

**Returns:** List of entries with task names, durations, descriptions, and billing status.

### Invoice Management

#### `list_paymo_invoices(client_id=None, status=None)`
List Paymo invoices with optional filters.

**Args:**
- `client_id` (int, optional): Filter by client
- `status` (str, optional): Filter by status ("draft", "sent", "viewed", "paid")

**Returns:** List of invoices with numbers, amounts, dates, and statuses.

#### `get_outstanding_invoices_last_week()`
Get outstanding invoices from the last 7 days.

**Returns:** List of recent invoices with status "sent" or "viewed".

#### `export_invoice_timesheet(invoice_number: str, strict: bool = True)`
Export a formatted, billing-ready timesheet CSV for a specific invoice. **This is the primary tool for generating invoice timesheets.**

**Args:**
- `invoice_number` (str): The invoice number as shown on the invoice (e.g., "INV-20260331-241")
- `strict` (bool): If True (default), validate that calculated totals match invoice. If False, skip validation.

**Returns:** Formatted CSV content with:
- **Header section**: Matter name, Invoice number, Period, Total Hours, Fees, Expenses, Total Due
- **Data section**: Date, Start Time (HH:MM), End Time (HH:MM), Duration, Task, Description
- **Footer**: Expenses summary

**Features:**
- **Strict matching (default)**: Only includes entries explicitly linked to that invoice via `invoice_item_id`, and validates that calculated fees match invoice totals within 5%
- Chronologically sorted by date (earliest first)
- Clean HH:MM time format (not raw ISO timestamps)
- Billing-ready format with summary header
- 90-day lookback to capture all entries

**Example:**
```python
export_invoice_timesheet("INV-20260331-241")           # strict validation (default)
export_invoice_timesheet("INV-20260331-241", False)    # skip validation
```

**When to use:**
- "Export timesheet for invoice INV-20260331-241"
- "Generate the timesheet for my latest invoice"
- "Get a billing-ready timesheet for invoice X"

**If validation fails:** Use `export_paymo_timesheet(start_date, end_date, project_id)` to export by date range instead.

#### `export_invoice_paymo_format(invoice_number: str, strict: bool = True)`
Export timesheet in **exact Paymo native format** with all standard columns. Use this when you need the export to match Paymo's own export format exactly.

**Args:**
- `invoice_number` (str): The invoice number (e.g., "INV-20260331-241")
- `strict` (bool): If True (default), validate totals match invoice

**Returns:** CSV with exact Paymo columns:
```
User, Internal User Id, Project, Internal Project Id, Project Description,
Tasklist, Internal Tasklist Id, Task, Internal Task Id, Start Time, End Time,
Worked Time, Decimal Hours, Time In Seconds
```

**When to use:**
- Need exact Paymo format for import into another system
- Need all internal IDs (user, project, task, tasklist)
- User explicitly asks for "Paymo format"

**Example:**
```python
export_invoice_paymo_format("INV-20260331-241")
```

#### `export_paymo_timesheet(start_date, end_date, project_id=None, format="csv")`
Export timesheet for a date range.

**Args:**
- `start_date` (str): Start date (YYYY-MM-DD)
- `end_date` (str): End date (YYYY-MM-DD)
- `project_id` (int, optional): Filter by project
- `format` (str): Export format ("csv" or "xls")

**Returns:** Path to exported file.

### Expense Management

#### `list_paymo_expenses(start_date=None, end_date=None, project_id=None, billable_only=False)`
List Paymo expenses, optionally filtered by date range and/or project.

**Args:**
- `start_date` (str, optional): YYYY-MM-DD (inclusive)
- `end_date` (str, optional): YYYY-MM-DD (inclusive)
- `project_id` (int, optional): Restrict to one project/matter
- `billable_only` (bool): If True, return only billable expenses (filtered in Python)

**Returns:** List of trimmed expense dicts (`id`, `project_id`, `client_id`, `name`, `notes`, `date`, `amount`, `currency`, `billable`, `invoiced`, `invoice_item_id`).

**Note on date filtering:** Paymo's server-side `where=date in (...)` clause is *set membership*, not a range, and silently returns wrong results. This tool filters dates in Python; the `project_id` server filter is used when supplied.

**When to use:**
- *"What expenses have I filed on matter X?"*
- *"What was the last expense I created?"* (sort results by `date`/`id` descending)

#### `create_paymo_expense(project_id, name, date, amount, description=None, quantity=1, billable=True, currency="USD")`
Create a single expense on a project/matter.

**Args:**
- `project_id` (int): Paymo project (matter) ID
- `name` (str): Short label, e.g. `"United - DFW deposition travel"`
- `date` (str): YYYY-MM-DD (charge date)
- `amount` (float): Total expense amount (price × quantity)
- `description` (str, optional): Detail (merchant, purpose)
- `quantity` (float): Default 1; `price` is derived as `amount / quantity`
- `billable` (bool): Default True
- `currency` (str): Default `"USD"`

**Returns:** Trimmed created expense dict (`id`, `project_id`, `name`, `date`, `amount`, `billable`).

#### `delete_paymo_expense(expense_id: int)`
Delete an expense by ID. Corrections = delete then recreate.

**Returns:** Status string (`"Successfully deleted expense N"` or a failure message).

#### `audit_paymo_expenses(start_date, end_date, project_id=None)`
Read-only audit over a date range - never writes. Checks:

- **Duplicates** — exact (project+date+amount+name → *error*), near-dupe (amount+name within ±2 days → *warn*)
- **Math integrity** — `abs(price - amount) > 0.01` → *error*; amount ≤ 0 → *warn*; non-USD → *warn*. (Paymo does not persist `quantity`, so unit expenses have `price == amount`.)
- **Matter-mapping sanity** — expense's project has zero hours in date ±1 window, or a different project dominated → *warn*
- **Billing hygiene** — billable + uninvoiced but matter has a later invoice → *info*; `invoiced=true` with no `invoice_item_id` link → *warn*

**Returns:** `{range, counts, totals_by_matter, findings}`. Fixes are executed by the caller via `delete_paymo_expense` + `create_paymo_expense`.

## Example Queries (via Claude Desktop)

### Time Entry Creation

- *"Create a 3.5 hour entry for the Patent Litigation project on Dec 10 for prior art research"*
- *"Log 6 hours today on expert report drafting for the IP case"*
- *"Add a 2 hour call entry for yesterday on the litigation support task"*

### Project & Invoice Discovery

- *"List all my active projects"*
- *"Show me tasks for the Corporate Advisory project"*
- *"What invoices do I have outstanding from last week?"*
- *"List all unpaid invoices for Client XYZ"*

### Analytical Queries

- *"How much unbilled time do I have in the last 30 days?"*
- *"Which projects haven't had an invoice in the last month?"*
- *"Show me unbilled hours for the Patent Litigation project"*
- *"What's my total billed revenue for November 2025?"*
- *"Calculate my unbilled revenue by project for Q4"*

### Timesheet Export

- *"Export the timesheet for invoice INV-20260331-241"*
- *"Generate timesheet for my latest DivX invoice"*
- *"Export timesheets for all outstanding invoices from last week"*
- *"Generate a CSV of my December time entries"*

## Example Output

### Invoice Timesheet Export

When you run `export_invoice_timesheet("INV-20260331-241")`, you get a billing-ready CSV:

```csv
Matter,DivX vs. Netflix
Invoice,INV-20260331-241
Period,2026-03-04 to 2026-03-25
Total Hours,239.01
Fees,$143406.00
Expenses,$1016.95
Total Due,$144422.95

Date,Start Time,End Time,Duration (hours),Task,Description
2026-03-04,09:00,10:30,1.50,Trial Prep,Trial prep
2026-03-04,11:00,13:00,2.00,Trial Prep,"Post outline review session: incorporated feedback on outline flow"
2026-03-06,01:00,04:00,3.00,Trial Prep,"Solo trial prep: reviewing patent materials and invalidity case outline"
2026-03-06,16:00,17:30,1.50,Trial Prep,"Trial prep run-through with counsel re: patent technical benefits section"

Expenses,$1016.95
```

**Key features:**
- Header with Matter, Invoice, Period, Total Hours, Fees, Expenses, Total Due
- Times in clean HH:MM format (not raw ISO timestamps)
- Entries sorted chronologically by date
- Footer with expenses

### Unbilled Time Analysis

When you ask *"How much unbilled time do I have?"*, Claude might respond:

```
You have 47.5 unbilled hours across 3 projects:

Patent Litigation Matter: 30.75 hours ($19,987.50)
Corporate Advisory: 12.00 hours ($7,800.00)
Expert Witness Case: 4.75 hours ($3,087.50)

Total unbilled: $30,875.00
```

## How It Works

### Natural Language to API Calls

The MCP server enables Claude to automatically translate natural language to Paymo API calls:

**You say:** *"Create a 6 hour entry for the litigation project on Dec 10"*

**Claude automatically:**
1. Calls `list_paymo_projects()` to find projects
2. Searches for "litigation" in project names
3. Calls `list_paymo_tasks(project_id)` to get tasks
4. Creates the entry with `create_paymo_entry()`

**You say:** *"Which projects have unbilled time?"*

**Claude automatically:**
1. Calls `list_paymo_projects()` to get all projects
2. For each project, calls `list_paymo_entries()` with `billed=False`
3. Aggregates and reports unbilled hours by project

### Invoice-Specific Exports

The `export_invoice_timesheet()` function provides billing-ready timesheets with **strict validation**:

1. Finds invoice by number (e.g., "INV-20260331-241")
2. Retrieves invoice and its line items
3. Finds all time entries linked to those invoice items (via `invoice_item_id`)
4. **Validates totals**: Checks that (hours × rate) matches invoice total within 5%
5. Looks back 90 days to catch all entries (handles monthly billing cycles)
6. Fetches task names and project/matter name
7. Sorts entries chronologically by date (earliest first)
8. Generates formatted CSV with:
   - Header: Matter, Invoice, Period, Total Hours, Fees, Expenses, Total Due
   - Data: Date, Start Time (HH:MM), End Time (HH:MM), Duration, Task, Description
   - Footer: Expenses summary

This ensures you get **only** the entries actually billed on that specific invoice, with validation that the totals match.

**Alternative - Date Range Export:**
If you need entries by date range regardless of invoice linkage (or if strict validation fails), use `export_paymo_timesheet(start_date, end_date, project_id)` instead.

## Rate Limiting

The script automatically handles Paymo's API rate limits:

- Monitors `X-Ratelimit-Remaining` headers
- Adds 2-second delays between task lookups
- Retries on 429 errors with exponential backoff
- Displays warnings when approaching limits

## Troubleshooting

### "API key not configured"

Create `~/.mcp-auth/paymo/auth.json` with your API key (see Configuration section).

### "fastmcp not installed"

Install the MCP server dependency:
```bash
pip install fastmcp
```

### "Rate limit exceeded"

The script will automatically wait and retry. If you see this frequently, reduce batch operation sizes.

### Empty invoice exports

Some invoices may not have time entries (flat fee or expense-only invoices). Verify the invoice includes time entries in Paymo.

## Development

### Project Structure

```
paymo-mcp/
├── paymo_timesheet.py  # Main script (CLI + MCP server)
├── requirements.txt    # Python dependencies
└── README.md          # This file
```

### Key Classes

- **`PaymoClient`**: API wrapper with rate limiting and retry logic
- **`TimesheetProcessor`**: YAML parsing and batch entry creation

### Adding New MCP Tools

1. Add the `@mcp.tool()` decorator
2. Define clear docstrings with arg descriptions
3. Load config and create PaymoClient
4. Return structured data (dicts/lists, not strings)

Example:
```python
@mcp.tool()
def my_new_tool(arg1: str, arg2: int) -> Dict[str, Any]:
    """
    Brief description of what this tool does

    Args:
        arg1: Description of first argument
        arg2: Description of second argument

    Returns:
        Description of return value
    """
    config = load_config()
    api_key = config.get('api_key')
    client = PaymoClient(api_key)

    # Implementation here
    return {"result": "data"}
```

## License

MIT

## Contributing

Issues and pull requests welcome! Please ensure:

- Code follows existing style
- New features include documentation
- MCP tools have clear docstrings
- Rate limiting is respected

## Acknowledgments

Built with [FastMCP](https://github.com/jlowin/fastmcp) for Model Context Protocol support.
