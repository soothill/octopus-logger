# Octopus Logger

A lightweight Python service that logs real-time power consumption data from your **Octopus Home Mini** via the **Octopus Energy API** into an **InfluxDB** database.  
It includes a systemd service for continuous operation and a Makefile for easy setup and management.

---

## Features
- Fetches live consumption data from the Octopus Energy API.
- Writes readings to InfluxDB.
- Caches readings locally if InfluxDB is unavailable.
- Automatically flushes cached data when InfluxDB reconnects.
- Runs as a systemd service.
- Includes a Makefile for installation, testing, and management.

---

## Requirements
- Python 3.8+
- InfluxDB 2.x
- Octopus Energy account with API access

---

## Getting an Octopus Energy API Key
1. Log in to your [Octopus Energy account](https://octopus.energy/dashboard/developer/).
2. Navigate to **Developer > API Keys**.
3. Generate a new API key (token).
4. Copy the key and paste it into your `config/config.yaml` file under `octopus.api_key`.

---

## Configuration
Edit `config/config.yaml` to include your details:

```yaml
octopus:
  api_key: "YOUR_OCTOPUS_API_KEY"
  mpan: "YOUR_MPAN"
  serial_number: "YOUR_METER_SERIAL"

influxdb:
  url: "http://localhost:8086"
  token: "YOUR_INFLUXDB_TOKEN"
  org: "YOUR_ORG"
  bucket: "YOUR_BUCKET"

poll_interval: 30
cache_file: "cache/pending_readings.json"
```

**Notes:**
- The default polling interval is **30 seconds**, which complies with Octopus API rate limits.
- You can adjust this interval if needed, but avoid setting it below 10 seconds.

---

## Installation
To install and start the service:

```bash
make install
```

This will:
- Install dependencies.
- Copy the systemd service file.
- Enable and start the service.

---

## Testing
To run the logger once (without installing as a service):

```bash
make test
```

This will execute the script once to verify configuration and connectivity.

---

## Managing the Service
```bash
make start   # Start the service
make stop    # Stop the service
make uninstall  # Remove the service
```

---

## Logs
View logs using:
```bash
journalctl -u octopus-logger.service -f
```

---

## Troubleshooting
- Ensure your API key, MPAN, and meter serial are correct.
- Verify InfluxDB is running and accessible.
- Check the cache file (`cache/pending_readings.json`) for unsent readings if InfluxDB is down.

---

## License
MIT License
