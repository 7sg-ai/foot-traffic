"""
Pydantic models for demographic observations and aggregates.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class PersonObservation(BaseModel):
    """A single person detected and analyzed by the VLM."""

    # Identity
    person_index: int = Field(..., description="Index of person in the frame (1-based)")

    # Demographics (all VLM estimates)
    gender: Optional[str] = Field(None, description="'male', 'female', or 'unknown'")
    age_group: Optional[str] = Field(
        None,
        description="'child' (0-12), 'teen' (13-17), 'young_adult' (18-35), 'adult' (36-60), 'senior' (60+)"
    )
    age_estimate_min: Optional[int] = Field(None, ge=0, le=120)
    age_estimate_max: Optional[int] = Field(None, ge=0, le=120)
    apparent_ethnicity: Optional[str] = Field(
        None,
        description="Broad visual category: 'white', 'black', 'hispanic', 'east_asian', 'south_asian', 'middle_eastern', 'mixed', 'unknown'"
    )

    # Behavior / attire
    attire_type: Optional[str] = Field(
        None,
        description="'business', 'casual', 'athletic', 'uniform', 'formal', 'other'"
    )
    is_working: Optional[bool] = Field(None, description="Appears to be commuting/working vs leisure")
    activity: Optional[str] = Field(
        None,
        description="'walking', 'running', 'standing', 'cycling', 'shopping', 'sitting', 'other'"
    )
    carrying_items: Optional[bool] = Field(None, description="Carrying bags, briefcase, etc.")
    using_phone: Optional[bool] = Field(None, description="Visibly using a mobile phone")
    group_size: Optional[int] = Field(None, ge=1, description="1=alone, 2+=group")

    # Confidence
    confidence_score: float = Field(0.7, ge=0.0, le=1.0)

    @field_validator("gender")
    @classmethod
    def validate_gender(cls, v):
        if v and v not in ("male", "female", "unknown"):
            return "unknown"
        return v

    @field_validator("age_group")
    @classmethod
    def validate_age_group(cls, v):
        valid = ("child", "teen", "young_adult", "adult", "senior")
        if v and v not in valid:
            return None
        return v

    @field_validator("attire_type")
    @classmethod
    def validate_attire(cls, v):
        valid = ("business", "casual", "athletic", "uniform", "formal", "other")
        if v and v not in valid:
            return "other"
        return v

    @field_validator("activity")
    @classmethod
    def validate_activity(cls, v):
        valid = ("walking", "running", "standing", "cycling", "shopping", "sitting", "other")
        if v and v not in valid:
            return "other"
        return v


class FrameAnalysisResult(BaseModel):
    """Result of analyzing a single video frame."""

    feed_id: int
    feed_url: str
    captured_at: datetime
    interval_start: datetime
    frame_blob_url: Optional[str] = None

    persons: list[PersonObservation] = Field(default_factory=list)
    total_persons_detected: int = 0
    scene_description: Optional[str] = None
    weather_conditions: Optional[str] = None
    time_of_day: Optional[str] = None  # 'morning', 'afternoon', 'evening', 'night'
    crowd_density: Optional[str] = None  # 'sparse', 'moderate', 'dense', 'very_dense'

    processing_duration_ms: int = 0
    model_version: str = "gpt-5.3-chat"
    vlm_raw_response: Optional[str] = None
    tokens_this_call: int = 0  # tokens used for this specific frame's VLM call
    error: Optional[str] = None

    @property
    def person_count(self) -> int:
        return len(self.persons)


class IntervalAggregate(BaseModel):
    """5-minute interval aggregate for a single feed."""

    feed_id: int
    interval_start: datetime
    interval_end: datetime

    total_count: int = 0
    frames_analyzed: int = 0

    # Gender
    count_male: int = 0
    count_female: int = 0
    count_gender_unknown: int = 0

    # Age groups
    count_children: int = 0
    count_teens: int = 0
    count_young_adults: int = 0
    count_adults: int = 0
    count_seniors: int = 0
    avg_estimated_age: Optional[float] = None

    # Ethnicity (JSON dict)
    ethnicity_breakdown: dict = Field(default_factory=dict)

    # Attire
    count_business_attire: int = 0
    count_casual_attire: int = 0
    count_athletic_attire: int = 0
    count_uniform_attire: int = 0

    # Work/leisure
    count_working: int = 0
    count_leisure: int = 0

    # Activity
    count_walking: int = 0
    count_running: int = 0
    count_standing: int = 0
    count_cycling: int = 0
    count_shopping: int = 0

    # Behavior
    count_using_phone: int = 0
    count_carrying_items: int = 0
    count_in_groups: int = 0

    # Derived
    pct_male: Optional[float] = None
    pct_female: Optional[float] = None
    pct_working: Optional[float] = None
    pct_using_phone: Optional[float] = None
    avg_confidence_score: Optional[float] = None

    processing_status: str = "complete"
    error_message: Optional[str] = None

    @classmethod
    def from_frame_results(
        cls,
        feed_id: int,
        interval_start: datetime,
        interval_end: datetime,
        frame_results: list[FrameAnalysisResult],
    ) -> "IntervalAggregate":
        """Build an aggregate from a list of frame analysis results."""
        agg = cls(
            feed_id=feed_id,
            interval_start=interval_start,
            interval_end=interval_end,
            frames_analyzed=len(frame_results),
        )

        all_persons: list[PersonObservation] = []
        confidence_scores: list[float] = []
        age_estimates: list[float] = []
        ethnicity_counts: dict[str, int] = {}

        for frame in frame_results:
            if frame.error:
                continue
            all_persons.extend(frame.persons)

        agg.total_count = len(all_persons)

        for p in all_persons:
            confidence_scores.append(p.confidence_score)

            # Gender
            if p.gender == "male":
                agg.count_male += 1
            elif p.gender == "female":
                agg.count_female += 1
            else:
                agg.count_gender_unknown += 1

            # Age group
            if p.age_group == "child":
                agg.count_children += 1
            elif p.age_group == "teen":
                agg.count_teens += 1
            elif p.age_group == "young_adult":
                agg.count_young_adults += 1
            elif p.age_group == "adult":
                agg.count_adults += 1
            elif p.age_group == "senior":
                agg.count_seniors += 1

            # Age estimate midpoint
            if p.age_estimate_min is not None and p.age_estimate_max is not None:
                age_estimates.append((p.age_estimate_min + p.age_estimate_max) / 2)

            # Ethnicity
            if p.apparent_ethnicity:
                ethnicity_counts[p.apparent_ethnicity] = (
                    ethnicity_counts.get(p.apparent_ethnicity, 0) + 1
                )

            # Attire
            if p.attire_type == "business":
                agg.count_business_attire += 1
            elif p.attire_type == "casual":
                agg.count_casual_attire += 1
            elif p.attire_type == "athletic":
                agg.count_athletic_attire += 1
            elif p.attire_type == "uniform":
                agg.count_uniform_attire += 1

            # Work/leisure
            if p.is_working is True:
                agg.count_working += 1
            elif p.is_working is False:
                agg.count_leisure += 1

            # Activity
            if p.activity == "walking":
                agg.count_walking += 1
            elif p.activity == "running":
                agg.count_running += 1
            elif p.activity == "standing":
                agg.count_standing += 1
            elif p.activity == "cycling":
                agg.count_cycling += 1
            elif p.activity == "shopping":
                agg.count_shopping += 1

            # Behavior
            if p.using_phone:
                agg.count_using_phone += 1
            if p.carrying_items:
                agg.count_carrying_items += 1
            if p.group_size and p.group_size > 1:
                agg.count_in_groups += 1

        # Derived percentages
        if agg.total_count > 0:
            agg.pct_male = round(agg.count_male / agg.total_count * 100, 2)
            agg.pct_female = round(agg.count_female / agg.total_count * 100, 2)
            agg.pct_working = round(agg.count_working / agg.total_count * 100, 2)
            agg.pct_using_phone = round(agg.count_using_phone / agg.total_count * 100, 2)

        if confidence_scores:
            agg.avg_confidence_score = round(sum(confidence_scores) / len(confidence_scores), 4)

        if age_estimates:
            agg.avg_estimated_age = round(sum(age_estimates) / len(age_estimates), 1)

        agg.ethnicity_breakdown = ethnicity_counts

        return agg

    def ethnicity_breakdown_json(self) -> str:
        return json.dumps(self.ethnicity_breakdown)


class VideoFeed(BaseModel):
    """A registered video feed."""
    feed_id: int
    feed_name: str
    feed_url: str
    location_name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: str = "UTC"
    is_active: bool = True


class ZeroPersonFrame:
    """Lightweight container for a zero-person sentinel row from raw_observations.

    Represents a frame that was previously analyzed by the VLM but returned
    0 people detected.  Used by the reprocessor to identify frames that should
    be re-analyzed after a container restart.
    """

    __slots__ = ("feed_id", "captured_at", "interval_start", "frame_blob_url")

    def __init__(
        self,
        feed_id: int,
        captured_at: datetime,
        interval_start: datetime,
        frame_blob_url: str,
    ) -> None:
        self.feed_id = feed_id
        self.captured_at = captured_at
        self.interval_start = interval_start
        self.frame_blob_url = frame_blob_url
