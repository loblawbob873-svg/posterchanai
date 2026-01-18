#!/usr/bin/env python3
"""
Organize CalDAV calendars from flat structure to subdirectories.

This script moves .ics files from the flat caldav directory into
subdirectories based on the calendar name in the VCALENDAR component.
"""
import sys
from pathlib import Path
from icalendar import Calendar
import re

def sanitize_calendar_name(name: str) -> str:
    """Sanitize calendar name for use as directory name."""
    # Remove special characters, keep only alphanumeric, spaces, hyphens, underscores
    name = re.sub(r'[^\w\s-]', '', name)
    # Replace spaces with underscores
    name = name.replace(' ', '_')
    # Remove consecutive underscores
    name = re.sub(r'_+', '_', name)
    return name.strip('_').lower()

def organize_calendars(caldav_path: Path, dry_run: bool = False):
    """
    Organize calendar files into subdirectories.
    
    Args:
        caldav_path: Path to the user's caldav directory
        dry_run: If True, only print what would be done without making changes
    """
    if not caldav_path.exists():
        print(f"CalDAV path does not exist: {caldav_path}")
        return
    
    # Find all .ics files in the root directory
    ics_files = list(caldav_path.glob("*.ics"))
    
    if not ics_files:
        print("No .ics files found in caldav root directory")
        return
    
    print(f"Found {len(ics_files)} calendar files")
    
    # Group files by calendar name
    calendar_groups = {}
    no_calendar_name = []
    
    for ics_file in ics_files:
        try:
            with open(ics_file, 'rb') as f:
                cal = Calendar.from_ical(f.read())
            
            # Try to get calendar name from X-WR-CALNAME
            cal_name = None
            if 'X-WR-CALNAME' in cal:
                cal_name = str(cal['X-WR-CALNAME'])
            elif 'X-WR-CALENDAR-NAME' in cal:
                cal_name = str(cal['X-WR-CALENDAR-NAME'])
            
            # If no calendar name, use a default based on component type
            if not cal_name:
                # Check component type
                for component in cal.walk():
                    if component.name == "VEVENT":
                        cal_name = "events"
                        break
                    elif component.name == "VTODO":
                        cal_name = "todos"
                        break
                
                if not cal_name:
                    no_calendar_name.append(ics_file)
                    continue
            
            # Sanitize calendar name
            cal_name = sanitize_calendar_name(cal_name)
            if not cal_name:
                cal_name = "default"
            
            if cal_name not in calendar_groups:
                calendar_groups[cal_name] = []
            calendar_groups[cal_name].append(ics_file)
            
        except Exception as e:
            print(f"Error processing {ics_file.name}: {e}")
            no_calendar_name.append(ics_file)
    
    # If we have ungrouped files, put them in "default"
    if no_calendar_name:
        if "default" not in calendar_groups:
            calendar_groups["default"] = []
        calendar_groups["default"].extend(no_calendar_name)
    
    # Print summary
    print(f"\nCalendar organization:")
    for cal_name, files in sorted(calendar_groups.items()):
        print(f"  {cal_name}: {len(files)} events")
    
    if dry_run:
        print("\nDry run mode - no changes made")
        return
    
    # Create subdirectories and move files
    print("\nOrganizing calendars...")
    for cal_name, files in calendar_groups.items():
        cal_dir = caldav_path / cal_name
        cal_dir.mkdir(exist_ok=True)
        
        for ics_file in files:
            dest = cal_dir / ics_file.name
            try:
                ics_file.rename(dest)
                print(f"  Moved {ics_file.name} to {cal_name}/")
            except Exception as e:
                print(f"  Error moving {ics_file.name}: {e}")
    
    print(f"\nDone! Created {len(calendar_groups)} calendar collections")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Organize CalDAV calendars into subdirectories")
    parser.add_argument("caldav_path", help="Path to the caldav directory (e.g., /path/to/storage/username/caldav)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    
    args = parser.parse_args()
    
    caldav_path = Path(args.caldav_path)
    organize_calendars(caldav_path, dry_run=args.dry_run)
