"""
Analytics Page - Deep-dive demographic analysis with advanced charts.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_synapse_client

st.set_page_config(
    page_title="Analytics | Foot Traffic",
    page_icon="📊",
    layout="wide",
)

st.title("📊 Advanced Analytics")
st.markdown("Deep-dive demographic analysis and trend comparisons.")

# ─── Filters ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## Filters")
    days_back = st.slider("Days of data", min_value=1, max_value=30, value=7)
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=days_back)

    db = get_synapse_client()
    feeds_df = db.get_feeds_dataframe()
    feed_options = ["All Feeds"] + (feeds_df["feed_name"].tolist() if not feeds_df.empty else [])
    feed_id_map = dict(zip(feeds_df["feed_name"], feeds_df["feed_id"])) if not feeds_df.empty else {}
    selected_feed = st.selectbox("Feed", feed_options)
    feed_id = feed_id_map.get(selected_feed) if selected_feed != "All Feeds" else None

# ─── Load data ────────────────────────────────────────────────────────────────
with st.spinner("Loading analytics data..."):
    df = db.get_interval_aggregates_df(
        feed_id=feed_id,
        start_time=start_time,
        end_time=end_time,
        limit=5000,
    )

if df.empty:
    st.warning("No data available for the selected period.")
    st.stop()

# ─── Tab layout ──────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs(["📈 Trends", "👥 Demographics", "🕐 Time Patterns", "📍 Feed Comparison"])

# ─── Tab 1: Trends ────────────────────────────────────────────────────────────
with tab1:
    st.subheader("Traffic Trends Over Time")

    # Rolling average
    df_sorted = df.sort_values("interval_start")
    df_sorted["rolling_avg"] = df_sorted["total_count"].rolling(window=12, min_periods=1).mean()

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df_sorted["interval_start"],
        y=df_sorted["total_count"],
        mode="lines",
        name="Raw Count",
        line=dict(color="rgba(45, 89, 134, 0.3)", width=1),
    ))
    fig.add_trace(go.Scatter(
        x=df_sorted["interval_start"],
        y=df_sorted["rolling_avg"],
        mode="lines",
        name="1-Hour Rolling Avg",
        line=dict(color="#2d5986", width=2.5),
    ))
    fig.update_layout(
        title="Pedestrian Count with Rolling Average",
        height=400,
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0, r=0, t=40, b=0),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Working vs Leisure trend
    col1, col2 = st.columns(2)
    with col1:
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(
            x=df_sorted["interval_start"],
            y=df_sorted["pct_working"],
            mode="lines",
            name="% Working",
            line=dict(color="#2d5986", width=2),
            fill="tozeroy",
            fillcolor="rgba(45, 89, 134, 0.1)",
        ))
        fig2.update_layout(
            title="% Working/Commuting Over Time",
            height=300,
            yaxis=dict(range=[0, 100], ticksuffix="%"),
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig2, use_container_width=True)

    with col2:
        fig3 = go.Figure()
        fig3.add_trace(go.Scatter(
            x=df_sorted["interval_start"],
            y=df_sorted["pct_using_phone"],
            mode="lines",
            name="% Using Phone",
            line=dict(color="#9b59b6", width=2),
            fill="tozeroy",
            fillcolor="rgba(155, 89, 182, 0.1)",
        ))
        fig3.update_layout(
            title="% Using Phone Over Time",
            height=300,
            yaxis=dict(range=[0, 100], ticksuffix="%"),
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig3, use_container_width=True)

# ─── Tab 2: Demographics ──────────────────────────────────────────────────────
with tab2:
    st.subheader("Demographic Deep Dive")

    col1, col2 = st.columns(2)

    with col1:
        # Gender over time (stacked area)
        fig_gender = go.Figure()
        fig_gender.add_trace(go.Scatter(
            x=df_sorted["interval_start"],
            y=df_sorted["count_male"],
            mode="lines",
            name="Male",
            stackgroup="one",
            line=dict(color="#3498db"),
            fillcolor="rgba(52, 152, 219, 0.6)",
        ))
        fig_gender.add_trace(go.Scatter(
            x=df_sorted["interval_start"],
            y=df_sorted["count_female"],
            mode="lines",
            name="Female",
            stackgroup="one",
            line=dict(color="#e74c3c"),
            fillcolor="rgba(231, 76, 60, 0.6)",
        ))
        fig_gender.update_layout(
            title="Gender Split Over Time (Stacked)",
            height=350,
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig_gender, use_container_width=True)

    with col2:
        # Age group over time
        age_cols = {
            "Children": "count_children",
            "Teens": "count_teens",
            "Young Adults": "count_young_adults",
            "Adults": "count_adults",
            "Seniors": "count_seniors",
        }
        colors = ["#1abc9c", "#3498db", "#2d5986", "#e67e22", "#95a5a6"]

        fig_age = go.Figure()
        for (label, col), color in zip(age_cols.items(), colors):
            if col in df_sorted.columns:
                fig_age.add_trace(go.Scatter(
                    x=df_sorted["interval_start"],
                    y=df_sorted[col],
                    mode="lines",
                    name=label,
                    stackgroup="one",
                    line=dict(color=color),
                ))
        fig_age.update_layout(
            title="Age Groups Over Time (Stacked)",
            height=350,
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig_age, use_container_width=True)

    # Ethnicity breakdown aggregated
    st.subheader("Ethnicity Distribution (Aggregated)")
    st.caption("⚠️ VLM visual estimates only — not ground truth.")

    ethnicity_totals: dict[str, int] = {}
    for val in df["ethnicity_breakdown"].dropna():
        try:
            d = json.loads(val) if isinstance(val, str) else val
            for k, v in d.items():
                ethnicity_totals[k] = ethnicity_totals.get(k, 0) + int(v)
        except Exception:
            continue

    if ethnicity_totals:
        eth_df = pd.DataFrame(
            list(ethnicity_totals.items()), columns=["Ethnicity", "Count"]
        ).sort_values("Count", ascending=False)
        total_eth = eth_df["Count"].sum()
        eth_df["Percentage"] = (eth_df["Count"] / total_eth * 100).round(1)

        col1, col2 = st.columns(2)
        with col1:
            fig_eth_pie = px.pie(
                eth_df,
                values="Count",
                names="Ethnicity",
                title="Ethnicity Distribution",
                color_discrete_sequence=px.colors.qualitative.Set3,
                hole=0.3,
            )
            fig_eth_pie.update_layout(height=350, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig_eth_pie, use_container_width=True)

        with col2:
            fig_eth_bar = px.bar(
                eth_df,
                x="Ethnicity",
                y="Percentage",
                title="Ethnicity % Breakdown",
                color="Ethnicity",
                color_discrete_sequence=px.colors.qualitative.Set3,
                text="Percentage",
            )
            fig_eth_bar.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
            fig_eth_bar.update_layout(
                height=350,
                showlegend=False,
                plot_bgcolor="rgba(0,0,0,0)",
                margin=dict(l=0, r=0, t=40, b=0),
            )
            st.plotly_chart(fig_eth_bar, use_container_width=True)

# ─── Tab 3: Time Patterns ─────────────────────────────────────────────────────
with tab3:
    st.subheader("Time-Based Traffic Patterns")

    df_copy = df.copy()
    df_copy["hour"] = pd.to_datetime(df_copy["interval_start"]).dt.hour
    df_copy["day_name"] = pd.to_datetime(df_copy["interval_start"]).dt.day_name()
    df_copy["is_weekend"] = pd.to_datetime(df_copy["interval_start"]).dt.dayofweek >= 5

    col1, col2 = st.columns(2)

    with col1:
        # Average by hour of day
        hourly = df_copy.groupby("hour").agg(
            avg_count=("total_count", "mean"),
            avg_pct_working=("pct_working", "mean"),
        ).reset_index()

        fig_hourly = make_subplots(specs=[[{"secondary_y": True}]])
        fig_hourly.add_trace(
            go.Bar(x=hourly["hour"], y=hourly["avg_count"], name="Avg Count", marker_color="#2d5986"),
            secondary_y=False,
        )
        fig_hourly.add_trace(
            go.Scatter(x=hourly["hour"], y=hourly["avg_pct_working"], name="% Working",
                      line=dict(color="#e74c3c", width=2), mode="lines+markers"),
            secondary_y=True,
        )
        fig_hourly.update_layout(
            title="Average Traffic by Hour of Day",
            height=350,
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, t=40, b=0),
        )
        fig_hourly.update_xaxes(title_text="Hour of Day (UTC)")
        fig_hourly.update_yaxes(title_text="Avg Pedestrian Count", secondary_y=False)
        fig_hourly.update_yaxes(title_text="% Working", secondary_y=True)
        st.plotly_chart(fig_hourly, use_container_width=True)

    with col2:
        # Weekday vs Weekend
        day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        daily = df_copy.groupby("day_name").agg(
            avg_count=("total_count", "mean"),
        ).reset_index()
        daily["day_name"] = pd.Categorical(daily["day_name"], categories=day_order, ordered=True)
        daily = daily.sort_values("day_name")

        colors_day = ["#2d5986"] * 5 + ["#e74c3c"] * 2
        fig_daily = px.bar(
            daily,
            x="day_name",
            y="avg_count",
            title="Average Traffic by Day of Week",
            color="day_name",
            color_discrete_sequence=colors_day,
        )
        fig_daily.update_layout(
            height=350,
            showlegend=False,
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig_daily, use_container_width=True)

    # Full heatmap
    day_order_full = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    pivot = df_copy.pivot_table(
        values="total_count",
        index="day_name",
        columns="hour",
        aggfunc="mean",
    ).reindex([d for d in day_order_full if d in df_copy["day_name"].unique()])

    if not pivot.empty:
        fig_heat = px.imshow(
            pivot,
            labels=dict(x="Hour of Day (UTC)", y="Day of Week", color="Avg Pedestrians"),
            color_continuous_scale="Blues",
            title="Traffic Heatmap: Day × Hour",
            aspect="auto",
        )
        fig_heat.update_layout(height=400, margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig_heat, use_container_width=True)

# ─── Tab 4: Feed Comparison ───────────────────────────────────────────────────
with tab4:
    st.subheader("Feed Location Comparison")

    if "feed_name" not in df.columns or df["feed_name"].nunique() < 2:
        st.info("Select 'All Feeds' and ensure multiple feeds have data to compare.")
    else:
        feed_summary = df.groupby("feed_name").agg(
            total_pedestrians=("total_count", "sum"),
            avg_per_interval=("total_count", "mean"),
            pct_male=("pct_male", "mean"),
            pct_female=("pct_female", "mean"),
            pct_working=("pct_working", "mean"),
            pct_phone=("pct_using_phone", "mean"),
            intervals=("total_count", "count"),
        ).reset_index()

        fig_compare = px.bar(
            feed_summary,
            x="feed_name",
            y="total_pedestrians",
            title="Total Pedestrians by Feed Location",
            color="feed_name",
            text="total_pedestrians",
        )
        fig_compare.update_traces(texttemplate="%{text:,}", textposition="outside")
        fig_compare.update_layout(
            height=350,
            showlegend=False,
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig_compare, use_container_width=True)

        # Radar chart for demographic comparison
        categories = ["% Male", "% Female", "% Working", "% Phone", "Avg/Interval (norm)"]
        max_avg = feed_summary["avg_per_interval"].max() or 1

        fig_radar = go.Figure()
        for _, row in feed_summary.iterrows():
            values = [
                row["pct_male"] or 0,
                row["pct_female"] or 0,
                row["pct_working"] or 0,
                row["pct_phone"] or 0,
                (row["avg_per_interval"] / max_avg * 100) if max_avg > 0 else 0,
            ]
            fig_radar.add_trace(go.Scatterpolar(
                r=values + [values[0]],
                theta=categories + [categories[0]],
                fill="toself",
                name=row["feed_name"],
                opacity=0.7,
            ))

        fig_radar.update_layout(
            polar=dict(radialaxis=dict(visible=True, range=[0, 100])),
            title="Demographic Profile Comparison",
            height=450,
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig_radar, use_container_width=True)

        st.dataframe(feed_summary, use_container_width=True)
