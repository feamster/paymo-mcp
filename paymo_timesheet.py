#!/usr/bin/env python3
"""
Paymo Timesheet Automation Script
Automate time entry creation in Paymo from structured meeting/work data.
Can run as CLI or MCP server.
"""

import requests
import yaml
import json
import sys
import time
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any
from pathlib import Path
import pytz
from dateutil import parser as dateparser
from rich.console import Console
from rich.table import Table
from rich import print as rprint
import click

console = Console()

# MCP Server support (optional)
MCP_AVAILABLE = False
try:
    from mcp.server.fastmcp import FastMCP
    MCP_AVAILABLE = True
except ImportError:
    pass


class PaymoClient:
    """Wrapper for Paymo API calls"""

    def __init__(self, api_key: str, base_url: str = "https://app.paymoapp.com/api/"):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/') + '/'
        self.session = requests.Session()
        self.session.auth = (api_key, 'X')
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        })

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict:
        """Make API request with error handling"""
        url = f"{self.base_url}{endpoint.lstrip('/')}"

        try:
            response = self.session.request(method, url, **kwargs)

            # Check rate limiting headers
            remaining = response.headers.get('X-Ratelimit-Remaining')
            limit = response.headers.get('X-Ratelimit-Limit')
            decay = response.headers.get('X-Ratelimit-Decay-Period')

            if remaining and int(remaining) < 5:
                console.print(f"[yellow]⚠ Rate limit: {remaining}/{limit} remaining (resets in {decay}s)[/yellow]")

            response.raise_for_status()
            return response.json()

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                retry_after = e.response.headers.get('Retry-After', '60')
                console.print(f"[red]Rate limit exceeded! Must wait {retry_after}s[/red]")
                # Re-raise with the retry_after info attached
                e.retry_after = int(retry_after)
            else:
                console.print(f"[red]API Error: {e}[/red]")
                if hasattr(e.response, 'text'):
                    console.print(f"[red]Response: {e.response.text}[/red]")
            raise
        except requests.exceptions.RequestException as e:
            console.print(f"[red]Request failed: {e}[/red]")
            raise

    def get_projects(self, active_only: bool = True) -> List[Dict]:
        """List all projects"""
        endpoint = "projects"
        if active_only:
            endpoint += "?where=active=true"

        response = self._request('GET', endpoint)
        return response.get('projects', [])

    def get_tasks(self, project_id: Optional[int] = None) -> List[Dict]:
        """List tasks, optionally filtered by project"""
        endpoint = "tasks"
        if project_id:
            endpoint += f"?where=project_id={project_id}"

        response = self._request('GET', endpoint)
        return response.get('tasks', [])

    def get_entries(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[Dict]:
        """List time entries within date range"""
        endpoint = "entries"

        if start_date and end_date:
            # Convert dates to ISO format
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.strptime(end_date, '%Y-%m-%d')

            start_iso = start_dt.strftime('%Y-%m-%dT00:00:00Z')
            end_iso = end_dt.strftime('%Y-%m-%dT23:59:59Z')

            endpoint += f'?where=time_interval in ("{start_iso}","{end_iso}")'

        response = self._request('GET', endpoint)
        return response.get('entries', [])

    def create_entry(self, task_id: int, **kwargs) -> Dict:
        """
        Create a new time entry

        Args:
            task_id: Required task ID
            **kwargs: Either (start_time, end_time) or (date, duration)
                     Plus optional description, billed, etc.
        """
        data = {'task_id': task_id, **kwargs}

        response = self._request('POST', 'entries', json=data)
        return response

    def create_entries_batch(self, entries: List[Dict]) -> Dict:
        """
        Create multiple time entries in one API call

        Args:
            entries: List of entry dicts, each with task_id and time data
        """
        response = self._request('POST', 'entries', json=entries)
        return response

    def delete_entry(self, entry_id: int) -> Dict:
        """Delete a time entry by ID"""
        response = self._request('DELETE', f'entries/{entry_id}')
        return response

    def create_task(self, project_id: int, name: str, billable: bool = True) -> Dict:
        """Create a new task in a project"""
        data = {
            'project_id': project_id,
            'name': name,
            'billable': billable
        }
        response = self._request('POST', 'tasks', json=data)
        return response

    def get_invoices(self, client_id: Optional[int] = None, status: Optional[str] = None) -> List[Dict]:
        """
        List invoices, optionally filtered by client and status

        Args:
            client_id: Filter by client ID
            status: Filter by status (sent, viewed, paid, etc.)
        """
        endpoint = "invoices"
        filters = []

        if client_id:
            filters.append(f"client_id={client_id}")
        if status:
            filters.append(f"status={status}")

        if filters:
            endpoint += "?where=" + " and ".join(filters)

        response = self._request('GET', endpoint)
        return response.get('invoices', [])

    def get_invoice(self, invoice_id: int) -> Dict:
        """Get detailed invoice information"""
        response = self._request('GET', f'invoices/{invoice_id}')
        return response.get('invoices', [{}])[0]

    def get_outstanding_invoices_last_week(self) -> List[Dict]:
        """Get outstanding invoices (sent or viewed) from the last 7 days"""
        from datetime import datetime, timedelta

        all_invoices = self.get_invoices()

        # Filter for outstanding (sent or viewed) from last 7 days
        week_ago = datetime.now() - timedelta(days=7)

        outstanding = []
        for inv in all_invoices:
            status = inv.get('status', '').lower()
            if status in ['sent', 'viewed']:
                inv_date_str = inv.get('date', '')
                if inv_date_str:
                    inv_date = datetime.strptime(inv_date_str, '%Y-%m-%d')
                    if inv_date >= week_ago:
                        outstanding.append(inv)

        return outstanding

    def export_invoice_entries_csv(self, invoice_id: int) -> str:
        """
        Export CSV of entries that are actually on a specific invoice

        Args:
            invoice_id: Invoice ID

        Returns:
            CSV content as string
        """
        import csv
        import io
        import html
        import time

        # Get invoice with items
        response = self._request('GET', f'invoices/{invoice_id}?include=invoiceitems')
        invoice = response.get('invoices', [{}])[0]
        invoice_items = invoice.get('invoiceitems', [])

        # Get invoice item IDs
        invoice_item_ids = set(item.get('id') for item in invoice_items if item.get('id'))

        if not invoice_item_ids:
            return "Date,Start Time,End Time,Duration (hours),Task,Description,Billed,Entry ID\n"

        # Get all entries and filter by invoice_item_id
        # We need to fetch entries to check their invoice_item_id
        # Use a broad date range - go back 3 months from invoice date to catch all entries
        inv_date = invoice.get('date', '')
        if inv_date:
            from datetime import datetime, timedelta
            inv_dt = datetime.strptime(inv_date, '%Y-%m-%d')
            # Get entries from 90 days before invoice to invoice date
            start_date = (inv_dt - timedelta(days=90)).strftime('%Y-%m-%d')
            end_date = inv_date
        else:
            # Fallback - get last 90 days
            from datetime import datetime, timedelta
            now = datetime.now()
            start_date = (now - timedelta(days=90)).strftime('%Y-%m-%d')
            end_date = now.strftime('%Y-%m-%d')

        all_entries = self.get_entries(start_date, end_date)

        # Filter to only entries on this invoice
        entries = [e for e in all_entries if e.get('invoice_item_id') in invoice_item_ids]

        # Sort entries by start date (earliest first)
        def get_entry_sort_key(entry):
            # Use start_time if available, otherwise use date
            if entry.get('start_time'):
                return entry.get('start_time')
            elif entry.get('date'):
                return entry.get('date')
            else:
                # Fallback to entry ID if no date info
                return str(entry.get('id', 0)).zfill(20)

        entries.sort(key=get_entry_sort_key)

        # Build task cache - fetch all unique tasks upfront
        task_cache = {}
        unique_task_ids = set(e.get('task_id') for e in entries if e.get('task_id'))

        for task_id in unique_task_ids:
            try:
                time.sleep(2)  # 2 second delay to avoid rate limits
                task_response = self._request('GET', f'tasks/{task_id}')
                task_data = task_response.get('tasks', [{}])[0] if 'tasks' in task_response else {}
                task_cache[task_id] = task_data.get('name', '')
            except Exception as e:
                # If we hit a rate limit, wait and retry once
                if '429' in str(e):
                    print(f"Rate limit hit, waiting 6 seconds...")
                    time.sleep(6)
                    try:
                        task_response = self._request('GET', f'tasks/{task_id}')
                        task_data = task_response.get('tasks', [{}])[0] if 'tasks' in task_response else {}
                        task_cache[task_id] = task_data.get('name', '')
                    except Exception as retry_err:
                        print(f"Warning: Failed to fetch task {task_id} after retry: {retry_err}")
                        task_cache[task_id] = ''
                else:
                    print(f"Warning: Failed to fetch task {task_id}: {e}")
                    task_cache[task_id] = ''

        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)

        # Header
        writer.writerow(['Date', 'Start Time', 'End Time', 'Duration (hours)',
                        'Task', 'Description', 'Billed', 'Entry ID'])

        # Rows
        for entry in entries:
            # Get task name from cache
            task_id = entry.get('task_id')
            task_name = task_cache.get(task_id, '') if task_id else ''

            # Clean description (strip HTML tags and decode entities)
            description = entry.get('description', '')
            if description:
                # Remove HTML tags
                import re
                description = re.sub(r'<[^>]+>', '', description)
                # Decode HTML entities
                description = html.unescape(description)
                description = description.strip()

            # Calculate duration
            if entry.get('duration'):
                duration_hours = entry['duration'] / 3600
            else:
                start = dateparser.parse(entry.get('start_time', ''))
                end = dateparser.parse(entry.get('end_time', ''))
                duration_hours = (end - start).total_seconds() / 3600 if start and end else 0

            writer.writerow([
                entry.get('date', ''),
                entry.get('start_time', ''),
                entry.get('end_time', ''),
                f"{duration_hours:.2f}",
                task_name,
                description,
                'Yes' if entry.get('billed') else 'No',
                entry.get('id', '')
            ])

        return output.getvalue()

    def export_timesheet_csv(self, start_date: str, end_date: str,
                            project_id: Optional[int] = None) -> str:
        """
        Export timesheet to CSV format by fetching entries and formatting

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            project_id: Optional project filter

        Returns:
            CSV content as string
        """
        import csv
        import io
        import html
        import time

        # Get all entries for date range
        entries = self.get_entries(start_date, end_date)

        # Filter by project if specified
        if project_id:
            entries = [e for e in entries if e.get('project_id') == project_id]

        # Sort entries by start date (earliest first)
        def get_entry_sort_key(entry):
            # Use start_time if available, otherwise use date
            if entry.get('start_time'):
                return entry.get('start_time')
            elif entry.get('date'):
                return entry.get('date')
            else:
                # Fallback to entry ID if no date info
                return str(entry.get('id', 0)).zfill(20)

        entries.sort(key=get_entry_sort_key)

        # Build task cache - fetch all unique tasks upfront
        task_cache = {}
        unique_task_ids = set(e.get('task_id') for e in entries if e.get('task_id'))

        for task_id in unique_task_ids:
            try:
                time.sleep(2)  # 2 second delay to avoid rate limits
                task_response = self._request('GET', f'tasks/{task_id}')
                task_data = task_response.get('tasks', [{}])[0] if 'tasks' in task_response else {}
                task_cache[task_id] = task_data.get('name', '')
            except Exception as e:
                # If we hit a rate limit, wait and retry once
                if '429' in str(e):
                    print(f"Rate limit hit, waiting 6 seconds...")
                    time.sleep(6)
                    try:
                        task_response = self._request('GET', f'tasks/{task_id}')
                        task_data = task_response.get('tasks', [{}])[0] if 'tasks' in task_response else {}
                        task_cache[task_id] = task_data.get('name', '')
                    except Exception as retry_err:
                        print(f"Warning: Failed to fetch task {task_id} after retry: {retry_err}")
                        task_cache[task_id] = ''
                else:
                    print(f"Warning: Failed to fetch task {task_id}: {e}")
                    task_cache[task_id] = ''

        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)

        # Header
        writer.writerow(['Date', 'Start Time', 'End Time', 'Duration (hours)',
                        'Task', 'Description', 'Billed', 'Entry ID'])

        # Rows
        for entry in entries:
            # Get task name from cache
            task_id = entry.get('task_id')
            task_name = task_cache.get(task_id, '') if task_id else ''

            # Clean description (strip HTML tags and decode entities)
            description = entry.get('description', '')
            if description:
                # Remove HTML tags
                import re
                description = re.sub(r'<[^>]+>', '', description)
                # Decode HTML entities
                description = html.unescape(description)
                description = description.strip()

            # Calculate duration
            if entry.get('duration'):
                duration_hours = entry['duration'] / 3600
            else:
                start = dateparser.parse(entry.get('start_time', ''))
                end = dateparser.parse(entry.get('end_time', ''))
                duration_hours = (end - start).total_seconds() / 3600 if start and end else 0

            writer.writerow([
                entry.get('date', ''),
                entry.get('start_time', ''),
                entry.get('end_time', ''),
                f"{duration_hours:.2f}",
                task_name,
                description,
                'Yes' if entry.get('billed') else 'No',
                entry.get('id', '')
            ])

        return output.getvalue()

    def find_project_by_name(self, name: str) -> Optional[Dict]:
        """Find project by partial name match (case-insensitive)"""
        projects = self.get_projects()
        name_lower = name.lower()

        for project in projects:
            if name_lower in project.get('name', '').lower():
                return project

        return None

    def find_task_by_name(self, project_id: int, name: str) -> Optional[Dict]:
        """Find task within project by name"""
        tasks = self.get_tasks(project_id)
        name_lower = name.lower()

        for task in tasks:
            if name_lower in task.get('name', '').lower():
                return task

        return None


