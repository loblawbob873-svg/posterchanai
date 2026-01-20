#!/usr/bin/env python3
"""
Fix settings table schema mismatch that causes IndexError.
Diagnoses and repairs the settings table structure.
"""
import sys
import os
import sqlite3
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import DATABASE_URL, SessionLocal
from sqlalchemy import text, inspect, create_engine

def diagnose_settings_table():
    """Check the settings table structure"""
    print("Diagnosing settings table...")
    
    # Parse database URL
    if DATABASE_URL.startswith('sqlite:///'):
        db_path = DATABASE_URL.replace('sqlite:///', '')
        if not os.path.isabs(db_path):
            db_path = os.path.join(os.getcwd(), db_path)
    else:
        print(f"Unsupported database URL: {DATABASE_URL}")
        return False
    
    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        return False
    
    print(f"Database file: {db_path}")
    
    # Check with SQLite directly
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Get table info
    cursor.execute("PRAGMA table_info(settings)")
    columns = cursor.fetchall()
    
    print(f"\nSettings table columns:")
    for col in columns:
        print(f"  {col[1]} ({col[2]}) - {'PRIMARY KEY' if col[5] else ''}")
    
    # Check for any rows with issues
    cursor.execute("SELECT COUNT(*) FROM settings")
    count = cursor.fetchone()[0]
    print(f"\nTotal settings: {count}")
    
    # Check for rows with NULL keys or values
    cursor.execute("SELECT COUNT(*) FROM settings WHERE key IS NULL OR value IS NULL")
    null_count = cursor.fetchone()[0]
    if null_count > 0:
        print(f"WARNING: {null_count} rows with NULL key or value")
    
    # Expected columns: key (PRIMARY KEY), value
    expected_cols = {'key', 'value'}
    actual_cols = {col[1] for col in columns}
    
    if actual_cols == expected_cols:
        print("\n✓ Schema looks correct!")
        # Check for data issues
        try:
            cursor.execute("SELECT key, value FROM settings LIMIT 5")
            rows = cursor.fetchall()
            print(f"\nSample rows:")
            for row in rows:
                print(f"  {row[0]} = {row[1][:50] if row[1] else 'NULL'}...")
        except Exception as e:
            print(f"\nERROR reading rows: {e}")
            return False
    else:
        print(f"\n✗ Schema mismatch!")
        print(f"  Expected columns: {expected_cols}")
        print(f"  Actual columns: {actual_cols}")
        extra_cols = actual_cols - expected_cols
        missing_cols = expected_cols - actual_cols
        if extra_cols:
            print(f"  Extra columns: {extra_cols}")
        if missing_cols:
            print(f"  Missing columns: {missing_cols}")
        return False
    
    conn.close()
    return True

def fix_settings_table():
    """Fix the settings table if needed"""
    print("\nAttempting to fix settings table...")
    
    # Parse database URL
    if DATABASE_URL.startswith('sqlite:///'):
        db_path = DATABASE_URL.replace('sqlite:///', '')
        if not os.path.isabs(db_path):
            db_path = os.path.join(os.getcwd(), db_path)
    else:
        print(f"Unsupported database URL: {DATABASE_URL}")
        return False
    
    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        return False
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # Backup current data
        cursor.execute("SELECT key, value FROM settings")
        backup_data = cursor.fetchall()
        print(f"Backed up {len(backup_data)} settings")
        
        # Check current schema
        cursor.execute("PRAGMA table_info(settings)")
        columns = cursor.fetchall()
        actual_cols = {col[1] for col in columns}
        expected_cols = {'key', 'value'}
        
        if actual_cols != expected_cols:
            print("Recreating settings table with correct schema...")
            
            # Drop and recreate table
            cursor.execute("DROP TABLE IF EXISTS settings_backup")
            cursor.execute("CREATE TABLE settings_backup AS SELECT * FROM settings")
            cursor.execute("DROP TABLE settings")
            cursor.execute("""
                CREATE TABLE settings (
                    key VARCHAR(100) PRIMARY KEY,
                    value TEXT
                )
            """)
            
            # Restore data (only key and value columns)
            for row in backup_data:
                if len(row) >= 2:
                    cursor.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?)",
                        (row[0], row[1])
                    )
                else:
                    print(f"WARNING: Skipping invalid row: {row}")
            
            conn.commit()
            print("✓ Settings table recreated successfully")
        else:
            print("Schema is correct, no fix needed")
        
        # Verify fix
        cursor.execute("SELECT COUNT(*) FROM settings")
        count = cursor.fetchone()[0]
        print(f"✓ Settings table now has {count} rows")
        
        conn.close()
        return True
        
    except Exception as e:
        print(f"ERROR fixing table: {e}")
        conn.rollback()
        conn.close()
        return False

def main():
    print("=" * 60)
    print("Settings Table Schema Fixer")
    print("=" * 60)
    
    if not diagnose_settings_table():
        print("\n" + "=" * 60)
        response = input("Schema issue detected. Fix it? (y/n): ")
        if response.lower() == 'y':
            if fix_settings_table():
                print("\n✓ Fix completed successfully!")
                print("Restart the service to apply changes.")
            else:
                print("\n✗ Fix failed. Check errors above.")
        else:
            print("Fix cancelled.")
    else:
        print("\nNo schema issues detected.")
        print("If you're still getting IndexError, it might be a data corruption issue.")
        print("Try running: sqlite3 posterchanai.db 'VACUUM;'")

if __name__ == "__main__":
    main()
