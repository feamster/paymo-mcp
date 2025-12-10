# Paymo MCP Server

A Model Context Protocol (MCP) server for [Paymo](https://www.paymoapp.com/) time tracking and invoicing. Enables Claude Desktop to manage time entries, projects, tasks, and generate invoice timesheets.

## Features

- ✅ **Time Entry Management**: Create and manage time entries via natural language
- ✅ **Project & Task Discovery**: List and search projects/tasks by name
- ✅ **Invoice Timesheet Export**: Generate CSV timesheets for specific invoices
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

Create `~/.paymo/config.yaml`:

```yaml
api_key: "your-paymo-api-key-here"
timezone: "America/Chicago"

projects:
  "Client Matter Name":
    project_id: 12345
    task_id: 67890  # Default task for quick entries
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

# Export invoice timesheet
python3 paymo_timesheet.py export-invoice-timesheets \
  --invoice-id 123456 \
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

#### `export_invoice_timesheet(invoice_id: int)`
Export detailed timesheet CSV for a specific invoice.

**Args:**
- `invoice_id` (int): The invoice ID to export

**Returns:** Path to generated CSV file.

**Features:**
- Only includes entries actually billed on that invoice
- Chronologically sorted (earliest first)
- Includes task names, descriptions, hours
- 90-day lookback to capture all entries

#### `export_paymo_timesheet(start_date, end_date, project_id=None, format="csv")`
Export timesheet for a date range.

**Args:**
- `start_date` (str): Start date (YYYY-MM-DD)
- `end_date` (str): End date (YYYY-MM-DD)
- `project_id` (int, optional): Filter by project
- `format` (str): Export format ("csv" or "xls")

**Returns:** Path to exported file.

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

- *"Export the timesheet for invoice #12345"*
- *"Export timesheets for all outstanding invoices from last week"*
- *"Generate a CSV of my December time entries"*
- *"Export the $19,500 invoice timesheet"* (matches by amount)

## Example Output

### Invoice Timesheet Export

```csv
Date,Start Time,End Time,Duration (hours),Task,Description,Billed,Entry ID
,2025-11-07T13:30:00Z,2025-11-07T19:30:00Z,6.00,Prior Art Research,Initial patent searches on all claims,,Yes,137317216
,2025-11-07T19:30:00Z,2025-11-07T20:00:00Z,0.50,Strategy Call,Case strategy discussion with counsel,,Yes,137317187
,2025-11-11T01:45:00Z,2025-11-11T05:59:00Z,4.23,Patent Analysis,Detailed analysis of Claims 1-5,,Yes,137364160
```

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

The `export_invoice_timesheet()` function uses smart filtering:

1. Retrieves invoice and its line items
2. Finds all time entries linked to those invoice items (via `invoice_item_id`)
3. Looks back 90 days to catch all entries (handles monthly billing cycles)
4. Fetches task names for each entry
5. Sorts chronologically (earliest first)
6. Generates clean CSV output

This ensures you get **only** the entries actually billed on that specific invoice, properly formatted and sorted.

## Rate Limiting

The script automatically handles Paymo's API rate limits:

- Monitors `X-Ratelimit-Remaining` headers
- Adds 2-second delays between task lookups
- Retries on 429 errors with exponential backoff
- Displays warnings when approaching limits

## Troubleshooting

### "API key not configured"

Create `~/.paymo/config.yaml` with your API key (see Configuration section).

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
