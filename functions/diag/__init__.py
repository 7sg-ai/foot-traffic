"""
Azure Function: diag
HTTP-triggered diagnostic function. Tests all dependencies and writes results to blob.
"""
import json
import logging
import os
import sys
import traceback

import azure.functions as func

logger = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    results = {}

    # Test imports
    for mod in ["cv2", "yt_dlp", "pyodbc", "PIL", "numpy", "openai", "azure.storage.blob", "tenacity"]:
        try:
            __import__(mod)
            results[f"import_{mod}"] = "OK"
        except Exception as e:
            results[f"import_{mod}"] = f"FAIL: {e}"

    # ODBC drivers
    try:
        import pyodbc
        results["odbc_drivers"] = pyodbc.drivers()
    except Exception as e:
        results["odbc_drivers"] = f"FAIL: {e}"

    # Env vars (masked)
    for var in ["STORAGE_CONNECTION_STRING", "STORAGE_ACCOUNT_NAME", "SYNAPSE_SERVER",
                "VIDEO_FEED_URLS", "AZURE_OPENAI_ENDPOINT", "FUNCTIONS_WORKER_RUNTIME"]:
        val = os.environ.get(var, "NOT SET")
        results[f"env_{var}"] = val[:60] + "..." if len(val) > 60 else val

    # yt-dlp URL resolution
    try:
        import yt_dlp
        ydl_opts = {
            "format": "best[height<=720]/best",
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "live_from_start": False,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                "https://www.youtube.com/watch?v=rnCTiKOB6Ks", download=False
            )
            if info:
                is_live = info.get("is_live")
                url = (info.get("manifest_url") or info.get("url")) if is_live else info.get("url")
                results["yt_dlp_resolve"] = f"OK is_live={is_live} url={str(url)[:80]}"
            else:
                results["yt_dlp_resolve"] = "no info returned"
    except Exception as e:
        results["yt_dlp_resolve"] = f"FAIL: {traceback.format_exc()[:500]}"

    # OpenCV VideoCapture
    try:
        import cv2
        cap = cv2.VideoCapture()
        results["cv2_videocapture"] = f"OK version={cv2.__version__}"
        cap.release()
    except Exception as e:
        results["cv2_videocapture"] = f"FAIL: {e}"

    # Synapse connection test
    try:
        import pyodbc
        synapse_server = os.environ.get("SYNAPSE_SERVER", "")
        synapse_db = os.environ.get("SYNAPSE_DATABASE", "foottrafficdw")
        synapse_user = os.environ.get("SYNAPSE_USERNAME", "sqladmin")
        synapse_pwd = os.environ.get("SYNAPSE_PASSWORD", "")
        if synapse_server and synapse_pwd:
            conn_str = (
                f"DRIVER={{ODBC Driver 18 for SQL Server}};"
                f"SERVER={synapse_server};"
                f"DATABASE={synapse_db};"
                f"UID={synapse_user};"
                f"PWD={synapse_pwd};"
                f"Encrypt=yes;TrustServerCertificate=no;Connection Timeout=15;"
            )
            conn = pyodbc.connect(conn_str, timeout=15)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM traffic.analysis_jobs")
            count = cursor.fetchone()[0]
            cursor.execute("SELECT TOP 3 status, frames_captured, error_message, started_at FROM traffic.analysis_jobs ORDER BY started_at DESC")
            rows = [str(r) for r in cursor.fetchall()]
            conn.close()
            results["synapse_connection"] = "OK"
            results["analysis_jobs_count"] = count
            results["recent_jobs"] = rows
        else:
            results["synapse_connection"] = "SKIP: missing env vars"
    except Exception as e:
        results["synapse_connection"] = f"FAIL: {traceback.format_exc()[:500]}"

    # Blob storage test
    try:
        conn_str = os.environ.get("STORAGE_CONNECTION_STRING", "")
        if conn_str:
            from azure.storage.blob import BlobServiceClient
            client = BlobServiceClient.from_connection_string(conn_str)
            containers = [c.name for c in client.list_containers()]
            results["blob_storage"] = f"OK containers={containers}"
            # Count blobs in video-frames
            container_client = client.get_container_client("video-frames")
            blobs = list(container_client.list_blobs())
            results["video_frames_blob_count"] = len(blobs)
        else:
            results["blob_storage"] = "SKIP: no STORAGE_CONNECTION_STRING"
    except Exception as e:
        results["blob_storage"] = f"FAIL: {e}"

    output = json.dumps(results, indent=2, default=str)
    logger.info("Diag complete: %s", output)

    # Always write to blob storage so we can read it even without HTTP access
    try:
        conn_str = os.environ.get("STORAGE_CONNECTION_STRING", "")
        if conn_str:
            from azure.storage.blob import BlobServiceClient
            client = BlobServiceClient.from_connection_string(conn_str)
            blob = client.get_blob_client("video-frames", "diag-output.json")
            blob.upload_blob(output.encode(), overwrite=True)
            results["_blob_upload"] = "OK: video-frames/diag-output.json"
    except Exception as e:
        results["_blob_upload"] = f"FAIL: {e}"

    return func.HttpResponse(
        json.dumps(results, indent=2, default=str),
        status_code=200,
        mimetype="application/json",
    )
