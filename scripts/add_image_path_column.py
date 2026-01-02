#!/usr/bin/env python3
"""Add image_path column to messages table"""
import sqlite3
import sys

def migrate(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check if column exists
    cursor.execute("PRAGMA table_info(messages)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if 'image_path' not in columns:
        print("Adding image_path column to messages table...")
        cursor.execute("ALTER TABLE messages ADD COLUMN image_path VARCHAR(500)")
        conn.commit()
        print("Migration complete!")
    else:
        print("Column image_path already exists")
    
    conn.close()

if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "/var/lib/posterchanai/posterchanai.db"
    migrate(db_path)