class TimesheetProcessor:
    """Process timesheet YAML and create Paymo entries"""

    def __init__(self, client: PaymoClient, config: Dict):
        self.client = client
        self.config = config
        self.default_tz = pytz.timezone(config.get('timezone', 'America/Chicago'))

    def load_timesheet(self, filepath: str) -> Dict:
        """Load and validate timesheet YAML"""
        with open(filepath, 'r') as f:
            data = yaml.safe_load(f)

        # Validate required fields
        if 'entries' not in data:
            raise ValueError("Timesheet must have 'entries' field")

        return data

    def resolve_project_task(self, matter: str) -> Tuple[int, int]:
        """Resolve matter name to (project_id, task_id)"""
        # First check config mappings
        projects_config = self.config.get('projects', {})

        if matter in projects_config:
            project_id = projects_config[matter].get('project_id')
            task_id = projects_config[matter].get('task_id')
            return project_id, task_id

        # Otherwise search by name
        project = self.client.find_project_by_name(matter)
        if not project:
            raise ValueError(f"Could not find project matching '{matter}'")

        project_id = project['id']

        # Get first task in project (or could prompt user)
        tasks = self.client.get_tasks(project_id)
        if not tasks:
            raise ValueError(f"Project '{project['name']}' has no tasks")

        task_id = tasks[0]['id']

        console.print(f"[yellow]Using project: {project['name']} (ID: {project_id})[/yellow]")
        console.print(f"[yellow]Using task: {tasks[0]['name']} (ID: {task_id})[/yellow]")

        return project_id, task_id

    def convert_to_utc(self, date: str, time: str, tz: str) -> str:
        """Convert local datetime to UTC ISO format"""
        timezone = pytz.timezone(tz) if tz else self.default_tz

        # Parse date and time
        dt_str = f"{date} {time}"
        local_dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M')

        # Localize and convert to UTC
        local_dt = timezone.localize(local_dt)
        utc_dt = local_dt.astimezone(pytz.UTC)

        return utc_dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    def process_entry(self, entry: Dict, task_id: int) -> Dict:
        """Convert timesheet entry to Paymo API format"""
        # Allow entry to override task_id
        entry_task_id = entry.get('task_id', task_id)
        api_entry = {'task_id': entry_task_id}

        # Get timezone
        tz = entry.get('timezone', self.config.get('timezone', 'America/Chicago'))

        # Handle two formats: (start_time, end_time) or (duration_hours)
        if 'start_time' in entry and 'end_time' in entry:
            # Convert to UTC
            api_entry['start_time'] = self.convert_to_utc(
                entry['date'], entry['start_time'], tz
            )
            api_entry['end_time'] = self.convert_to_utc(
                entry['date'], entry['end_time'], tz
            )
        elif 'duration_hours' in entry:
            # Use date + duration
            api_entry['date'] = entry['date']
            api_entry['duration'] = int(entry['duration_hours'] * 3600)
        else:
            raise ValueError(f"Entry must have either (start_time, end_time) or duration_hours: {entry}")

        # Add description
        if 'description' in entry:
            api_entry['description'] = entry['description']

        # Add billed flag if specified
        if 'billed' in entry:
            api_entry['billed'] = entry['billed']

        return api_entry

    def calculate_duration(self, entry: Dict) -> float:
        """Calculate duration in hours for preview"""
        if 'duration_hours' in entry:
            return entry['duration_hours']

        # Calculate from start/end times
        tz = entry.get('timezone', self.config.get('timezone', 'America/Chicago'))
        timezone = pytz.timezone(tz)

        start_str = f"{entry['date']} {entry['start_time']}"
        end_str = f"{entry['date']} {entry['end_time']}"

        start_dt = datetime.strptime(start_str, '%Y-%m-%d %H:%M')
        end_dt = datetime.strptime(end_str, '%Y-%m-%d %H:%M')

        start_dt = timezone.localize(start_dt)
        end_dt = timezone.localize(end_dt)

        duration = (end_dt - start_dt).total_seconds() / 3600
        return duration

    def preview(self, filepath: str) -> List[Dict]:
        """Preview entries without creating"""
        data = self.load_timesheet(filepath)
        entries = data['entries']
        matter = data.get('matter', 'Unknown')
        rate = data.get('rate', 0)

        # Create table
        table = Table(title=f"Timesheet Preview: {matter}")
        table.add_column("Date", style="cyan")
        table.add_column("Time", style="magenta")
        table.add_column("Duration", style="green")
        table.add_column("Hours", style="yellow")
        table.add_column("Description", style="white")

        total_hours = 0

        for entry in entries:
            date = entry['date']
            duration_hours = self.calculate_duration(entry)
            total_hours += duration_hours

            # Format time range or duration
            if 'start_time' in entry:
                time_str = f"{entry['start_time']}-{entry['end_time']}"
            else:
                time_str = "—"

            # Format duration
            hours = int(duration_hours)
            minutes = int((duration_hours - hours) * 60)
            duration_str = f"{hours}:{minutes:02d}"

            description = entry.get('description', '')
            if len(description) > 50:
                description = description[:47] + "..."

            table.add_row(
                date,
                time_str,
                duration_str,
                f"{duration_hours:.2f}",
                description
            )

        console.print(table)

        # Summary
        total_billing = total_hours * rate if rate else 0
        console.print(f"\n[bold]Total: {total_hours:.2f} hours[/bold]", end="")
        if rate:
            console.print(f" [bold green](${total_billing:,.2f} at ${rate}/hr)[/bold green]")
        else:
            console.print()

        return entries

    def submit(self, filepath: str, dry_run: bool = False, auto_confirm: bool = False) -> List[Dict]:
        """Create all entries from timesheet"""
        data = self.load_timesheet(filepath)
        entries = data['entries']
        matter = data.get('matter')

        if not matter:
            raise ValueError("Timesheet must specify 'matter' field")

        # Resolve project and task
        console.print(f"\n[bold]Resolving project for matter: {matter}[/bold]")
        project_id, task_id = self.resolve_project_task(matter)

        # Preview first
        console.print(f"\n[bold]Preview of entries to create:[/bold]")
        self.preview(filepath)

        if dry_run:
            console.print("\n[yellow]Dry run - no entries created[/yellow]")
            return []

        # Confirm
        if not auto_confirm:
            if not click.confirm("\nCreate these entries in Paymo?"):
                console.print("[yellow]Cancelled[/yellow]")
                return []
        else:
            console.print("\n[green]Auto-confirmed - proceeding with creation[/green]")

        # Create entries as batch
        console.print(f"\n[bold]Creating {len(entries)} entries in batch...[/bold]")

        try:
            # Process all entries
            api_entries = [self.process_entry(entry, task_id) for entry in entries]

            # Try batch creation first
            try:
                result = self.client.create_entries_batch(api_entries)
                console.print(f"[green]✓ Successfully created {len(entries)} entries in one API call[/green]")
                return result
            except Exception as batch_error:
                # If batch fails, fall back to individual creation
                console.print(f"[yellow]Batch creation failed, trying individual entries...[/yellow]")
                console.print(f"[yellow]Error: {batch_error}[/yellow]")

                created = []
                for i, (entry, api_entry) in enumerate(zip(entries, api_entries), 1):
                    try:
                        console.print(f"[{i}/{len(entries)}] Creating entry for {entry['date']}...", end=" ")
                        result = self.client.create_entry(**api_entry)
                        created.append(result)
                        console.print("[green]✓[/green]")

                        # Add delay between calls to avoid rate limiting
                        if i < len(entries):
                            time.sleep(2)
                    except requests.exceptions.HTTPError as e:
                        if e.response.status_code == 429:
                            retry_after = getattr(e, 'retry_after', 60)
                            console.print(f"[yellow]⏳ Rate limited, waiting {retry_after}s...[/yellow]")
                            time.sleep(retry_after)
                            # Retry this entry
                            try:
                                result = self.client.create_entry(**api_entry)
                                created.append(result)
                                console.print("[green]✓ (after retry)[/green]")
                            except Exception as retry_error:
                                console.print(f"[red]✗ Retry failed: {retry_error}[/red]")
                        else:
                            console.print(f"[red]✗ Error: {e}[/red]")
                    except Exception as e:
                        console.print(f"[red]✗ Error: {e}[/red]")

                console.print(f"\n[bold green]Successfully created {len(created)} entries[/bold green]")
                return created

        except Exception as e:
            console.print(f"[red]Error processing entries: {e}[/red]")
            return []


