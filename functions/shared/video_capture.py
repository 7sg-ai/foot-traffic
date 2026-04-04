"""
Video frame capture utility.
Extracts frames from public video streams (YouTube live, RTSP, HLS, etc.)
using yt-dlp + OpenCV.
"""
from __future__ import annotations

import io
import logging
import time
from datetime import datetime
from typing import Optional

import cv2
import numpy as np
import yt_dlp
from azure.storage.blob import BlobServiceClient, ContentSettings
from PIL import Image

from .config import get_settings

logger = logging.getLogger(__name__)

# Supported URL patterns
YOUTUBE_PATTERNS = ("youtube.com/watch", "youtu.be/", "youtube.com/live")
RTSP_PATTERNS = ("rtsp://", "rtsps://")
HLS_PATTERNS = (".m3u8",)


class VideoCapture:
    """Captures frames from public video streams and stores them in Azure Blob Storage."""

    def __init__(self):
        self._settings = get_settings()
        self._blob_client = BlobServiceClient.from_connection_string(
            self._settings.storage_connection_string
        )
        self._container = self._settings.frames_container

    def _get_stream_url(self, feed_url: str) -> str:
        """
        Resolve the actual stream URL from a video feed URL.
        For YouTube, uses yt-dlp to get the direct stream URL.
        """
        if any(p in feed_url for p in YOUTUBE_PATTERNS):
            return self._resolve_youtube_url(feed_url)
        # For RTSP/HLS/direct URLs, return as-is
        return feed_url

    def _resolve_youtube_url(self, youtube_url: str) -> str:
        """Use yt-dlp to get the best available stream URL for a live or VOD video."""
        ydl_opts = {
            # Prefer a direct video+audio format ≤720p.
            # For live streams yt-dlp returns an HLS manifest URL — OpenCV handles
            # HLS natively via FFmpeg, so we just need the manifest URL.
            # "best" as the final fallback ensures we always get *something*.
            "format": "best[height<=720]/best",
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            # Do NOT seek to the beginning of a live stream — we want the live edge.
            "live_from_start": False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
            if info is None:
                raise ValueError(f"Could not extract stream info from: {youtube_url}")

            url = None

            # For live streams prefer the manifest URL (HLS/DASH)
            if info.get("is_live"):
                url = info.get("manifest_url") or info.get("url")
            else:
                url = info.get("url")

            # Fall back to iterating formats (newest / highest quality first)
            if not url:
                for fmt in reversed(info.get("formats", [])):
                    if fmt.get("url"):
                        url = fmt["url"]
                        break

            if not url:
                raise ValueError(f"No stream URL found for: {youtube_url}")

            logger.info(
                "Resolved stream URL for %s (is_live=%s)",
                youtube_url,
                info.get("is_live"),
            )
            return url

    def capture_frames(
        self,
        feed_url: str,
        feed_id: int,
        interval_start: datetime,
        num_frames: int = 5,
        frame_interval_seconds: float = 10.0,
    ) -> list[tuple[bytes, str]]:
        """
        Capture multiple frames from a video stream.

        For live streams the capture strategy is:
          • Open the stream at the live edge.
          • Read a frame, sleep `frame_interval_seconds`, repeat.
        This avoids the previous approach of calling cap.grab() thousands of
        times to skip 60 s of video, which caused timeouts on live HLS streams.

        Args:
            feed_url: Source video URL
            feed_id: Database feed ID (for blob naming)
            interval_start: 5-minute interval start time
            num_frames: Number of frames to capture
            frame_interval_seconds: Real-time seconds to wait between frames.
                                    Default 10 s keeps total capture time ≤ ~50 s
                                    for 5 frames, well within the 10-min timeout.

        Returns:
            List of (jpeg_bytes, blob_url) tuples
        """
        frames = []
        diag: list[str] = []

        def _dlog(msg: str) -> None:
            logger.info(msg)
            diag.append(msg)

        def _flush_diag() -> None:
            try:
                content = "\n".join(diag).encode()
                blob = self._blob_client.get_blob_client(
                    self._container, f"capture-diag-feed{feed_id}.log"
                )
                blob.upload_blob(content, overwrite=True)
            except Exception:
                pass

        try:
            _dlog(f"capture_frames: resolving URL for feed_id={feed_id} url={feed_url}")
            stream_url = self._get_stream_url(feed_url)
            _dlog(f"capture_frames: resolved stream_url={stream_url[:120] if stream_url else 'None'}")
        except Exception as e:
            import traceback as _tb
            _dlog(f"capture_frames: FAIL resolve URL: {e}\n{_tb.format_exc()[:800]}")
            logger.error("Failed to resolve stream URL for %s: %s", feed_url, e)
            _flush_diag()
            return frames

        cap = None
        try:
            _dlog(f"capture_frames: opening cv2.VideoCapture({stream_url[:80]})")
            cap = cv2.VideoCapture(stream_url)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            opened = cap.isOpened()
            _dlog(f"capture_frames: cap.isOpened()={opened}")
            if not opened:
                logger.error("Could not open video stream: %s", feed_url)
                _flush_diag()
                return frames

            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            _dlog(f"capture_frames: stream opened fps={fps:.1f}")
            logger.info("Stream opened: fps=%.1f, feed_id=%d", fps, feed_id)

            for i in range(num_frames):
                if i > 0:
                    time.sleep(frame_interval_seconds)

                # Flush buffer then retrieve — never call cap.read() after cap.grab()
                for _ in range(3):
                    cap.grab()

                ret, frame = cap.retrieve()
                _dlog(f"capture_frames: frame {i+1}/{num_frames} ret={ret} frame_is_none={frame is None}")
                if not ret or frame is None:
                    logger.warning(
                        "Failed to read frame %d/%d from %s", i + 1, num_frames, feed_url
                    )
                    continue

                # Convert BGR → RGB
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                # Resize to max 1280×720 to reduce API costs
                frame_rgb = self._resize_frame(frame_rgb, max_width=1280, max_height=720)

                # Encode as JPEG
                jpeg_bytes = self._encode_jpeg(frame_rgb, quality=85)

                # Upload to blob storage
                blob_url = self._upload_frame(
                    jpeg_bytes=jpeg_bytes,
                    feed_id=feed_id,
                    interval_start=interval_start,
                    frame_index=i,
                )

                frames.append((jpeg_bytes, blob_url))
                _dlog(f"capture_frames: captured frame {i+1}/{num_frames} size={len(jpeg_bytes)} blob={blob_url[:60]}")
                logger.debug(
                    "Captured frame %d/%d for feed_id=%d", i + 1, num_frames, feed_id
                )

        except Exception as e:
            import traceback as _tb
            _dlog(f"capture_frames: EXCEPTION: {e}\n{_tb.format_exc()[:800]}")
            logger.error("Frame capture error for feed_id=%d: %s", feed_id, e)
        finally:
            if cap is not None:
                cap.release()
            _flush_diag()

        logger.info(
            "Captured %d/%d frames for feed_id=%d interval=%s",
            len(frames),
            num_frames,
            feed_id,
            interval_start.isoformat(),
        )
        return frames

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
        # Blob path: video-frames/feed_{id}/YYYY/MM/DD/HH/YYYYMMDD_HHMM_frame{NN}.jpg
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


# Singleton
_video_capture: Optional[VideoCapture] = None


def get_video_capture() -> VideoCapture:
    global _video_capture
    if _video_capture is None:
        _video_capture = VideoCapture()
    return _video_capture
