"""
Monitor Page - System health, job status, and feed management.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_synapse_client

st.set_page_config(
    page_title="Monitor | Foot Traffic",
    page_icon="⚙️",
    layout="wide",
)

st.title("⚙️ System Monitor")
st.markdown("Monitor analysis jobs, feed health, and system performance.")

db = get_synapse_client()

# ─── Auto-refresh ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## Monitor Settings")
    auto_refresh = st.checkbox("Auto-refresh (30s)", value=False)
    if st.button("🔄 Refresh Now"):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.markdown("### 📹 Feed Management")
    st.markdown("Add or manage video feeds via the database.")
    st.code("""
-- Add a new feed
INSERT INTO traffic.video_feeds
  (feed_id, feed_name, feed_url, location_name, timezone)
VALUES
  (4, 'My Camera', 'https://...', 'Location', 'UTC');
    """, language="sql")

# ─── Job Status Overview ──────────────────────────────────────────────────────
st.markdown("### 📊 Recent Analysis Jobs")

jobs_df = db.get_recent_jobs(limit=50)

if jobs_df.empty:
    st.info("No analysis jobs found. The scheduler may not have run yet.")
else:
    # Status summary
    col1, col2, col3, col4 = st.columns(4)

    status_counts = jobs_df["status"].value_counts()
    success_count = status_counts.get("success", 0)
    failed_count = status_counts.get("failed", 0)
    running_count = status_counts.get("running", 0)
    total_persons = jobs_df["persons_detected"].sum() if "persons_detected" in jobs_df.columns else 0

    with col1:
        st.metric("✅ Successful Jobs", success_count)
    with col2:
        st.metric("❌ Failed Jobs", failed_count, delta=f"-{failed_count}" if failed_count > 0 else None)
    with col3:
        st.metric("🔄 Running", running_count)
    with col4:
        st.metric("👥 Total Persons Detected", f"{int(total_persons):,}")

    # Success rate gauge
    total_jobs = len(jobs_df)
    success_rate = (success_count / total_jobs * 100) if total_jobs > 0 else 0

    fig_gauge = go.Figure(go.Indicator(
        mode="gauge+number",
        value=success_rate,
        title={"text": "Job Success Rate (%)"},
        gauge={
            "axis": {"range": [0, 100]},
            "bar": {"color": "#2d5986"},
            "steps": [
                {"range": [0, 50], "color": "#e74c3c"},
                {"range": [50, 80], "color": "#f39c12"},
                {"range": [80, 100], "color": "#2ecc71"},
            ],
            "threshold": {
                "line": {"color": "red", "width": 4},
                "thickness": 0.75,
                "value": 90,
            },
        },
        number={"suffix": "%", "font": {"size": 28}},
    ))
    fig_gauge.update_layout(height=250, margin=dict(l=20, r=20, t=40, b=20))

    col1, col2 = st.columns([1, 2])
    with col1:
        st.plotly_chart(fig_gauge, use_container_width=True)
    with col2:
        # Jobs timeline
        if "started_at" in jobs_df.columns:
            jobs_df_sorted = jobs_df.sort_values("started_at")
            color_map = {"success": "#2ecc71", "failed": "#e74c3c", "running": "#f39c12"}

            fig_timeline = px.scatter(
                jobs_df_sorted,
                x="started_at",
                y="feed_name",
                color="status",
                color_discrete_map=color_map,
                size="persons_detected",
                size_max=20,
                hover_data=["frames_captured", "duration_seconds", "total_tokens_used"],
                title="Job Timeline",
            )
            fig_timeline.update_layout(
                height=250,
                margin=dict(l=0, r=0, t=40, b=0),
                plot_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig_timeline, use_container_width=True)

    # Jobs table
    st.markdown("#### Job Details")
    display_cols = [
        "feed_name", "interval_start", "status",
        "frames_captured", "persons_detected", "duration_seconds",
        "total_tokens_used", "error_message",
    ]
    available_cols = [c for c in display_cols if c in jobs_df.columns]

    def style_status(val):
        if val == "success":
            return "background-color: rgba(46, 204, 113, 0.2); color: #27ae60"
        elif val == "failed":
            return "background-color: rgba(231, 76, 60, 0.2); color: #c0392b"
        elif val == "running":
            return "background-color: rgba(243, 156, 18, 0.2); color: #e67e22"
        return ""

    styled_df = jobs_df[available_cols].style.applymap(
        style_status, subset=["status"]
    )
    st.dataframe(styled_df, use_container_width=True, height=300)

# ─── Feed Health ──────────────────────────────────────────────────────────────
st.markdown("---")
st.markdown("### 📹 Feed Health Status")

feeds_df = db.get_feeds_dataframe()

if feeds_df.empty:
    st.warning("No feeds configured.")
else:
    # Check last analysis time per feed
    try:
        last_analysis_sql = """
        SELECT
            f.feed_id,
            f.feed_name,
            f.location_name,
            f.feed_url,
            f.is_active,
            MAX(ia.interval_start) AS last_analysis,
            COUNT(ia.aggregate_id) AS total_intervals,
            SUM(ia.total_count) AS total_persons,
            AVG(ia.avg_confidence_score) AS avg_confidence
        FROM traffic.video_feeds f
        LEFT JOIN traffic.interval_aggregates ia
            ON ia.feed_id = f.feed_id
            AND ia.processing_status = 'complete'
        GROUP BY f.feed_id, f.feed_name, f.location_name, f.feed_url, f.is_active
        ORDER BY f.feed_id
        """
        feed_health_df = db.execute_custom_query(last_analysis_sql)

        if not feed_health_df.empty:
            now = datetime.now(timezone.utc)

            for _, row in feed_health_df.iterrows():
                with st.container():
                    col1, col2, col3, col4, col5 = st.columns([2, 2, 1, 1, 1])

                    with col1:
                        status_icon = "🟢" if row.get("is_active") else "🔴"
                        st.markdown(f"**{status_icon} {row['feed_name']}**")
                        st.caption(row.get("location_name", "Unknown location"))

                    with col2:
                        if row.get("last_analysis") is not None:
                            last = pd.to_datetime(row["last_analysis"])
                            if last.tzinfo is None:
                                last = last.replace(tzinfo=timezone.utc)
                            age_min = (now - last).total_seconds() / 60
                            if age_min < 10:
                                st.markdown(f"🟢 **Live** ({age_min:.0f}m ago)")
                            elif age_min < 30:
                                st.markdown(f"🟡 **Recent** ({age_min:.0f}m ago)")
                            else:
                                st.markdown(f"🔴 **Stale** ({age_min:.0f}m ago)")
                        else:
                            st.markdown("⚪ **No data yet**")

                    with col3:
                        st.metric("Intervals", f"{int(row.get('total_intervals', 0)):,}")

                    with col4:
                        st.metric("Persons", f"{int(row.get('total_persons', 0) or 0):,}")

                    with col5:
                        conf = row.get("avg_confidence")
                        if conf:
                            st.metric("Confidence", f"{float(conf):.2f}")
                        else:
                            st.metric("Confidence", "N/A")

                    st.divider()

    except Exception as e:
        st.error(f"Failed to load feed health: {e}")
        st.dataframe(feeds_df, use_container_width=True)

# ─── Performance Metrics ──────────────────────────────────────────────────────
st.markdown("---")
st.markdown("### 📈 Processing Performance")

if not jobs_df.empty and "duration_seconds" in jobs_df.columns:
    perf_df = jobs_df[jobs_df["status"] == "success"].copy()

    if not perf_df.empty:
        col1, col2 = st.columns(2)

        with col1:
            fig_duration = px.histogram(
                perf_df,
                x="duration_seconds",
                nbins=20,
                title="Job Duration Distribution (seconds)",
                color_discrete_sequence=["#2d5986"],
            )
            fig_duration.update_layout(
                height=300,
                plot_bgcolor="rgba(0,0,0,0)",
                margin=dict(l=0, r=0, t=40, b=0),
            )
            st.plotly_chart(fig_duration, use_container_width=True)

        with col2:
            if "total_tokens_used" in perf_df.columns:
                fig_tokens = px.scatter(
                    perf_df,
                    x="persons_detected",
                    y="total_tokens_used",
                    color="feed_name",
                    title="Tokens Used vs Persons Detected",
                    trendline="ols",
                )
                fig_tokens.update_layout(
                    height=300,
                    plot_bgcolor="rgba(0,0,0,0)",
                    margin=dict(l=0, r=0, t=40, b=0),
                )
                st.plotly_chart(fig_tokens, use_container_width=True)

        # Summary stats
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Avg Duration", f"{perf_df['duration_seconds'].mean():.1f}s")
        with col2:
            st.metric("Max Duration", f"{perf_df['duration_seconds'].max():.1f}s")
        with col3:
            if "total_tokens_used" in perf_df.columns:
                st.metric("Total Tokens Used", f"{perf_df['total_tokens_used'].sum():,.0f}")
        with col4:
            if "persons_detected" in perf_df.columns:
                st.metric("Avg Persons/Job", f"{perf_df['persons_detected'].mean():.1f}")

# ─── Auto-refresh ─────────────────────────────────────────────────────────────
if auto_refresh:
    import time
    time.sleep(30)
    st.rerun()