def load_config() -> Dict:
    """Load configuration from ~/.paymo/config.yaml"""
    config_path = Path.home() / '.paymo' / 'config.yaml'

    if not config_path.exists():
        console.print(f"[yellow]Warning: Config file not found at {config_path}[/yellow]")
        console.print("[yellow]Using environment variable PAYMO_API_KEY or will prompt[/yellow]")
        return {
            'timezone': 'America/Chicago',
            'projects': {}
        }

    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


@click.group()
def cli():
    """Paymo Timesheet Automation Tool"""
    pass


@cli.command()
def list_projects():
    """List all active Paymo projects"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)
    projects = client.get_projects()

    table = Table(title="Paymo Projects")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Client", style="magenta")
    table.add_column("Active", style="green")

    for project in projects:
        table.add_row(
            str(project['id']),
            project.get('name', ''),
            project.get('client_name', ''),
            "✓" if project.get('active') else "✗"
        )

    console.print(table)


@cli.command()
@click.option('--project-id', type=int, required=True, help='Project ID')
def list_tasks(project_id: int):
    """List tasks for a project"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)
    tasks = client.get_tasks(project_id)

    table = Table(title=f"Tasks for Project {project_id}")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Billable", style="green")

    for task in tasks:
        table.add_row(
            str(task['id']),
            task.get('name', ''),
            "✓" if task.get('billable') else "✗"
        )

    console.print(table)


