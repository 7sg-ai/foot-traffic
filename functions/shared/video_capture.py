"""
Video frame capture utility.
Extracts frames from public video streams.

Supported source types
----------------------
* TfL JamCam  – short MP4 clips (~5-10 s) hosted on S3 and refreshed every
                 ~90 seconds.  We download the clip in-memory and extract
                 frames evenly spread across its duration.
* RTSP / HLS  – direct stream URLs opened with OpenCV.
"""
from __future__ import annotations

import io
import logging
import time
import tempfile
import os
from datetime import datetime
from typing import Optional

import cv2
import numpy as np
import requests
from azure.storage.blob import BlobServiceClient, ContentSettings
from PIL import Image

from .config import get_settings

logger = logging.getLogger(__name__)

# Supported URL patterns
RTSP_PATTERNS = ("rtsp://", "rtsps://")
HLS_PATTERNS = (".m3u8",)
TFL_JAMCAM_PATTERNS = ("jamcams.tfl.gov.uk",)

# TfL JamCam base URLs
TFL_S3_BASE = "https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/"
TFL_API_BASE = "https://api.tfl.gov.uk/Place/Type/JamCam"


class VideoCapture:
    """Captures frames from public video streams and stores them in Azure Blob Storage."""

    def __init__(self):
        self._settings = get_settings()
        self._blob_client = BlobServiceClient.from_connection_string(
            self._settings.storage_connection_string
        )
        self._container = self._settings.frames_container

    # ------------------------------------------------------------------
    # Source-type detection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_tfl_jamcam(url: str) -> bool:
        return any(p in url for p in TFL_JAMCAM_PATTERNS)

    @staticmethod
    def _is_tfl_image(url: str) -> bool:
        """True when the TfL URL points to a JPEG still rather than an MP4 clip."""
        return VideoCapture._is_tfl_jamcam(url) and url.endswith(".jpg")

    # ------------------------------------------------------------------
    # TfL JamCam helpers
    # ------------------------------------------------------------------

    def _fetch_tfl_mp4_frames(
        self,
        mp4_url: str,
        num_frames: int,
    ) -> list[np.ndarray]:
        """
        Download a TfL JamCam MP4 clip and extract *num_frames* frames spread
        evenly across the clip's duration.

        Returns a list of BGR numpy arrays (may be shorter than num_frames if
        the clip is very short).
        """
        logger.info("Downloading TfL JamCam clip: %s", mp4_url)
        self._write_status_blob(f"tfl: downloading {mp4_url}")

        try:
            resp = requests.get(mp4_url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            self._write_status_blob(f"tfl: download FAILED {mp4_url}: {e}")
            raise

        mp4_bytes = resp.content
        logger.info("Downloaded %d bytes from TfL JamCam", len(mp4_bytes))
        self._write_status_blob(f"tfl: downloaded {len(mp4_bytes)} bytes")

        # Write to a temp file so OpenCV can open it
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(mp4_bytes)
            tmp_path = tmp.name

        frames: list[np.ndarray] = []
        try:
            cap = cv2.VideoCapture(tmp_path)
            if not cap.isOpened():
                raise ValueError(f"OpenCV could not open downloaded MP4: {mp4_url}")

            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            logger.info(
                "TfL clip: total_frames=%d fps=%.1f", total_frames, fps
            )

            if total_frames <= 0:
                # Fallback: read sequentially and sample
                all_frames: list[np.ndarray] = []
                while True:
                    ret, frame = cap.read()
                    if not ret:
                        break
                    all_frames.append(frame)
                if all_frames:
                    indices = _evenly_spaced_indices(len(all_frames), num_frames)
                    frames = [all_frames[i] for i in indices]
            else:
                indices = _evenly_spaced_indices(total_frames, num_frames)
                for idx in indices:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        frames.append(frame)

            cap.release()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        logger.info("Extracted %d frames from TfL clip", len(frames))
        self._write_status_blob(f"tfl: extracted {len(frames)} frames from clip")
        return frames

    def _fetch_tfl_image_frame(self, image_url: str) -> Optional[np.ndarray]:
        """Download a TfL JamCam JPEG still and return it as a BGR numpy array."""
        try:
            resp = requests.get(image_url, timeout=15)
            resp.raise_for_status()
            img_array = np.frombuffer(resp.content, dtype=np.uint8)
            frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            return frame
        except Exception as e:
            logger.warning("Failed to fetch TfL image %s: %s", image_url, e)
            return None

    # ------------------------------------------------------------------
    # Status blob
    # ------------------------------------------------------------------

    def _write_status_blob(self, msg: str) -> None:
        """Write a status message to blob storage for debugging."""
        try:
            import datetime as _dt
            ts = _dt.datetime.utcnow().isoformat()
            existing = ""
            try:
                blob = self._blob_client.get_blob_client(self._container, "capture-status.log")
                existing = blob.download_blob().readall().decode()
            except Exception:
                pass
            content = existing + f"\n[{ts}] {msg}"
            blob = self._blob_client.get_blob_client(self._container, "capture-status.log")
            blob.upload_blob(content.encode(), overwrite=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def capture_frames(
        self,
        feed_url: str,
        feed_id: int,
        interval_start: datetime,
        num_frames: int = 5,
        frame_interval_seconds: float = 10.0,
    ) -> list[tuple[bytes, str]]:
        """
        Capture multiple frames from a video feed.

        Reference frames mode
        ---------------------
        When REFERENCE_FRAMES_MODE=true, live video capture is skipped entirely.
        Instead, frames are loaded from the ``profile-reference/`` folder in the
        video-frames blob container (uploaded during deployment from profile_data/).
        This is useful for profiling / testing the VLM pipeline without consuming
        live camera bandwidth or requiring network access to the camera feeds.

        TfL JamCam strategy
        -------------------
        The feed_url points to a short MP4 clip (~5-10 s) on S3 that TfL
        refreshes every ~90 seconds.  We download the clip once and extract
        *num_frames* frames spread evenly across it.  No sleeping between
        frames is needed.

        RTSP / HLS strategy
        -------------------
        Open the stream with OpenCV, flush buffered frames, then sleep
        *frame_interval_seconds* between captures.

        Args:
            feed_url: Source video URL
            feed_id: Database feed ID (for blob naming)
            interval_start: 5-minute interval start time
            num_frames: Number of frames to capture
            frame_interval_seconds: Seconds to wait between frames (RTSP/HLS only)

        Returns:
            List of (jpeg_bytes, blob_url) tuples
        """
        frames_out: list[tuple[bytes, str]] = []

        # ---- Reference frames mode ----------------------------------
        if self._settings.reference_frames_mode:
            return self._capture_reference_frames(
                feed_id=feed_id,
                interval_start=interval_start,
                num_frames=num_frames,
            )

        # ---- TfL JamCam path ----------------------------------------
        if self._is_tfl_jamcam(feed_url):
            return self._capture_tfl_frames(
                feed_url=feed_url,
                feed_id=feed_id,
                interval_start=interval_start,
                num_frames=num_frames,
            )

        # ---- RTSP / HLS stream path ---------------------------------
        cap = None
        try:
            cap = cv2.VideoCapture(feed_url)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            if not cap.isOpened():
                logger.error("Could not open video stream: %s", feed_url)
                return frames_out

            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            logger.info("Stream opened: fps=%.1f, feed_id=%d", fps, feed_id)

            for i in range(num_frames):
                if i > 0:
                    time.sleep(frame_interval_seconds)

                for _ in range(3):
                    cap.grab()

                ret, frame = cap.retrieve()
                if not ret or frame is None:
                    logger.warning(
                        "Failed to read frame %d/%d from %s", i + 1, num_frames, feed_url
                    )
                    continue

                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frame_rgb = self._resize_frame(frame_rgb, max_width=1280, max_height=720)
                jpeg_bytes = self._encode_jpeg(frame_rgb, quality=85)
                blob_url = self._upload_frame(
                    jpeg_bytes=jpeg_bytes,
                    feed_id=feed_id,
                    interval_start=interval_start,
                    frame_index=i,
                )
                frames_out.append((jpeg_bytes, blob_url))
                logger.debug("Captured frame %d/%d for feed_id=%d", i + 1, num_frames, feed_id)

        except Exception as e:
            logger.error("Frame capture error for feed_id=%d: %s", feed_id, e)
        finally:
            if cap is not None:
                cap.release()

        logger.info(
            "Captured %d/%d frames for feed_id=%d interval=%s",
            len(frames_out),
            num_frames,
            feed_id,
            interval_start.isoformat(),
        )
        return frames_out

    def _capture_reference_frames(
        self,
        feed_id: int,
        interval_start: datetime,
        num_frames: int,
    ) -> list[tuple[bytes, str]]:
        """
        Load pre-uploaded reference frames from blob storage instead of capturing
        live video.  Frames are read from the ``profile-reference/`` prefix in the
        video-frames container (populated during deployment from profile_data/).

        Up to *num_frames* blobs are selected at random from the reference pool so
        that each scheduler run sees a varied sample.  The selected blobs are then
        re-uploaded under the normal live-capture path so the rest of the pipeline
        (VLM analysis, DB writes) is completely unchanged.
        """
        import random

        prefix = self._settings.reference_frames_prefix
        logger.info(
            "Reference frames mode: loading up to %d frames from blob prefix '%s' for feed_id=%d",
            num_frames, prefix, feed_id,
        )

        try:
            container_client = self._blob_client.get_container_client(self._container)
            all_blobs = [
                b.name
                for b in container_client.list_blobs(name_starts_with=prefix)
                if b.name.lower().endswith((".jpg", ".jpeg", ".png"))
            ]
        except Exception as e:
            logger.error("Failed to list reference blobs under '%s': %s", prefix, e)
            return []

        if not all_blobs:
            logger.warning("No reference frames found under prefix '%s'", prefix)
            return []

        # Pick a random sample (with replacement if pool is smaller than num_frames)
        selected = random.choices(all_blobs, k=num_frames) if len(all_blobs) < num_frames \
            else random.sample(all_blobs, num_frames)

        frames_out: list[tuple[bytes, str]] = []
        for i, blob_name in enumerate(selected):
            try:
                blob_client = container_client.get_blob_client(blob_name)
                jpeg_bytes = blob_client.download_blob().readall()

                # Re-upload under the standard live-capture path so downstream
                # code (VLM analyzer, DB writes) sees a normal blob URL.
                blob_url = self._upload_frame(
                    jpeg_bytes=jpeg_bytes,
                    feed_id=feed_id,
                    interval_start=interval_start,
                    frame_index=i,
                )
                frames_out.append((jpeg_bytes, blob_url))
                logger.debug(
                    "Reference frame %d/%d: %s → %s", i + 1, num_frames, blob_name, blob_url
                )
            except Exception as e:
                logger.warning("Failed to load reference blob '%s': %s", blob_name, e)

        logger.info(
            "Loaded %d/%d reference frames for feed_id=%d interval=%s",
            len(frames_out), num_frames, feed_id, interval_start.isoformat(),
        )
        return frames_out

    def _capture_tfl_frames(
        self,
        feed_url: str,
        feed_id: int,
        interval_start: datetime,
        num_frames: int,
    ) -> list[tuple[bytes, str]]:
        """
        Capture frames from a TfL JamCam feed.

        If the URL ends with .mp4 we download the clip and extract frames.
        If the URL ends with .jpg we fetch the still image and replicate it
        (or derive the .mp4 URL and fall back to the image on failure).
        """
        frames_out: list[tuple[bytes, str]] = []

        # Derive companion URLs
        if feed_url.endswith(".mp4"):
            mp4_url = feed_url
            jpg_url = feed_url[:-4] + ".jpg"
        elif feed_url.endswith(".jpg"):
            jpg_url = feed_url
            mp4_url = feed_url[:-4] + ".mp4"
        else:
            mp4_url = feed_url
            jpg_url = None

        bgr_frames: list[np.ndarray] = []

        # Try MP4 first
        try:
            bgr_frames = self._fetch_tfl_mp4_frames(mp4_url, num_frames)
        except Exception as e:
            logger.warning(
                "TfL MP4 fetch failed for feed_id=%d (%s): %s — trying JPEG fallback",
                feed_id, mp4_url, e,
            )

        # Fall back to JPEG still if MP4 failed or yielded no frames
        if not bgr_frames and jpg_url:
            logger.info("Falling back to TfL JPEG still: %s", jpg_url)
            frame = self._fetch_tfl_image_frame(jpg_url)
            if frame is not None:
                # Replicate the single still to fill num_frames slots
                bgr_frames = [frame] * num_frames

        if not bgr_frames:
            logger.error(
                "No frames obtained from TfL feed_id=%d url=%s", feed_id, feed_url
            )
            return frames_out

        for i, bgr_frame in enumerate(bgr_frames[:num_frames]):
            frame_rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
            frame_rgb = self._resize_frame(frame_rgb, max_width=1280, max_height=720)
            jpeg_bytes = self._encode_jpeg(frame_rgb, quality=85)
            blob_url = self._upload_frame(
                jpeg_bytes=jpeg_bytes,
                feed_id=feed_id,
                interval_start=interval_start,
                frame_index=i,
            )
            frames_out.append((jpeg_bytes, blob_url))

        logger.info(
            "Captured %d/%d TfL frames for feed_id=%d interval=%s",
            len(frames_out),
            num_frames,
            feed_id,
            interval_start.isoformat(),
        )
        return frames_out

    # ------------------------------------------------------------------
    # Frame helpers
    # ------------------------------------------------------------------

    def _resize_frame(
        self,
        frame: np.ndarray,
        max_width: int = 1280,
        max_height: int = 720,
    ) -> np.ndarray:
        """Resize frame while maintaining aspect ratio."""
        h, w = frame.shape[:2]
        if w <= max_width and h <= max_height:
            return frame
        scale = min(max_width / w, max_height / h)
        new_w = int(w * scale)
        new_h = int(h * scale)
        return cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)

    def _encode_jpeg(self, frame_rgb: np.ndarray, quality: int = 85) -> bytes:
        """Encode numpy array as JPEG bytes."""
        img = Image.fromarray(frame_rgb)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality, optimize=True)
        return buffer.getvalue()

    def _upload_frame(
        self,
        jpeg_bytes: bytes,
        feed_id: int,
        interval_start: datetime,
        frame_index: int,
    ) -> str:
        """Upload a frame to Azure Blob Storage and return the blob URL."""
        date_path = interval_start.strftime("%Y/%m/%d/%H")
        timestamp_str = interval_start.strftime("%Y%m%d_%H%M")
        blob_name = f"feed_{feed_id}/{date_path}/{timestamp_str}_frame{frame_index:02d}.jpg"

        try:
            container_client = self._blob_client.get_container_client(self._container)
            blob_client = container_client.get_blob_client(blob_name)
            blob_client.upload_blob(
                jpeg_bytes,
                overwrite=True,
                content_settings=ContentSettings(content_type="image/jpeg"),
            )
            account_name = self._settings.storage_account_name
            blob_url = (
                f"https://{account_name}.blob.core.windows.net"
                f"/{self._container}/{blob_name}"
            )
            return blob_url
        except Exception as e:
            logger.warning("Failed to upload frame to blob storage: %s", e)
            return ""

    def capture_single_frame(
        self,
        feed_url: str,
        feed_id: int,
        interval_start: datetime,
    ) -> Optional[tuple[bytes, str]]:
        """Capture a single frame from a video stream."""
        frames = self.capture_frames(
            feed_url=feed_url,
            feed_id=feed_id,
            interval_start=interval_start,
            num_frames=1,
        )
        return frames[0] if frames else None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _evenly_spaced_indices(total: int, n: int) -> list[int]:
    """Return *n* indices spread evenly across [0, total)."""
    if n <= 0 or total <= 0:
        return []
    if n >= total:
        return list(range(total))
    step = total / n
    return [int(step * i + step / 2) for i in range(n)]


# Singleton
_video_capture: Optional[VideoCapture] = None


def get_video_capture() -> VideoCapture:
    global _video_capture
    if _video_capture is None:
        _video_capture = VideoCapture()
    return _video_capture
