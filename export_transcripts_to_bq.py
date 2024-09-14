#!/usr/bin/env python3
import sqlite3
import os
from google.cloud import bigquery
from google.oauth2 import service_account
from datetime import datetime, timedelta
import sys

# Configuration
SQLITE_DB_PATH = 'podcast_transcriber/db.sqlite3'
BIGQUERY_TABLE_ID = ''
CREDENTIALS_PATH = ''
LAST_SYNC_FILE = 'last_sync_time.txt'

# Initialize BigQuery client
credentials = service_account.Credentials.from_service_account_file(
    CREDENTIALS_PATH, scopes=["https://www.googleapis.com/auth/cloud-platform"],
)
bq_client = bigquery.Client(credentials=credentials, project=credentials.project_id)

def connect_to_sqlite():
    """Connect to the SQLite database."""
    return sqlite3.connect(SQLITE_DB_PATH)

def fetch_new_data(sqlite_conn, last_sync_time):
    """Fetch new data from SQLite database."""
    cursor = sqlite_conn.cursor()
    query = f"""
    SELECT * FROM podcast_transcriber_app_transcript
    WHERE created_at > '{last_sync_time}'
    ORDER BY created_at ASC
    """
    cursor.execute(query)
    columns = [description[0] for description in cursor.description]
    rows = cursor.fetchall()
    return columns, rows

def process_rows(columns, rows):
    """Process rows to remove numeric index if present."""
    processed_rows = []
    for row in rows:
        if row and str(row[0]).isdigit():
            processed_row = row[1:]
            processed_columns = columns[1:]
        else:
            processed_row = row
            processed_columns = columns
        processed_rows.append(dict(zip(processed_columns, processed_row)))
    return processed_rows

def append_to_bigquery(columns, rows):
    """Append new data to BigQuery table."""
    table = bq_client.get_table(BIGQUERY_TABLE_ID)
    processed_rows = process_rows(columns, rows)
    errors = bq_client.insert_rows_json(table, processed_rows)
    if errors:
        print(f"Errors occurred while inserting rows: {errors}")
    else:
        print(f"Successfully inserted {len(processed_rows)} rows into BigQuery")

    return processed_rows

def get_last_sync_time():
    """Get the last sync time from file or return a default time."""
    if os.path.exists(LAST_SYNC_FILE):
        with open(LAST_SYNC_FILE, 'r') as f:
            return datetime.fromisoformat(f.read().strip())
    return datetime.now() - timedelta(days=1)  # Default to 1 day ago

def save_last_sync_time(sync_time):
    """Save the last sync time to a file."""
    with open(LAST_SYNC_FILE, 'w') as f:
        f.write(sync_time)

def main():
    print(f"Starting sync process at {datetime.now()}")
    
    last_sync_time = get_last_sync_time()
    sqlite_conn = connect_to_sqlite()
    
    columns, new_data = fetch_new_data(sqlite_conn, last_sync_time)
    
    if new_data:
        added_rows = append_to_bigquery(columns, new_data)
        last_sync_time = max(row["created_at"] for row in added_rows)
        save_last_sync_time(last_sync_time)
    else:
        print("No new data to sync")
    
    sqlite_conn.close()
    
    print(f"Sync process completed at {datetime.now()}")

if __name__ == "__main__":
    main()