@cli.command()
@click.option('--start', help='Start date (YYYY-MM-DD)')
@click.option('--end', help='End date (YYYY-MM-DD)')
def list_entries(start: str, end: str):
    """List time entries for a date range"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)
    entries = client.get_entries(start, end)

    table = Table(title=f"Time Entries ({start} to {end})")
    table.add_column("ID", style="cyan")
    table.add_column("Date", style="magenta")
    table.add_column("Duration", style="green")
    table.add_column("Description", style="white")

    total_seconds = 0

    for entry in entries:
        entry_id = str(entry['id'])
        date = entry.get('date', '')

        # Calculate duration
        if entry.get('duration'):
            duration_sec = entry['duration']
        else:
            start_time = dateparser.parse(entry.get('start_time', ''))
            end_time = dateparser.parse(entry.get('end_time', ''))
            duration_sec = (end_time - start_time).total_seconds()

        total_seconds += duration_sec
        hours = int(duration_sec / 3600)
        minutes = int((duration_sec % 3600) / 60)
        duration_str = f"{hours}:{minutes:02d}"

        description = entry.get('description', '')[:50]

        table.add_row(entry_id, date, duration_str, description)

    console.print(table)

    total_hours = total_seconds / 3600
    console.print(f"\n[bold]Total: {total_hours:.2f} hours[/bold]")


@cli.command()
@click.argument('filepath', type=click.Path(exists=True))
def preview(filepath: str):
    """Preview timesheet entries without creating them"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)
    processor = TimesheetProcessor(client, config)

    processor.preview(filepath)


