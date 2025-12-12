import sys
import os
import asyncio
import logging
import aiohttp

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.main import (
    load_config,
    obtain_token,
    find_home_mini_device_id,
    fetch_telemetry,
    fetch_rate_limit_info,
)


async def run_test():
    # Enable debug logging for test runs
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    
    print("Loading configuration...")
    config = load_config()

    api_key = config["octopus"]["api_key"]
    account_number = config["octopus"]["account_number"]

    print("Starting async test run...")

    # Configure connection pooling for aiohttp
    connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        # Acquire token
        print("Obtaining Kraken token...")
        token = await obtain_token(session, api_key)

        # Find Home Mini device ID
        print("Fetching Home Mini device ID...")
        device_id = await find_home_mini_device_id(session, account_number, token)

        # Fetch rate limit info (helps catch schema mismatches early)
        print("Fetching rate limit info...")
        rate_limit = await fetch_rate_limit_info(session, token)
        pa = (rate_limit or {}).get("pointsAllowanceRateLimit") or {}
        print("Rate limit points allowance:", pa)

        # Fetch a single telemetry sample
        print(f"Fetching single telemetry reading for device {device_id}...")
        reading = await fetch_telemetry(session, device_id, token)

        if reading:
            print("Success! Sample reading:")
            print(reading)
        else:
            print("No telemetry returned from GraphQL.")

    print("Async test complete.")


if __name__ == "__main__":
    try:
        asyncio.run(run_test())
    except Exception as e:
        print("Error during async test run:", e)
        sys.exit(1)
