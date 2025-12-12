import asyncio
import aiohttp
import json
import yaml
import logging
from logging.handlers import RotatingFileHandler
from influxdb_client import InfluxDBClient, Point, WritePrecision
from pathlib import Path
from datetime import datetime, timezone, timedelta

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"
CACHE_PATH = Path(__file__).parent.parent / "cache" / "pending_readings.json"
GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"

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

def save_cache(cache):
    CACHE_PATH.parent.mkdir(exist_ok=True, parents=True)
    CACHE_PATH.write_text(json.dumps(cache))

############################################################
# ASYNC GRAPHQL CLIENT
############################################################

async def graphql_request(session, query, variables=None, token=None, debug=False):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"JWT {token}"

    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    if debug:
        logging.debug(f"GraphQL Payload: {payload}")

    async with session.post(GRAPHQL_URL, json=payload, headers=headers) as resp:
        text = await resp.text()

        if debug:
            logging.debug(f"GraphQL HTTP Status: {resp.status}")
            logging.debug(f"GraphQL Raw Response: {text}")

        if resp.status >= 400:
            print("GRAPHQL RAW ERROR RESPONSE:")
            print(text)

        resp.raise_for_status()
        data = json.loads(text)

        if "errors" in data:
            raise RuntimeError(f"GraphQL error(s): {data['errors']}")

        return data["data"]

############################################################
# TOKEN + DEVICE ID
############################################################

async def obtain_token(session, api_key, debug=False):
    query = """
    mutation ObtainToken($apiKey: String!) {
      obtainKrakenToken(input: { APIKey: $apiKey }) {
        token
      }
    }
    """
    data = await graphql_request(session, query, {"apiKey": api_key}, debug=debug)
    token = data["obtainKrakenToken"]["token"]
    if debug:
        logging.debug(f"Obtained token: {token}")
    return token

async def find_home_mini_device_id(session, account_number, token, debug=False):
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
    data = await graphql_request(session, query, {"accountNumber": account_number}, token, debug)
    account = data.get("account")
    if not account:
        raise RuntimeError("Account not found")

    for agreement in account.get("electricityAgreements", []):
        mp = agreement.get("meterPoint") or {}
        for meter in mp.get("meters", []):
            for dev in meter.get("smartDevices", []):
                if dev.get("deviceId"):
                    if debug:
                        logging.debug(f"Found Home Mini deviceId: {dev['deviceId']}")
                    return dev["deviceId"]

    raise RuntimeError("No smart meter deviceId found")

############################################################
# ASYNC TELEMETRY
############################################################

async def fetch_telemetry(session, device_id, token, debug=False):
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

    data = await graphql_request(session, query, variables, token, debug)
    pts = data.get("smartMeterTelemetry") or []
    if not pts:
        return None

    pts = sorted(pts, key=lambda x: x["readAt"])
    latest = pts[-1]
    return {"timestamp": latest["readAt"], "value": latest["demand"]}

############################################################
# ASYNC RATE LIMIT INFO
############################################################

async def fetch_rate_limit_info(session, token, debug=False):
    # Octopus GraphQL schema (as of Dec 2025):
    # rateLimitInfo: CombinedRateLimitInformation
    # - pointsAllowanceRateLimit: PointsAllowanceRateLimitInformation
    #     limit, remainingPoints, usedPoints, ttl, isBlocked
    # - fieldSpecificRateLimits: FieldSpecificRateLimitInformationConnection
    #     edges { node { field, rate, ttl, isBlocked } }
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
        # This field is a connection and requires pagination arguments.
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

    data = await graphql_request(session, query, None, token, debug)
    return data["rateLimitInfo"]

############################################################
# ASYNC INFLUX WRITE (sync wrapped)
############################################################

async def write_influx(client, org, bucket, reading):
    def _write():
        point = (
            Point("power")
            .field("value", reading["value"])
            .time(reading["timestamp"], WritePrecision.S)
        )
        client.write_api().write(bucket=bucket, org=org, record=point)

    await asyncio.to_thread(_write)

############################################################
# ASYNC MAIN LOOP
############################################################

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

    log_path = Path(__file__).parent.parent / "octopus_logger.log"
    file_handler = RotatingFileHandler(log_path, maxBytes=5*1024*1024, backupCount=3)
    syslog = logging.handlers.SysLogHandler(address="/dev/log")
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        handlers=[file_handler, syslog, logging.StreamHandler()],
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Suppress extremely chatty dependency loggers when running with debug enabled.
    # In particular, influxdb-client depends on reactivex which logs
    # `timeout: <seconds>` at DEBUG level on logger name "Rx".
    logging.getLogger("Rx").setLevel(logging.WARNING)

    logging.info("Starting async Octopus Logger...")

    async with aiohttp.ClientSession() as session:
        token = await obtain_token(session, api_key, debug)
        device_id = await find_home_mini_device_id(session, account_number, token, debug)
        logging.info(f"Using Home Mini deviceId: {device_id}")

        rate_limit = await fetch_rate_limit_info(session, token, debug)
        pa = (rate_limit or {}).get("pointsAllowanceRateLimit") or {}
        logging.info(
            "Rate Limit Summary: PA remainingPoints=%s usedPoints=%s limit=%s ttl=%s isBlocked=%s",
            pa.get("remainingPoints"),
            pa.get("usedPoints"),
            pa.get("limit"),
            pa.get("ttl"),
            pa.get("isBlocked"),
        )

        influx_client = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org)

        last_rate_poll = datetime.now(timezone.utc)

        while True:
            try:
                reading = await fetch_telemetry(session, device_id, token, debug)
                if reading:
                    await write_influx(influx_client, influx_org, influx_bucket, reading)
                    logging.info(f"Telemetry: {reading}")

                now = datetime.now(timezone.utc)
                if (now - last_rate_poll) >= timedelta(minutes=10):
                    rate_limit = await fetch_rate_limit_info(session, token, debug)
                    pa = (rate_limit or {}).get("pointsAllowanceRateLimit") or {}
                    logging.info(
                        "Rate Limit Summary: PA remainingPoints=%s usedPoints=%s limit=%s ttl=%s isBlocked=%s",
                        pa.get("remainingPoints"),
                        pa.get("usedPoints"),
                        pa.get("limit"),
                        pa.get("ttl"),
                        pa.get("isBlocked"),
                    )
                    last_rate_poll = now

            except Exception as e:
                logging.error(f"Main loop error: {e}")

            await asyncio.sleep(interval)

if __name__ == "__main__":
    asyncio.run(main())