@cli.command()
@click.argument('filepath', type=click.Path(exists=True))
@click.option('--dry-run', is_flag=True, help='Preview only, do not create entries')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation prompt')
def submit(filepath: str, dry_run: bool, yes: bool):
    """Submit timesheet entries to Paymo"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)
    processor = TimesheetProcessor(client, config)

    processor.submit(filepath, dry_run=dry_run, auto_confirm=yes)


@cli.command()
@click.argument('entry_ids', nargs=-1, type=int, required=True)
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation prompt')
def delete(entry_ids: tuple, yes: bool):
    """Delete time entries by ID"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)

    console.print(f"\n[bold red]About to delete {len(entry_ids)} entries:[/bold red]")
    for entry_id in entry_ids:
        console.print(f"  - Entry ID: {entry_id}")

    if not yes:
        if not click.confirm("\nAre you sure you want to delete these entries?"):
            console.print("[yellow]Cancelled[/yellow]")
            return

    deleted = 0
    for entry_id in entry_ids:
        try:
            console.print(f"Deleting entry {entry_id}...", end=" ")
            client.delete_entry(entry_id)
            console.print("[green]✓[/green]")
            deleted += 1
        except Exception as e:
            console.print(f"[red]✗ Error: {e}[/red]")

    console.print(f"\n[bold green]Successfully deleted {deleted} entries[/bold green]")


