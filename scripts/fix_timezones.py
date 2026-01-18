#!/usr/bin/env python3
"""
Fix CalDAV .ics files by adding missing VTIMEZONE components.
"""
import sys
from pathlib import Path
from icalendar import Calendar, Timezone
import pytz
from datetime import datetime

def get_timezones_from_event(cal):
    """Extract all timezone IDs referenced in an event."""
    timezones = set()
    for component in cal.walk():
        if component.name in ("VEVENT", "VTODO"):
            for prop in component.property_items():
                if hasattr(prop[1], 'params') and 'TZID' in prop[1].params:
                    tzid = prop[1].params['TZID']
                    timezones.add(tzid)
    return timezones

def has_vtimezone(cal, tzid):
    """Check if calendar already has a VTIMEZONE component for this timezone."""
    for component in cal.walk():
        if component.name == "VTIMEZONE":
            if str(component.get('TZID', '')) == tzid:
                return True
    return False

def add_vtimezone(cal, tzid):
    """Add VTIMEZONE component for the given timezone ID."""
    try:
        # Try to get the timezone from pytz
        tz = pytz.timezone(tzid)
        
        # Create a VTIMEZONE component
        vtimezone = Timezone()
        vtimezone.add('TZID', tzid)
        
        # Add to calendar (insert after VCALENDAR properties but before VEVENT/VTODO)
        cal.add_component(vtimezone)
        return True
    except Exception as e:
        print(f"Warning: Could not create VTIMEZONE for {tzid}: {e}")
        return False

def fix_ics_file(ics_file, dry_run=False):
    """Fix a single .ics file by adding missing VTIMEZONE components."""
    try:
        with open(ics_file, 'rb') as f:
            cal = Calendar.from_ical(f.read())
        
        # Get all timezones referenced in the event
        timezones = get_timezones_from_event(cal)
        
        if not timezones:
            return None  # No timezones to fix
        
        # Check which timezones are missing
        missing_timezones = []
        for tzid in timezones:
            if not has_vtimezone(cal, tzid):
                missing_timezones.append(tzid)
        
        if not missing_timezones:
            return None  # All timezones already present
        
        if dry_run:
            return missing_timezones
        
        # Add missing timezones using pytz
        for tzid in missing_timezones:
            try:
                tz = pytz.timezone(tzid)
                
                # Get current year transitions for this timezone
                now = datetime.now()
                transitions = []
                
                # Get the timezone at different times to capture DST changes
                for year in [now.year - 1, now.year, now.year + 1]:
                    # Standard time (winter)
                    dt_std = tz.localize(datetime(year, 1, 1, 12, 0, 0))
                    # Daylight time (summer)  
                    dt_dst = tz.localize(datetime(year, 7, 1, 12, 0, 0))
                    transitions.extend([dt_std, dt_dst])
                
                # Create VTIMEZONE component manually with proper iCalendar format
                tz_lines = [
                    "BEGIN:VTIMEZONE",
                    f"TZID:{tzid}",
                ]
                
                # Add at least one STANDARD and DAYLIGHT component if DST exists
                std_added = False
                dst_added = False
                
                for dt in transitions:
                    utc_offset = dt.strftime('%z')
                    dst_offset = dt.dst()
                    
                    if dst_offset and dst_offset.total_seconds() > 0 and not dst_added:
                        # Daylight time
                        tz_lines.extend([
                            "BEGIN:DAYLIGHT",
                            f"TZOFFSETFROM:{utc_offset}",
                            f"TZOFFSETTO:{utc_offset}",
                            f"TZNAME:{dt.tzname()}",
                            f"DTSTART:{dt.strftime('%Y%m%dT%H%M%S')}",
                            "END:DAYLIGHT",
                        ])
                        dst_added = True
                    elif (not dst_offset or dst_offset.total_seconds() == 0) and not std_added:
                        # Standard time
                        tz_lines.extend([
                            "BEGIN:STANDARD",
                            f"TZOFFSETFROM:{utc_offset}",
                            f"TZOFFSETTO:{utc_offset}",
                            f"TZNAME:{dt.tzname()}",
                            f"DTSTART:{dt.strftime('%Y%m%dT%H%M%S')}",
                            "END:STANDARD",
                        ])
                        std_added = True
                    
                    if std_added and dst_added:
                        break
                
                tz_lines.append("END:VTIMEZONE")
                
                # Parse the VTIMEZONE and add it to calendar
                tz_ical = '\r\n'.join(tz_lines)
                tz_component = Calendar.from_ical(f"BEGIN:VCALENDAR\r\n{tz_ical}\r\nEND:VCALENDAR\r\n")
                
                for component in tz_component.walk():
                    if component.name == "VTIMEZONE":
                        cal.add_component(component)
                        break
                        
            except Exception as e:
                print(f"Error adding timezone {tzid}: {e}")
                continue
        
        # Write back to file
        with open(ics_file, 'wb') as f:
            f.write(cal.to_ical())
        
        return missing_timezones
        
    except Exception as e:
        print(f"Error processing {ics_file}: {e}")
        return None

def fix_calendar_directory(caldav_path, dry_run=False):
    """Fix all .ics files in a calendar directory."""
    if not caldav_path.exists():
        print(f"Directory does not exist: {caldav_path}")
        return
    
    total_files = 0
    fixed_files = 0
    
    # Process all subdirectories
    for cal_dir in caldav_path.iterdir():
        if not cal_dir.is_dir() or cal_dir.name.startswith('.'):
            continue
        
        print(f"\nProcessing calendar: {cal_dir.name}")
        
        for ics_file in cal_dir.glob("*.ics"):
            total_files += 1
            result = fix_ics_file(ics_file, dry_run=dry_run)
            
            if result:
                fixed_files += 1
                if dry_run:
                    print(f"  Would fix {ics_file.name}: missing {', '.join(result)}")
                else:
                    print(f"  Fixed {ics_file.name}: added {', '.join(result)}")
    
    print(f"\n{'Dry run:' if dry_run else 'Done!'} {fixed_files}/{total_files} files needed timezone fixes")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Fix CalDAV .ics files by adding missing VTIMEZONE components")
    parser.add_argument("caldav_path", help="Path to the caldav directory")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    
    args = parser.parse_args()
    
    caldav_path = Path(args.caldav_path)
    fix_calendar_directory(caldav_path, dry_run=args.dry_run)
