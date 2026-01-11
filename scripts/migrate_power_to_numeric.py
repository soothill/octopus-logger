#!/usr/bin/env python3
"""
Migration script to convert existing 'value' field in InfluxDB to numeric 'power_numeric' field.

This script:
1. Reads all existing data from the 'power' measurement
2. Converts the 'value' field to a float
3. Writes a new 'power_numeric' field with the numeric value

Usage:
    python scripts/migrate_power_to_numeric.py [--dry-run]

Options:
    --dry-run    Show what would be migrated without actually writing data
"""

import sys
import yaml
import logging
from pathlib import Path
from datetime import datetime, timezone
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"


def load_config():
    """Load configuration from config.yaml"""
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def safe_float(value, default=None):
    """Safely convert a value to float."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError) as e:
        logging.warning("Failed to convert value to float: %r - %s", value, e)
        return default


def migrate_data(dry_run=False):
    """Migrate existing 'value' field to numeric 'power_numeric' field."""
    config = load_config()
    
    influx_url = config["influxdb"]["url"]
    influx_token = config["influxdb"]["token"]
    influx_org = config["influxdb"]["org"]
    influx_bucket = config["influxdb"]["bucket"]
    
    logging.info("Connecting to InfluxDB at %s", influx_url)
    logging.info("Organization: %s, Bucket: %s", influx_org, influx_bucket)
    
    if dry_run:
        logging.info("DRY RUN MODE - No data will be written")
    
    client = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org)
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS)
    
    # Query all existing data with the 'value' field
    # We query for records that have 'value' field to migrate
    query = f'''
    from(bucket: "{influx_bucket}")
      |> range(start: 0)
      |> filter(fn: (r) => r._measurement == "power")
      |> filter(fn: (r) => r._field == "value")
    '''
    
    logging.info("Querying existing data...")
    
    try:
        tables = query_api.query(query, org=influx_org)
    except Exception as e:
        logging.error("Failed to query data: %s", e)
        client.close()
        return False
    
    total_records = 0
    converted_records = 0
    skipped_records = 0
    points_to_write = []
    
    for table in tables:
        for record in table.records:
            total_records += 1
            
            time = record.get_time()
            value = record.get_value()
            
            numeric_value = safe_float(value)
            
            if numeric_value is None:
                logging.warning("Skipping record at %s with invalid value: %r", time, value)
                skipped_records += 1
                continue
            
            point = (
                Point("power")
                .field("power_numeric", numeric_value)
                .time(time, WritePrecision.NS)
            )
            points_to_write.append(point)
            converted_records += 1
            
            # Batch writes to avoid memory issues
            if len(points_to_write) >= 1000:
                if not dry_run:
                    write_api.write(bucket=influx_bucket, org=influx_org, record=points_to_write)
                logging.info("Progress: %d records processed...", converted_records)
                points_to_write = []
    
    # Write remaining points
    if points_to_write:
        if not dry_run:
            write_api.write(bucket=influx_bucket, org=influx_org, record=points_to_write)
    
    logging.info("=" * 50)
    logging.info("Migration Summary:")
    logging.info("  Total records found: %d", total_records)
    logging.info("  Records converted: %d", converted_records)
    logging.info("  Records skipped (invalid): %d", skipped_records)
    
    if dry_run:
        logging.info("  DRY RUN - No data was written")
    else:
        logging.info("  Data written successfully!")
    
    write_api.close()
    client.close()
    
    return True


def main():
    dry_run = "--dry-run" in sys.argv
    
    logging.info("Starting power field migration...")
    logging.info("This will add a 'power_numeric' field to existing 'power' measurements")
    
    success = migrate_data(dry_run=dry_run)
    
    if success:
        logging.info("Migration completed successfully!")
        sys.exit(0)
    else:
        logging.error("Migration failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