# Main entry point moved to end


@cli.command()
@click.option('--client-id', type=int, help='Filter by client ID')
def list_invoices(client_id: Optional[int]):
    """List Paymo invoices"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)
    invoices = client.get_invoices(client_id)

    table = Table(title="Paymo Invoices")
    table.add_column("ID", style="cyan")
    table.add_column("Number", style="white")
    table.add_column("Client", style="magenta")
    table.add_column("Amount", style="green")
    table.add_column("Date", style="yellow")
    table.add_column("Status", style="blue")

    for invoice in invoices:
        table.add_row(
            str(invoice.get('id', '')),
            invoice.get('number', ''),
            invoice.get('client_name', ''),
            f"${invoice.get('total', 0):,.2f}",
            invoice.get('date', ''),
            invoice.get('status', '')
        )

    console.print(table)


@cli.command()
@click.option('--start', required=True, help='Start date (YYYY-MM-DD)')
@click.option('--end', required=True, help='End date (YYYY-MM-DD)')
@click.option('--project-id', type=int, help='Filter by project ID')
@click.option('--output', '-o', help='Output file path')
def export_timesheet(start: str, end: str, project_id: Optional[int], output: Optional[str]):
    """Export timesheet to CSV"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)

    console.print(f"\n[bold]Exporting timesheet: {start} to {end}[/bold]")
    if project_id:
        console.print(f"[yellow]Project ID: {project_id}[/yellow]")

    try:
        csv_content = client.export_timesheet_csv(start, end, project_id)

        # Determine output filename
        if not output:
            output = f"paymo_timesheet_{start}_{end}.csv"

        # Save file
        with open(output, 'w') as f:
            f.write(csv_content)

        console.print(f"[green]✓ Exported to: {output}[/green]")
        console.print(f"[green]  Size: {len(csv_content):,} bytes[/green]")

    except Exception as e:
        console.print(f"[red]Error exporting timesheet: {e}[/red]")
        raise


