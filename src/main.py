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
from typing import Any, Dict, List, Optional

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"
CACHE_PATH = Path(__file__).parent.parent / "cache" / "pending_readings.json"
GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"

# Graceful shutdown event
shutdown_event = asyncio.Event()


class AuthError(RuntimeError):
    """Raised when Octopus GraphQL indicates an authentication/authorization error."""


############################################################
# CONFIG + CACHE
############################################################

def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def load_cache() -> List[Dict[str, Any]]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            return []
    return []


def save_cache(cache: List[Dict[str, Any]], max_size: int = 10000) -> None:
    """Save readings to cache, truncating to max_size if needed."""
    CACHE_PATH.parent.mkdir(exist_ok=True, parents=True)
    if len(cache) > max_size:
        cache = cache[-max_size:]
    CACHE_PATH.write_text(json.dumps(cache))


############################################################
# ASYNC GRAPHQL CLIENT
############################################################

def _graphql_operation_name(query: str) -> str:
    # Best-effort extraction of operation name for logging.
    # Examples:
    #   "mutation ObtainToken($apiKey: String!)" -> ObtainToken
    #   "query Telemetry($deviceId: String!)" -> Telemetry
    q = " ".join(query.strip().split())
    parts = q.split(" ")
    if len(parts) >= 2 and parts[0] in {"query", "mutation", "subscription"}:
        return parts[1].split("(")[0]
    return "<unknown>"


def _is_auth_error(errors: Any) -> bool:
    # Octopus can return auth failures either as HTTP 401 or as GraphQL errors.
    # We detect common patterns.
    if not isinstance(errors, list):
        return False

    for err in errors:
        if not isinstance(err, dict):
            continue
        msg = (err.get("message") or "").lower()
        code = ((err.get("extensions") or {}).get("code") or "").lower()

        if any(k in msg for k in ["jwt", "token", "unauth", "authoris", "forbidden", "not authenticated"]):
            return True
        if code in {"unauthenticated", "forbidden"}:
            return True

    return False


