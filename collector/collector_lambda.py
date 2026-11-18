"""
TransferWatch - drt-collector
Downloads Durham Region Transit (DRT) GTFS-Realtime feeds and saves them
to S3 exactly as received (land raw, transform later).

- No extra packages (urllib + boto3, both built into Lambda)
- Triggered every 2 minutes by EventBridge
- Saves to raw/durham/<feed>/date=YYYY-MM-DD/hour=HH/<timestamp>.pb

Environment variable:
  BUCKET = your S3 bucket name (e.g. transferwatch-drt)
"""

import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

import boto3

s3 = boto3.client("s3")
BUCKET = os.environ["BUCKET"]
CITY = "durham"

# Official DRT feed URLs (from the Transitland Atlas record for DRT)
FEEDS = {
    "trip_updates": "https://drtonline.durhamregiontransit.com/gtfsrealtime/TripUpdates",
    "vehicle_positions": "https://drtonline.durhamregiontransit.com/gtfsrealtime/VehiclePositions",
    "alerts": "https://maps.durham.ca/OpenDataGTFS/alerts.pb",  # useful for snow-day analysis
}

HEADERS = {"User-Agent": "TransferWatch/1.0 (personal data project)"}


def download(url, retries=2):
    """Download a feed, retrying briefly on network errors."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries:
                raise
            print(f"Retry {attempt + 1} for {url}: {e}")
            time.sleep(2)


def lambda_handler(event, context):
    now = datetime.now(timezone.utc)
    date_part = now.strftime("%Y-%m-%d")
    hour_part = now.strftime("%H")
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    results = {}
    failures = 0

    for feed_name, url in FEEDS.items():
        try:
            data = download(url)
            if not data:
                raise ValueError("empty response")
            key = f"raw/{CITY}/{feed_name}/date={date_part}/hour={hour_part}/{stamp}.pb"
            s3.put_object(Bucket=BUCKET, Key=key, Body=data)
            results[feed_name] = len(data)
            print(f"Saved {len(data):,} bytes to s3://{BUCKET}/{key}")
        except Exception as e:
            failures += 1
            results[feed_name] = f"ERROR: {e}"
            print(f"FAILED {feed_name}: {e}")

    # If every feed failed, raise so the CloudWatch error alarm fires
    if failures == len(FEEDS):
        raise RuntimeError(f"All DRT feeds failed: {results}")

    return {"saved_at": stamp, "results": results}