# MCP Server Implementation
if MCP_AVAILABLE:
    mcp = FastMCP("Paymo Timesheet Manager")

    @mcp.tool()
    def list_paymo_projects() -> List[Dict[str, Any]]:
        """List all active Paymo projects"""
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured in ~/.paymo/config.yaml")

        client = PaymoClient(api_key)
        return client.get_projects()

    @mcp.tool()
    def list_paymo_tasks(project_id: int) -> List[Dict[str, Any]]:
        """List tasks for a specific Paymo project"""
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        return client.get_tasks(project_id)

    @mcp.tool()
    def create_paymo_entry(
        task_id: int,
        date: str,
        duration_hours: float,
        description: str
    ) -> Dict[str, Any]:
        """
        Create a single time entry in Paymo

        Args:
            task_id: Paymo task ID
            date: Date in YYYY-MM-DD format
            duration_hours: Hours worked
            description: Entry description
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)

        return client.create_entry(
            task_id=task_id,
            date=date,
            duration=int(duration_hours * 3600),
            description=description
        )

    @mcp.tool()
    def submit_paymo_timesheet(yaml_content: str) -> Dict[str, Any]:
        """
        Submit a complete timesheet from YAML content

        Args:
            yaml_content: YAML timesheet content with entries

        Returns:
            Summary of created entries
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        # Parse YAML
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            client = PaymoClient(api_key)
            processor = TimesheetProcessor(client, config)
            created = processor.submit(yaml_file, auto_confirm=True)

            return {
                "success": True,
                "entries_created": len(created),
                "entries": created
            }
        finally:
            os.unlink(yaml_file)

    @mcp.tool()
    def export_paymo_timesheet(
        start_date: str,
        end_date: str,
        project_id: Optional[int] = None,
        format: str = "xls"
    ) -> str:
        """
        Export timesheet to XLS/CSV

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            project_id: Optional project filter
            format: 'xls' or 'csv'

        Returns:
            Path to exported file
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        content = client.export_timesheet(start_date, end_date, format, project_id)

        # Save to temp file
        output_path = f"/tmp/paymo_timesheet_{start_date}_{end_date}.{format}"
        with open(output_path, 'wb') as f:
            f.write(content)

        return output_path

    @mcp.tool()
    def list_paymo_invoices(client_id: Optional[int] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List Paymo invoices

        Args:
            client_id: Filter by client ID
            status: Filter by status (sent, viewed, paid)
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        return client.get_invoices(client_id, status)

    @mcp.tool()
    def get_outstanding_invoices_last_week() -> List[Dict[str, Any]]:
        """Get outstanding invoices (sent or viewed) from the last 7 days"""
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        return client.get_outstanding_invoices_last_week()

    @mcp.tool()
    def export_invoice_timesheet(invoice_id: int) -> str:
        """
        Export timesheet CSV for a specific invoice

        Args:
            invoice_id: Invoice ID to export

        Returns:
            Path to exported CSV file
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)

        # Get invoice
        inv = client.get_invoice(invoice_id)
        inv_number = inv.get('number', f'INV-{invoice_id}')
        inv_date = inv.get('date', '')

        # Calculate billing period
        if inv_date:
            from datetime import datetime
            inv_dt = datetime.strptime(inv_date, '%Y-%m-%d')
            start_date = inv_dt.replace(day=1).strftime('%Y-%m-%d')
            end_date = inv_date
        else:
            from datetime import datetime
            now = datetime.now()
            start_date = now.replace(day=1).strftime('%Y-%m-%d')
            end_date = now.strftime('%Y-%m-%d')

        # Export - use invoice-specific method to get only entries on this invoice
        csv_content = client.export_invoice_entries_csv(invoice_id)

        # Save
        filename = f"{inv_number.replace('#', '').replace('/', '-')}_timesheet.csv"
        output_path = f"/tmp/{filename}"

        with open(output_path, 'w') as f:
            f.write(csv_content)

        return output_path

    @mcp.tool()
    def list_paymo_entries(
        start_date: str,
        end_date: str,
        project_id: Optional[int] = None,
        billed: Optional[bool] = None
    ) -> List[Dict[str, Any]]:
        """
        List time entries with optional filters

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            project_id: Optional project filter
            billed: Optional filter - True for billed, False for unbilled, None for all

        Returns:
            List of time entries with task names, durations, descriptions
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)

        # Get entries
        entries = client.get_entries(start_date, end_date)

        # Filter by project if specified
        if project_id is not None:
            entries = [e for e in entries if e.get('project_id') == project_id]

        # Filter by billed status if specified
        if billed is not None:
            entries = [e for e in entries if e.get('billed') == billed]

        # Enhance entries with task names and readable data
        result = []
        for entry in entries:
            # Get task name
            task_id = entry.get('task_id')
            task_name = ''
            if task_id:
                try:
                    task_response = client._request('GET', f'tasks/{task_id}')
                    task_data = task_response.get('tasks', [{}])[0]
                    task_name = task_data.get('name', '')
                except:
                    task_name = f'Task {task_id}'

            # Calculate duration in hours
            duration_hours = entry.get('duration', 0) / 3600 if entry.get('duration') else 0

            # Clean description
            description = entry.get('description', '')
            if description:
                import re
                import html
                description = re.sub(r'<[^>]+>', '', description)
                description = html.unescape(description).strip()

            result.append({
                'id': entry.get('id'),
                'project_id': entry.get('project_id'),
                'task_id': task_id,
                'task_name': task_name,
                'date': entry.get('date', ''),
                'start_time': entry.get('start_time', ''),
                'end_time': entry.get('end_time', ''),
                'duration_hours': round(duration_hours, 2),
                'description': description,
                'billed': entry.get('billed', False),
                'price': entry.get('price', 0)
            })

        return result


def run_mcp_server():
    """Run as MCP server"""
    if not MCP_AVAILABLE:
        console.print("[red]Error: fastmcp not installed. Install with: pip install fastmcp[/red]")
        sys.exit(1)

    console.print("[bold green]Starting Paymo MCP Server...[/bold green]")
    mcp.run()



@cli.command()
@click.option('--status', help='Filter by status (sent, viewed, paid)')
@click.option('--last-week', is_flag=True, help='Only show invoices from last 7 days')
def list_invoices_filtered(status: Optional[str], last_week: bool):
    """List Paymo invoices with filters"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)

    if last_week:
        invoices = client.get_outstanding_invoices_last_week()
        console.print(f"\n[bold]Outstanding invoices from last 7 days[/bold]\n")
    else:
        invoices = client.get_invoices(status=status)

    table = Table(title="Paymo Invoices")
    table.add_column("ID", style="cyan")
    table.add_column("Number", style="white")
    table.add_column("Client", style="magenta")
    table.add_column("Amount", style="green")
    table.add_column("Date", style="yellow")
    table.add_column("Status", style="blue")

    total = 0
    for invoice in invoices:
        amount = invoice.get('total', 0)
        total += amount

        table.add_row(
            str(invoice.get('id', '')),
            invoice.get('number', ''),
            invoice.get('client_name', ''),
            f"${amount:,.2f}",
            invoice.get('date', ''),
            invoice.get('status', '')
        )

    console.print(table)
    console.print(f"\n[bold]Total: ${total:,.2f}[/bold]")
    console.print(f"[bold]Count: {len(invoices)} invoices[/bold]\n")