async def graphql_request(
    session: aiohttp.ClientSession,
    query: str,
    variables: Optional[Dict[str, Any]] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"JWT {token}"

    payload: Dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    op = _graphql_operation_name(query)
    logging.debug(
        "GraphQL request op=%s vars=%s",
        op,
        list((variables or {}).keys()),
    )

    async with session.post(GRAPHQL_URL, json=payload, headers=headers) as resp:
        logging.debug("GraphQL HTTP Status op=%s status=%s", op, resp.status)

        if resp.status >= 400:
            # Only decode the body for errors to avoid extra work.
            text = await resp.text()
            logging.error("GraphQL HTTP error op=%s status=%s body=%s", op, resp.status, text[:2000])
            resp.raise_for_status()

        data = await resp.json()

        if "errors" in data:
            # Avoid logging full errors by default (may contain sensitive info).
            if _is_auth_error(data["errors"]):
                raise AuthError(f"GraphQL authentication error (op={op})")
            raise RuntimeError(f"GraphQL error(s) (op={op}): {data['errors']}")

        # Light debug summary (no full payload dumps)
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            top_keys = list((data or {}).keys())
            logging.debug("GraphQL response op=%s top_keys=%s", op, top_keys)

        return data["data"]


############################################################
# TOKEN + DEVICE ID
############################################################

async def obtain_token(session: aiohttp.ClientSession, api_key: str) -> str:
    query = """
    mutation ObtainToken($apiKey: String!) {
      obtainKrakenToken(input: { APIKey: $apiKey }) {
        token
      }
    }
    """
    data = await graphql_request(session, query, {"apiKey": api_key})
    token = data["obtainKrakenToken"]["token"]
    logging.debug("Obtained token (len=%s)", len(token) if token else 0)
    return token


async def find_home_mini_device_id(session: aiohttp.ClientSession, account_number: str, token: str) -> str:
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
                device_id = dev.get("deviceId")
                if device_id:
                    logging.debug("Found Home Mini deviceId")
                    return device_id

    raise RuntimeError("No smart meter deviceId found")


############################################################
# ASYNC TELEMETRY
############################################################

async def fetch_telemetry(session: aiohttp.ClientSession, device_id: str, token: str) -> Optional[Dict[str, Any]]:
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

async def fetch_rate_limit_info(session: aiohttp.ClientSession, token: str) -> Dict[str, Any]:
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


def log_rate_limit(rate_limit: Dict[str, Any]) -> None:
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

def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Safely convert a value to float, returning default if conversion fails."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError) as e:
        logging.warning("Failed to convert value to float: %r - %s", value, e)
        return default


def _reading_to_point(reading: Dict[str, Any]) -> Optional[Point]:
    """Convert a reading dict to an InfluxDB Point with numeric power field.
    
    Returns None if the value cannot be converted to a valid float.
    """
    power_value = _safe_float(reading.get("value"))
    if power_value is None:
        logging.warning("Skipping reading with invalid power value: %r", reading.get("value"))
        return None
    
    return (
        Point("power")
        .field("power_numeric", power_value)
        .time(reading["timestamp"], WritePrecision.S)
    )


def write_influx_sync(write_api, org: str, bucket: str, readings: List[Dict[str, Any]]) -> None:
    """Synchronous InfluxDB write for one or many readings."""
    # Filter out None points (readings with invalid values)
    points = [p for p in (_reading_to_point(r) for r in readings) if p is not None]
    if points:
        write_api.write(bucket=bucket, org=org, record=points)


async def write_influx(write_api, org: str, bucket: str, readings: List[Dict[str, Any]]) -> None:
    """Async wrapper for InfluxDB writes (single thread hop per batch)."""
    await asyncio.to_thread(write_influx_sync, write_api, org, bucket, readings)


async def flush_cache(
    write_api,
    org: str,
    bucket: str,
    cache: List[Dict[str, Any]],
    max_cache_size: int,
    *,
    batch_size: int = 500,
    max_batches: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Attempt to flush cached readings to InfluxDB.

    - Writes in batches to reduce overhead.
    - Avoids O(n^2) list.remove patterns by slicing.
    """
    if not cache:
        return cache

    written = 0
    batches = 0

    while cache:
        if max_batches is not None and batches >= max_batches:
            break

        batch = cache[:batch_size]
        try:
            await write_influx(write_api, org, bucket, batch)
        except Exception as e:
            logging.warning("Failed to flush cache (wrote %d so far): %s", written, e)
            break

        written += len(batch)
        cache = cache[len(batch) :]
        batches += 1

    if written > 0:
        save_cache(cache, max_cache_size)
        logging.info("Flushed %d cached readings (%d remaining)", written, len(cache))

    return cache


############################################################
# ASYNC MAIN LOOP
############################################################

def setup_signal_handlers() -> None:
    """Setup graceful shutdown handlers for SIGTERM and SIGINT."""

    def handle_shutdown(signum, frame):
        signame = signal.Signals(signum).name
        logging.info("Received %s, initiating graceful shutdown...", signame)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)


async def main() -> None:
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
    file_handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3)
    syslog = logging.handlers.SysLogHandler(address="/dev/log")
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        handlers=[file_handler, syslog, logging.StreamHandler()],
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Suppress chatty dependency loggers
    logging.getLogger("Rx").setLevel(logging.WARNING)

    setup_signal_handlers()

    logging.info("Starting async Octopus Logger...")

    # Configure connection pooling for aiohttp
    connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)

    async with aiohttp.ClientSession(connector=connector) as session:
        async def refresh_token(reason: str) -> str:
            nonlocal token_obtained_at
            logging.info("Refreshing authentication token (%s)...", reason)
            t = await obtain_token(session, api_key)
            token_obtained_at = datetime.now(timezone.utc)
            logging.info("Token refreshed")
            return t

        token = await obtain_token(session, api_key)
        token_obtained_at = datetime.now(timezone.utc)

        device_id = await find_home_mini_device_id(session, account_number, token)
        logging.info("Using Home Mini deviceId: %s", device_id)

        rate_limit = await fetch_rate_limit_info(session, token)
        log_rate_limit(rate_limit)

        # Create InfluxDB client and reusable WriteApi
        influx_client = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org)
        write_api = influx_client.write_api(write_options=SYNCHRONOUS)

        cache: List[Dict[str, Any]] = load_cache()
        if cache:
            logging.info("Found %d cached readings, attempting to flush...", len(cache))
            cache = await flush_cache(write_api, influx_org, influx_bucket, cache, max_cache_size)

        last_rate_poll = datetime.now(timezone.utc)

        while not shutdown_event.is_set():
            now = datetime.now(timezone.utc)
            try:
                # Proactive refresh (tokens commonly expire around ~1 hour)
                if (now - token_obtained_at) >= timedelta(minutes=55):
                    token = await refresh_token("scheduled")

                reading = await fetch_telemetry(session, device_id, token)
                if reading:
                    try:
                        await write_influx(write_api, influx_org, influx_bucket, [reading])
                        logging.info("Telemetry: %s", reading)

                        # After a successful live write, flush at most one cache batch
                        if cache:
                            cache = await flush_cache(
                                write_api,
                                influx_org,
                                influx_bucket,
                                cache,
                                max_cache_size,
                                max_batches=1,
                            )

                    except Exception as e:
                        logging.warning("Failed to write to InfluxDB, caching reading: %s", e)
                        cache.append(reading)
                        save_cache(cache, max_cache_size)

                if (now - last_rate_poll) >= timedelta(minutes=10):
                    rate_limit = await fetch_rate_limit_info(session, token)
                    log_rate_limit(rate_limit)
                    last_rate_poll = now

            except AuthError:
                # GraphQL auth error embedded in JSON errors.
                try:
                    token = await refresh_token("graphql-auth-error")
                except Exception as refresh_error:
                    logging.error("Failed to refresh token after auth error: %s", refresh_error)

            except aiohttp.ClientResponseError as e:
                if e.status == 401:
                    try:
                        token = await refresh_token("http-401")
                    except Exception as refresh_error:
                        logging.error("Failed to refresh token after 401: %s", refresh_error)
                else:
                    logging.error("HTTP error in main loop: %s", e)

            except Exception as e:
                logging.error("Main loop error: %s", e)

            # Wait with timeout for graceful shutdown support
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass

        logging.info("Shutting down...")
        try:
            write_api.close()
        finally:
            influx_client.close()

        if cache:
            save_cache(cache, max_cache_size)
            logging.info("Saved %d readings to cache for next run", len(cache))

        logging.info("Octopus Logger stopped")


if __name__ == "__main__":
    asyncio.run(main())
