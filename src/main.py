import asyncio
import signal
import aiohttp
import json
import yaml
import logging
from logging.handlers import RotatingFileHandler
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from pathlib import Path
from datetime import datetime, timezone, timedelta

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"
CACHE_PATH = Path(__file__).parent.parent / "cache" / "pending_readings.json"
GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"

# Graceful shutdown event
shutdown_event = asyncio.Event()

############################################################
# CONFIG + CACHE
############################################################

def load_config():
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)

def load_cache():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            return []
    return []

def save_cache(cache, max_size=10000):
    """Save readings to cache, truncating to max_size if needed."""
    CACHE_PATH.parent.mkdir(exist_ok=True, parents=True)
    # Keep only the most recent readings if over max_size
    if len(cache) > max_size:
        cache = cache[-max_size:]
    CACHE_PATH.write_text(json.dumps(cache))

############################################################
# ASYNC GRAPHQL CLIENT
############################################################

async def graphql_request(session, query, variables=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"JWT {token}"

    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    logging.debug("GraphQL Payload: %s", payload)

    async with session.post(GRAPHQL_URL, json=payload, headers=headers) as resp:
        logging.debug("GraphQL HTTP Status: %s", resp.status)

        if resp.status >= 400:
            text = await resp.text()
            logging.error("GraphQL error response: %s", text)
            resp.raise_for_status()

        data = await resp.json()

        logging.debug("GraphQL Response: %s", data)

        if "errors" in data:
            raise RuntimeError(f"GraphQL error(s): {data['errors']}")

        return data["data"]

############################################################
# TOKEN + DEVICE ID
############################################################

async def obtain_token(session, api_key):
    query = """
    mutation ObtainToken($apiKey: String!) {
      obtainKrakenToken(input: { APIKey: $apiKey }) {
        token
      }
    }
    """
    data = await graphql_request(session, query, {"apiKey": api_key})
    token = data["obtainKrakenToken"]["token"]
    logging.debug("Obtained token: %s...", token[:20] if token else "None")
    return token

async def find_home_mini_device_id(session, account_number, token):
    query = """
    query HomeMiniDevice($accountNumber: String!) {
      account(accountNumber: $accountNumber) {
        electricityAgreements(active: true) {
          meterPoint {
            meters(includeInactive: false) {
              smartDevices {
                deviceId
              }
            }
          }
        }
      }
    }
    """
    data = await graphql_request(session, query, {"accountNumber": account_number}, token)
    account = data.get("account")
    if not account:
        raise RuntimeError("Account not found")

    for agreement in account.get("electricityAgreements", []):
        mp = agreement.get("meterPoint") or {}
        for meter in mp.get("meters", []):
            for dev in meter.get("smartDevices", []):
                if dev.get("deviceId"):
                    logging.debug("Found Home Mini deviceId: %s", dev['deviceId'])
                    return dev["deviceId"]

    raise RuntimeError("No smart meter deviceId found")

############################################################
# ASYNC TELEMETRY
############################################################

async def fetch_telemetry(session, device_id, token):
    now = datetime.now(timezone.utc)
    start = now - timedelta(seconds=300)

    query = """
    query Telemetry($deviceId: String!, $start: DateTime!, $end: DateTime!) {
      smartMeterTelemetry(
        deviceId: $deviceId
        grouping: TEN_SECONDS
        start: $start
        end: $end
      ) {
        readAt
        demand
        consumptionDelta
        costDelta
      }
    }
    """

    variables = {
        "deviceId": device_id,
        "start": start.isoformat(),
        "end": now.isoformat(),
    }

    data = await graphql_request(session, query, variables, token)
    pts = data.get("smartMeterTelemetry") or []
    if not pts:
        return None

    # Use max() instead of sorting - O(n) vs O(n log n)
    latest = max(pts, key=lambda x: x["readAt"])
    return {"timestamp": latest["readAt"], "value": latest["demand"]}

############################################################
# ASYNC RATE LIMIT INFO
############################################################

async def fetch_rate_limit_info(session, token):
    query = """
    query RateLimitInfo {
      rateLimitInfo {
        pointsAllowanceRateLimit {
          limit
          remainingPoints
          usedPoints
          ttl
          isBlocked
        }
        fieldSpecificRateLimits(first: 50) {
          edges {
            node {
              field
              rate
              ttl
              isBlocked
            }
          }
        }
      }
    }
    """

    data = await graphql_request(session, query, None, token)
    return data["rateLimitInfo"]

def log_rate_limit(rate_limit):
    """Helper to log rate limit info consistently."""
    pa = (rate_limit or {}).get("pointsAllowanceRateLimit") or {}
    logging.info(
        "Rate Limit Summary: PA remainingPoints=%s usedPoints=%s limit=%s ttl=%s isBlocked=%s",
        pa.get("remainingPoints"),
        pa.get("usedPoints"),
        pa.get("limit"),
        pa.get("ttl"),
        pa.get("isBlocked"),
    )

############################################################
# INFLUX WRITE (using reusable WriteApi)
############################################################

def write_influx_sync(write_api, org, bucket, reading):
    """Synchronous InfluxDB write using pre-created WriteApi."""
    point = (
        Point("power")
        .field("value", reading["value"])
        .time(reading["timestamp"], WritePrecision.S)
    )
    write_api.write(bucket=bucket, org=org, record=point)

async def write_influx(write_api, org, bucket, reading):
    """Async wrapper for InfluxDB write."""
    await asyncio.to_thread(write_influx_sync, write_api, org, bucket, reading)

############################################################
# ASYNC MAIN LOOP
############################################################

def setup_signal_handlers():
    """Setup graceful shutdown handlers for SIGTERM and SIGINT."""
    def handle_shutdown(signum, frame):
        signame = signal.Signals(signum).name
        logging.info("Received %s, initiating graceful shutdown...", signame)
        shutdown_event.set()
    
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

async def main():
    config = load_config()
    api_key = config["octopus"]["api_key"]
    account_number = config["octopus"]["account_number"]
    influx_url = config["influxdb"]["url"]
    influx_token = config["influxdb"]["token"]
    influx_org = config["influxdb"]["org"]
    influx_bucket = config["influxdb"]["bucket"]
    interval = config.get("poll_interval", 10)
    debug = config.get("debug", False)
    max_cache_size = config.get("max_cache_size", 10000)

    log_path = Path(__file__).parent.parent / "octopus_logger.log"
    file_handler = RotatingFileHandler(log_path, maxBytes=5*1024*1024, backupCount=3)
    syslog = logging.handlers.SysLogHandler(address="/dev/log")
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        handlers=[file_handler, syslog, logging.StreamHandler()],
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Suppress chatty dependency loggers
    logging.getLogger("Rx").setLevel(logging.WARNING)

    # Setup signal handlers for graceful shutdown
    setup_signal_handlers()

    logging.info("Starting async Octopus Logger...")

    # Configure connection pooling for aiohttp
    connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        token = await obtain_token(session, api_key)
        token_obtained_at = datetime.now(timezone.utc)
        
        device_id = await find_home_mini_device_id(session, account_number, token)
        logging.info("Using Home Mini deviceId: %s", device_id)

        rate_limit = await fetch_rate_limit_info(session, token)
        log_rate_limit(rate_limit)

        # Create InfluxDB client and reusable WriteApi (fixes memory leak)
        influx_client = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org)
        write_api = influx_client.write_api(write_options=SYNCHRONOUS)

        # Load any cached readings from previous failed writes
        cache = load_cache()
        if cache:
            logging.info("Found %d cached readings, attempting to write...", len(cache))
            for cached_reading in cache:
                try:
                    await write_influx(write_api, influx_org, influx_bucket, cached_reading)
                except Exception as e:
                    logging.warning("Failed to write cached reading: %s", e)
                    break
            else:
                # All cached readings written successfully
                cache = []
                save_cache(cache, max_cache_size)
                logging.info("Successfully wrote all cached readings")

        last_rate_poll = datetime.now(timezone.utc)

        while not shutdown_event.is_set():
            try:
                # Check if token needs refresh (tokens typically expire after 1 hour)
                now = datetime.now(timezone.utc)
                if (now - token_obtained_at) >= timedelta(minutes=55):
                    logging.info("Refreshing authentication token...")
                    try:
                        token = await obtain_token(session, api_key)
                        token_obtained_at = now
                        logging.info("Token refreshed successfully")
                    except Exception as e:
                        logging.error("Failed to refresh token: %s", e)

                reading = await fetch_telemetry(session, device_id, token)
                if reading:
                    try:
                        await write_influx(write_api, influx_org, influx_bucket, reading)
                        logging.info("Telemetry: %s", reading)
                        
                        # If we successfully wrote, try to flush any cached readings
                        if cache:
                            for cached_reading in cache[:]:
                                try:
                                    await write_influx(write_api, influx_org, influx_bucket, cached_reading)
                                    cache.remove(cached_reading)
                                except Exception:
                                    break
                            if not cache:
                                save_cache(cache, max_cache_size)
                                logging.info("Flushed all cached readings")
                    except Exception as e:
                        logging.warning("Failed to write to InfluxDB, caching reading: %s", e)
                        cache.append(reading)
                        save_cache(cache, max_cache_size)

                if (now - last_rate_poll) >= timedelta(minutes=10):
                    rate_limit = await fetch_rate_limit_info(session, token)
                    log_rate_limit(rate_limit)
                    last_rate_poll = now

            except aiohttp.ClientResponseError as e:
                if e.status == 401:
                    logging.warning("Authentication error (401), refreshing token...")
                    try:
                        token = await obtain_token(session, api_key)
                        token_obtained_at = datetime.now(timezone.utc)
                        logging.info("Token refreshed after 401 error")
                    except Exception as refresh_error:
                        logging.error("Failed to refresh token after 401: %s", refresh_error)
                else:
                    logging.error("HTTP error in main loop: %s", e)
            except Exception as e:
                logging.error("Main loop error: %s", e)

            # Use wait with timeout for graceful shutdown support
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Normal timeout, continue loop

        # Cleanup
        logging.info("Shutting down...")
        write_api.close()
        influx_client.close()
        
        # Save any remaining cached readings
        if cache:
            save_cache(cache, max_cache_size)
            logging.info("Saved %d readings to cache for next run", len(cache))
        
        logging.info("Octopus Logger stopped")

if __name__ == "__main__":
    asyncio.run(main())