@cli.command()
@click.option('--invoice-id', type=int, help='Specific invoice ID')
@click.option('--last-week', is_flag=True, help='Export for all outstanding invoices from last week')
@click.option('--output-dir', '-o', default='.', help='Output directory for exports')
def export_invoice_timesheets(invoice_id: Optional[int], last_week: bool, output_dir: str):
    """Export timesheets for invoice(s)"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)

    # Determine which invoices to export
    if invoice_id:
        invoices = [client.get_invoice(invoice_id)]
    elif last_week:
        invoices = client.get_outstanding_invoices_last_week()
        console.print(f"\n[bold]Found {len(invoices)} outstanding invoices from last week[/bold]\n")
    else:
        console.print("[red]Error: Must specify --invoice-id or --last-week[/red]")
        return

    if not invoices:
        console.print("[yellow]No invoices found[/yellow]")
        return

    # Export each invoice
    import os
    os.makedirs(output_dir, exist_ok=True)

    for inv in invoices:
        inv_id = inv.get('id')
        inv_number = inv.get('number', f'INV-{inv_id}')
        inv_date = inv.get('date', '')

        # Get invoice details to find time entries
        # Use invoice date and calculate billing period (usually monthly)
        if inv_date:
            from datetime import datetime, timedelta
            inv_dt = datetime.strptime(inv_date, '%Y-%m-%d')

            # Assume monthly billing - use first day of month to invoice date
            start_date = inv_dt.replace(day=1).strftime('%Y-%m-%d')
            end_date = inv_date
        else:
            # Fallback - use current month
            from datetime import datetime
            now = datetime.now()
            start_date = now.replace(day=1).strftime('%Y-%m-%d')
            end_date = now.strftime('%Y-%m-%d')

        console.print(f"\n[bold]Exporting: {inv_number}[/bold]")
        console.print(f"  Period: {start_date} to {end_date}")
        console.print(f"  Amount: ${inv.get('total', 0):,.2f}")

        try:
            # Export timesheet - use invoice-specific method to get only entries on this invoice
            csv_content = client.export_invoice_entries_csv(inv_id)

            # Save file
            filename = f"{inv_number.replace('#', '').replace('/', '-')}_timesheet.csv"
            output_path = os.path.join(output_dir, filename)

            with open(output_path, 'w') as f:
                f.write(csv_content)

            console.print(f"  [green]✓ Saved: {output_path}[/green]")

        except Exception as e:
            console.print(f"  [red]✗ Error: {e}[/red]")

    console.print(f"\n[bold green]Exported {len(invoices)} invoice timesheets[/bold green]\n")


if __name__ == '__main__':
    # Check if running as MCP server
    if len(sys.argv) > 1 and sys.argv[1] == 'mcp':
        run_mcp_server()
    else:
        cli()
