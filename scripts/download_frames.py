"""
Script to:
1. Query traffic.analysis_jobs for 100 random rows with persons_detected > 1
2. Download one representative frame per job from Azure Blob Storage into profile_data/
3. Create a JSONL file (frames_manifest.jsonl) with filename and persons_detected count

Blob naming convention in video-frames container:
  feed_{feed_id}/{YYYY}/{MM}/{DD}/{HH}/{YYYYMMDD}_{HHMM}_frame{NN}.jpg
"""
import json
import os
import sys
import pyodbc
from azure.storage.blob import BlobServiceClient

# ── Credentials (read from environment variables) ─────────────────────────────
# Set these before running:
#   export SYNAPSE_SERVER=<workspace>.sql.azuresynapse.net
#   export SYNAPSE_DATABASE=foottrafficdw
#   export SYNAPSE_USERNAME=sqladmin
#   export SYNAPSE_PASSWORD=<password>
#   export STORAGE_ACCOUNT=<storage-account-name>
#   export STORAGE_KEY=<storage-account-key>
SYNAPSE_SERVER   = os.environ.get("SYNAPSE_SERVER",   "syn-2kmmhwfzp6qsq.sql.azuresynapse.net")
SYNAPSE_DATABASE = os.environ.get("SYNAPSE_DATABASE", "foottrafficdw")
SYNAPSE_USERNAME = os.environ.get("SYNAPSE_USERNAME", "sqladmin")
SYNAPSE_PASSWORD = os.environ["SYNAPSE_PASSWORD"]

STORAGE_ACCOUNT  = os.environ.get("STORAGE_ACCOUNT", "stg2kmmhwfzp6qsq")
STORAGE_KEY      = os.environ["STORAGE_KEY"]
FRAMES_CONTAINER = "video-frames"

OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "..", "profile_data")
JSONL_PATH  = os.path.join(OUTPUT_DIR, "frames_manifest.jsonl")

# ── Step 1: Query Synapse ─────────────────────────────────────────────────────
conn_str = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    f"SERVER={SYNAPSE_SERVER};"
    f"DATABASE={SYNAPSE_DATABASE};"
    f"UID={SYNAPSE_USERNAME};"
    f"PWD={SYNAPSE_PASSWORD};"
    "Encrypt=yes;"
    "TrustServerCertificate=no;"
    "Connection Timeout=30;"
)

print("Connecting to Synapse...")
conn = pyodbc.connect(conn_str, autocommit=True)
cursor = conn.cursor()

sql = """
SELECT TOP 100
    job_id,
    feed_id,
    interval_start,
    persons_detected
FROM traffic.analysis_jobs
WHERE persons_detected > 1
  AND status = 'success'
ORDER BY NEWID()
"""

print("Querying traffic.analysis_jobs for 100 random rows with persons_detected > 1 ...")
cursor.execute(sql)
rows = cursor.fetchall()
cols = [c[0] for c in cursor.description]
jobs = [dict(zip(cols, r)) for r in rows]
print(f"  → Found {len(jobs)} matching rows")

cursor.close()
conn.close()

if not jobs:
    print("No matching rows found. Exiting.")
    sys.exit(0)

# ── Step 2: Download frames from Blob Storage ─────────────────────────────────
# Blob path pattern: feed_{feed_id}/{YYYY}/{MM}/{DD}/{HH}/{YYYYMMDD}_{HHMM}_frame{NN}.jpg
# We list all blobs under the interval's prefix and download the first frame found.

print("\nConnecting to Azure Blob Storage...")
blob_service = BlobServiceClient(
    account_url=f"https://{STORAGE_ACCOUNT}.blob.core.windows.net",
    credential=STORAGE_KEY,
)
container_client = blob_service.get_container_client(FRAMES_CONTAINER)

os.makedirs(OUTPUT_DIR, exist_ok=True)

manifest = []
downloaded = 0
skipped = 0

for job in jobs:
    feed_id          = job["feed_id"]
    interval_start   = job["interval_start"]   # datetime object
    persons_detected = job["persons_detected"]
    job_id           = job["job_id"]

    # Build the blob prefix from interval_start
    # e.g. feed_1/2026/04/05/21/
    year  = interval_start.strftime("%Y")
    month = interval_start.strftime("%m")
    day   = interval_start.strftime("%d")
    hour  = interval_start.strftime("%H")
    # Interval timestamp used in filename: YYYYMMDD_HHMM
    ts    = interval_start.strftime("%Y%m%d_%H%M")

    prefix = f"feed_{feed_id}/{year}/{month}/{day}/{hour}/{ts}_"

    # List blobs under this prefix (should be frame00..frame04)
    try:
        blobs = list(container_client.list_blobs(name_starts_with=prefix))
    except Exception as e:
        print(f"  [ERROR] Listing blobs for prefix {prefix}: {e}")
        skipped += 1
        continue

    if not blobs:
        print(f"  [SKIP] No blobs found at prefix: {prefix}")
        skipped += 1
        continue

    # Download all frames for this job interval
    for blob in blobs:
        blob_name = blob.name
        # Local filename: flatten path separators
        safe_name = blob_name.replace("/", "_")
        local_path = os.path.join(OUTPUT_DIR, safe_name)

        # Skip if already downloaded
        if os.path.exists(local_path):
            print(f"  [EXISTS] {safe_name}")
            manifest.append({
                "filename": safe_name,
                "persons_detected": persons_detected,
            })
            downloaded += 1
            continue

        try:
            blob_client = container_client.get_blob_client(blob_name)
            with open(local_path, "wb") as f:
                blob_client.download_blob().readinto(f)
            print(f"  [OK] {safe_name}  (persons_detected={persons_detected})")
            manifest.append({
                "filename": safe_name,
                "persons_detected": persons_detected,
            })
            downloaded += 1
        except Exception as e:
            print(f"  [ERROR] {blob_name}: {e}")
            skipped += 1

# ── Step 3: Write JSONL manifest ──────────────────────────────────────────────
print(f"\nWriting manifest to {JSONL_PATH} ...")
with open(JSONL_PATH, "w") as f:
    for entry in manifest:
        f.write(json.dumps(entry) + "\n")

print(f"\nDone!")
print(f"  Frames downloaded : {downloaded}")
print(f"  Skipped/errors    : {skipped}")
print(f"  Manifest entries  : {len(manifest)}")
print(f"  Manifest path     : {os.path.abspath(JSONL_PATH)}")
