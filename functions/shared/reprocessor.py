"""
Reprocessor: re-analyzes frames that previously returned 0 people detected.

On container restart, improvements to the VLM model or prompt may yield better
results for frames that were previously analyzed but returned zero persons.
This module finds those frames (identified by sentinel rows in raw_observations),
fetches the stored JPEG blobs, re-runs VLM analysis, and updates the database.

Design
------
* Zero-person frames are identified by sentinel rows in traffic.raw_observations
  where gender IS NULL (all demographic fields are NULL) and frame_blob_url IS NOT NULL.
* For each such frame we:
    1. Download the JPEG from Azure Blob Storage.
    2. Re-run VLM analysis.
    3. Delete the old sentinel row (and any stale interval aggregate).
    4. Insert fresh raw_observations rows.
    5. Rebuild the interval aggregate from all observations for that interval.
* Reprocessing is intentionally rate-limited to avoid hammering the VLM API on
  every restart.  A configurable cap (MAX_REPROCESS_FRAMES_PER_RESTART) limits
  how many frames are reprocessed per container lifecycle.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
from azure.storage.blob import BlobServiceClient

from .config import get_settings
from .db_client import SynapseClient
from .models import IntervalAggregate, ZeroPersonFrame
from .vlm_analyzer import VLMAnalyzer

logger = logging.getLogger(__name__)

# Maximum frames to reprocess per container restart.  Set to 0 to disable.
MAX_REPROCESS_FRAMES_PER_RESTART = int(
    __import__("os").environ.get("REPROCESS_MAX_FRAMES", "50")
)

# How far back (in days) to look for zero-person frames to reprocess.
REPROCESS_LOOKBACK_DAYS = int(
    __import__("os").environ.get("REPROCESS_LOOKBACK_DAYS", "7")
)


def run_startup_reprocessing(
    db: SynapseClient,
    analyzer: VLMAnalyzer,
) -> None:
    """
    Entry point called once per container lifecycle (on first timer trigger).

    Finds frames with 0 people detected, re-analyzes them with the current
    VLM model/prompt, and updates the database records accordingly.
    """
    if MAX_REPROCESS_FRAMES_PER_RESTART <= 0:
        logger.info("Startup reprocessing disabled (REPROCESS_MAX_FRAMES=0)")
        return

    logger.info(
        "Startup reprocessing: scanning for zero-person frames "
        "(lookback=%d days, cap=%d frames)",
        REPROCESS_LOOKBACK_DAYS,
        MAX_REPROCESS_FRAMES_PER_RESTART,
    )

    try:
        frames = db.get_zero_person_frames(
            lookback_days=REPROCESS_LOOKBACK_DAYS,
            limit=MAX_REPROCESS_FRAMES_PER_RESTART,
        )
    except Exception as e:
        logger.error("Failed to query zero-person frames for reprocessing: %s", e)
        return

    if not frames:
        logger.info("Startup reprocessing: no zero-person frames found — nothing to do")
        return

    logger.info(
        "Startup reprocessing: found %d zero-person frame(s) to reprocess", len(frames)
    )

    settings = get_settings()
    blob_service = BlobServiceClient.from_connection_string(
        settings.storage_connection_string
    )

    reprocessed = 0
    skipped = 0
    failed = 0

    for frame_info in frames:
        try:
            success = _reprocess_frame(
                frame_info=frame_info,
                db=db,
                analyzer=analyzer,
                blob_service=blob_service,
                settings=settings,
            )
            if success:
                reprocessed += 1
            else:
                skipped += 1
        except Exception as e:
            logger.error(
                "Reprocessing failed for feed_id=%d captured_at=%s: %s",
                frame_info.feed_id,
                frame_info.captured_at.isoformat(),
                e,
            )
            failed += 1

    logger.info(
        "Startup reprocessing complete: reprocessed=%d, skipped=%d, failed=%d",
        reprocessed,
        skipped,
        failed,
    )


def _reprocess_frame(
    frame_info: ZeroPersonFrame,
    db: SynapseClient,
    analyzer: VLMAnalyzer,
    blob_service: BlobServiceClient,
    settings,
) -> bool:
    """
    Re-analyze a single zero-person frame and update the database.

    Returns True if the frame was successfully reprocessed (even if still 0
    persons — the sentinel will be refreshed), False if the blob could not
    be fetched (frame is skipped).
    """
    feed_id = frame_info.feed_id
    captured_at = frame_info.captured_at
    interval_start = frame_info.interval_start
    blob_url = frame_info.frame_blob_url

    logger.info(
        "Reprocessing feed_id=%d captured_at=%s blob=%s",
        feed_id,
        captured_at.isoformat(),
        blob_url,
    )

    # 1. Download the stored JPEG from blob storage
    image_bytes = _download_blob(blob_url, blob_service, settings)
    if image_bytes is None:
        logger.warning(
            "Could not download blob for feed_id=%d captured_at=%s — skipping",
            feed_id,
            captured_at.isoformat(),
        )
        return False

    # 2. Re-run VLM analysis
    start_time = time.time()
    try:
        frame_result = analyzer.analyze_frame(
            image_bytes=image_bytes,
            feed_id=feed_id,
            feed_url="",  # not needed for reprocessing; blob already stored
            captured_at=captured_at,
            interval_start=interval_start,
            frame_blob_url=blob_url,
            max_persons=settings.max_persons_per_frame,
        )
    except Exception as e:
        logger.error(
            "VLM re-analysis failed for feed_id=%d captured_at=%s: %s",
            feed_id,
            captured_at.isoformat(),
            e,
        )
        return False

    duration_ms = int((time.time() - start_time) * 1000)
    persons_found = len(frame_result.persons)

    logger.info(
        "Re-analysis result: feed_id=%d captured_at=%s persons=%d (was 0)",
        feed_id,
        captured_at.isoformat(),
        persons_found,
    )

    # 3. Delete the old sentinel row for this specific frame
    db.delete_zero_person_sentinel(
        feed_id=feed_id,
        captured_at=captured_at,
    )

    # 4. Insert fresh observations (or a new sentinel if still 0)
    if not frame_result.error:
        db.insert_raw_observations(frame_result)

    # 5. Rebuild the interval aggregate for this interval
    _rebuild_interval_aggregate(
        db=db,
        feed_id=feed_id,
        interval_start=interval_start,
    )

    return True


def _download_blob(
    blob_url: str,
    blob_service: BlobServiceClient,
    settings,
) -> Optional[bytes]:
    """
    Download a blob by its full HTTPS URL.

    Tries the Azure SDK first (authenticated), then falls back to a plain
    HTTP GET (works for public blobs).
    """
    if not blob_url:
        return None

    # Parse container and blob name from the URL
    # URL format: https://<account>.blob.core.windows.net/<container>/<blob_name>
    try:
        # Strip the scheme + host to get /<container>/<blob_path>
        account_url = f"https://{settings.storage_account_name}.blob.core.windows.net"
        if blob_url.startswith(account_url):
            path = blob_url[len(account_url):].lstrip("/")
            container_name, _, blob_name = path.partition("/")
            container_client = blob_service.get_container_client(container_name)
            blob_client = container_client.get_blob_client(blob_name)
            data = blob_client.download_blob().readall()
            return data
    except Exception as e:
        logger.warning("SDK blob download failed (%s), trying HTTP GET: %s", blob_url, e)

    # Fallback: plain HTTP GET (works for public containers)
    try:
        resp = requests.get(blob_url, timeout=30)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.error("HTTP blob download also failed for %s: %s", blob_url, e)
        return None


def _rebuild_interval_aggregate(
    db: SynapseClient,
    feed_id: int,
    interval_start: datetime,
) -> None:
    """
    Rebuild the interval_aggregates row for a given feed + interval by
    re-reading all raw_observations for that interval from the database.
    """
    interval_end = interval_start + timedelta(minutes=5)

    try:
        frame_results = db.get_frame_results_for_interval(
            feed_id=feed_id,
            interval_start=interval_start,
        )

        aggregate = IntervalAggregate.from_frame_results(
            feed_id=feed_id,
            interval_start=interval_start,
            interval_end=interval_end,
            frame_results=frame_results,
        )

        db.insert_interval_aggregate(aggregate)

        logger.info(
            "Rebuilt interval aggregate: feed_id=%d interval=%s total_count=%d",
            feed_id,
            interval_start.isoformat(),
            aggregate.total_count,
        )
    except Exception as e:
        logger.error(
            "Failed to rebuild interval aggregate for feed_id=%d interval=%s: %s",
            feed_id,
            interval_start.isoformat(),
            e,
        )
