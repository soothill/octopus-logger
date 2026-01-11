# Octopus Logger

A lightweight Python service that logs real-time power consumption data from your **Octopus Home Mini** (via the **Octopus Energy GraphQL API**) into an **InfluxDB** database.

It includes a systemd service for continuous operation and a Makefile for easy setup and management.

---

## Features
- Fetches live consumption data from the Octopus Energy GraphQL API.
- Writes readings to InfluxDB with numeric `power_numeric` field for mathematical operations.
- Caches readings locally if InfluxDB is unavailable.
- Automatically flushes cached data when InfluxDB reconnects.
- Runs as a systemd service.

---

## Requirements
- Python 3.8+
- InfluxDB 2.x
- Octopus Energy account with API access

---

## Getting an Octopus Energy API Key
1. Log in to your [Octopus Energy account](https://octopus.energy/dashboard/developer/).
2. Navigate to **Developer > API Keys**.
3. Generate a new API key.
4. Copy the key and paste it into your `config/config.yaml` under `octopus.api_key`.

---

## Configuration
Copy the sample config and edit it:

```bash
cp config/config.sample.yaml config/config.yaml
```

Example `config/config.yaml`:

```yaml
octopus:
  api_key: "YOUR_OCTOPUS_API_KEY"        # Obtain from https://octopus.energy/dashboard/developer/
  account_number: "A-XXXXXXXX"           # Your Octopus account number (e.g. A-1234ABCD)

influxdb:
  url: "http://localhost:8086"           # InfluxDB instance URL
  token: "YOUR_INFLUXDB_TOKEN"           # InfluxDB authentication token
  org: "YOUR_ORG"                        # InfluxDB organization name
  bucket: "YOUR_BUCKET"                  # InfluxDB bucket name

# Polling interval in seconds (default 10s)
poll_interval: 10

# Maximum number of cached readings if InfluxDB is unavailable (default 10000)
max_cache_size: 10000

# Enable debug logging (default false)
debug: false

# systemd service rendering (used during `make install`)
# If omitted, sensible defaults are derived from the repo location.
# Uncomment and edit to customize:
#
# systemd:
#   working_directory: "/path/to/octopus-logger"
#   python_executable: "/path/to/octopus-logger/.venv/bin/python"
#   main_path: "/path/to/octopus-logger/src/main.py"
#   user: "your_username"
#   read_write_paths:
#     - "/path/to/octopus-logger"   # directory must exist; allows writes to cache/ and log
```

**Notes:**
- Default polling interval is **10 seconds**.
- Avoid setting it too low to stay within Octopus API rate limits.

---

## Installation
To install and start the service:

```bash
make install
```

This will:
- Create a virtualenv (`.venv`)
- Install dependencies
- Render `systemd/octopus-logger.service` from `config/config.yaml`
- Copy the systemd service file
- Enable and start the service

---

## Testing
To run the logger once (without installing as a service):

```bash
make test
```

---

## Managing the Service
```bash
make start     # Start the service
make stop      # Stop the service
make status    # Show service status
make uninstall # Remove the service
```

---

## Logs
View logs using:

```bash
journalctl -u octopus-logger.service -f
```

---

## InfluxDB Data Schema

Data is written to the `power` measurement with the following field:

| Field | Type | Description |
|-------|------|-------------|
| `power_numeric` | float | Power demand in watts (numeric for mathematical operations) |

### Example Flux Queries

```flux
# Get average power over last hour
from(bucket: "your_bucket")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "power")
  |> filter(fn: (r) => r._field == "power_numeric")
  |> mean()

# Calculate total energy consumption (sum)
from(bucket: "your_bucket")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "power")
  |> filter(fn: (r) => r._field == "power_numeric")
  |> sum()
```

---

## Migrating Existing Data

If you have existing data with the old `value` field (stored as strings), you can migrate it to the new numeric `power_numeric` field:

```bash
# Preview what will be migrated (dry run)
python scripts/migrate_power_to_numeric.py --dry-run

# Run the actual migration
python scripts/migrate_power_to_numeric.py
```

The migration script:
- Reads all existing records from the `power` measurement
- Converts `value` fields to float
- Writes new `power_numeric` fields with the numeric values
- Preserves original timestamps
- Skips any records that cannot be converted to valid numbers

---

## Troubleshooting
- Ensure your `octopus.api_key` and `octopus.account_number` are correct.
- Verify InfluxDB is running and accessible.
- If InfluxDB is down, readings will be queued in `cache/pending_readings.json` and flushed once writes succeed again.

---

## License
MIT License
