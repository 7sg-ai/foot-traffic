-- =============================================================================
-- Foot Traffic Analyzer - Azure Synapse Analytics Schema
-- =============================================================================
-- Run this against the Synapse Dedicated SQL Pool (foottrafficdw)
--
-- Synapse Dedicated SQL Pool restrictions vs standard T-SQL:
--   • DEFAULT constraints must be constants (no GETUTCDATE(), no functions)
--   • INSERT ... VALUES only accepts constant literals — no function calls.
--     Use INSERT ... SELECT to include expressions like GETUTCDATE().
--   • No multi-row VALUES (...), (...) with function calls — use separate INSERTs
--   • No PARTITION BY RANGE syntax — use DISTRIBUTION only
--   • No FOREIGN KEY, UNIQUE, or CHECK constraints
--   • CLUSTERED COLUMNSTORE INDEX does not support NVARCHAR(MAX) columns
--     → Use NVARCHAR(4000) max, or use HEAP for tables that need MAX columns
-- =============================================================================

-- ─── Schema ──────────────────────────────────────────────────────────────────
IF NOT EXISTS (SELECT * FROM sys.schemas WHERE name = 'traffic')
    EXEC('CREATE SCHEMA traffic');
GO

-- =============================================================================
-- Table: traffic.video_feeds
-- Catalog of monitored video feeds
-- =============================================================================
IF OBJECT_ID('traffic.video_feeds', 'U') IS NULL
CREATE TABLE traffic.video_feeds
(
    feed_id         INT             NOT NULL,
    feed_name       NVARCHAR(255)   NOT NULL,
    feed_url        NVARCHAR(2048)  NOT NULL,
    location_name   NVARCHAR(255)   NULL,
    latitude        DECIMAL(9,6)    NULL,
    longitude       DECIMAL(9,6)    NULL,
    timezone        NVARCHAR(64)    NOT NULL DEFAULT 'UTC',
    is_active       BIT             NOT NULL DEFAULT 1,
    created_at      DATETIME2       NOT NULL DEFAULT '1900-01-01',
    updated_at      DATETIME2       NOT NULL DEFAULT '1900-01-01'
)
WITH
(
    DISTRIBUTION = REPLICATE,
    CLUSTERED COLUMNSTORE INDEX
);
GO

-- =============================================================================
-- Table: traffic.raw_observations
-- Individual pedestrian observations from VLM analysis
-- Each row = one person detected in one frame
-- Note: Uses HEAP because vlm_raw_response can be large text (not CCI-compatible)
-- =============================================================================
IF OBJECT_ID('traffic.raw_observations', 'U') IS NULL
CREATE TABLE traffic.raw_observations
(
    observation_id      BIGINT          NOT NULL,
    feed_id             INT             NOT NULL,
    captured_at         DATETIME2       NOT NULL,
    interval_start      DATETIME2       NOT NULL,
    frame_blob_url      NVARCHAR(2048)  NULL,

    -- VLM-assigned demographics
    gender              NVARCHAR(32)    NULL,
    age_group           NVARCHAR(32)    NULL,
    age_estimate_min    INT             NULL,
    age_estimate_max    INT             NULL,
    apparent_ethnicity  NVARCHAR(64)    NULL,
    attire_type         NVARCHAR(64)    NULL,
    is_working          BIT             NULL,
    activity            NVARCHAR(128)   NULL,
    carrying_items      BIT             NULL,
    using_phone         BIT             NULL,
    group_size          INT             NULL,
    confidence_score    DECIMAL(5,4)    NULL,

    -- Raw VLM response (large text — stored truncated to 4000 chars)
    vlm_raw_response    NVARCHAR(4000)  NULL,

    -- Processing metadata
    processing_duration_ms  INT         NULL,
    model_version           NVARCHAR(64) NULL,
    created_at              DATETIME2   NOT NULL DEFAULT '1900-01-01'
)
WITH
(
    DISTRIBUTION = HASH(feed_id),
    CLUSTERED COLUMNSTORE INDEX
);
GO

-- =============================================================================
-- Table: traffic.interval_aggregates
-- Pre-aggregated 5-minute interval summaries per feed
-- =============================================================================
IF OBJECT_ID('traffic.interval_aggregates', 'U') IS NULL
CREATE TABLE traffic.interval_aggregates
(
    aggregate_id            BIGINT          NOT NULL,
    feed_id                 INT             NOT NULL,
    interval_start          DATETIME2       NOT NULL,
    interval_end            DATETIME2       NOT NULL,

    -- Counts
    total_count             INT             NOT NULL DEFAULT 0,
    frames_analyzed         INT             NOT NULL DEFAULT 0,

    -- Gender breakdown
    count_male              INT             NOT NULL DEFAULT 0,
    count_female            INT             NOT NULL DEFAULT 0,
    count_gender_unknown    INT             NOT NULL DEFAULT 0,

    -- Age group breakdown
    count_children          INT             NOT NULL DEFAULT 0,
    count_teens             INT             NOT NULL DEFAULT 0,
    count_young_adults      INT             NOT NULL DEFAULT 0,
    count_adults            INT             NOT NULL DEFAULT 0,
    count_seniors           INT             NOT NULL DEFAULT 0,
    avg_estimated_age       DECIMAL(5,1)    NULL,

    -- Ethnicity breakdown (JSON stored as bounded string)
    ethnicity_breakdown     NVARCHAR(4000)  NULL,

    -- Attire / activity
    count_business_attire   INT             NOT NULL DEFAULT 0,
    count_casual_attire     INT             NOT NULL DEFAULT 0,
    count_athletic_attire   INT             NOT NULL DEFAULT 0,
    count_uniform_attire    INT             NOT NULL DEFAULT 0,
    count_working           INT             NOT NULL DEFAULT 0,
    count_leisure           INT             NOT NULL DEFAULT 0,

    -- Activity breakdown
    count_walking           INT             NOT NULL DEFAULT 0,
    count_running           INT             NOT NULL DEFAULT 0,
    count_standing          INT             NOT NULL DEFAULT 0,
    count_cycling           INT             NOT NULL DEFAULT 0,
    count_shopping          INT             NOT NULL DEFAULT 0,

    -- Behavior
    count_using_phone       INT             NOT NULL DEFAULT 0,
    count_carrying_items    INT             NOT NULL DEFAULT 0,
    count_in_groups         INT             NOT NULL DEFAULT 0,

    -- Derived metrics
    pct_male                DECIMAL(5,2)    NULL,
    pct_female              DECIMAL(5,2)    NULL,
    pct_working             DECIMAL(5,2)    NULL,
    pct_using_phone         DECIMAL(5,2)    NULL,
    avg_confidence_score    DECIMAL(5,4)    NULL,

    -- Processing metadata
    processing_status       NVARCHAR(32)    NOT NULL DEFAULT 'pending',
    error_message           NVARCHAR(4000)  NULL,
    created_at              DATETIME2       NOT NULL DEFAULT '1900-01-01',
    updated_at              DATETIME2       NOT NULL DEFAULT '1900-01-01'
)
WITH
(
    DISTRIBUTION = HASH(feed_id),
    CLUSTERED COLUMNSTORE INDEX
);
GO

-- =============================================================================
-- Table: traffic.hourly_aggregates
-- Hourly rollups for faster dashboard queries
-- =============================================================================
IF OBJECT_ID('traffic.hourly_aggregates', 'U') IS NULL
CREATE TABLE traffic.hourly_aggregates
(
    aggregate_id            BIGINT          NOT NULL,
    feed_id                 INT             NOT NULL,
    hour_start              DATETIME2       NOT NULL,
    hour_end                DATETIME2       NOT NULL,

    total_count             INT             NOT NULL DEFAULT 0,
    count_male              INT             NOT NULL DEFAULT 0,
    count_female            INT             NOT NULL DEFAULT 0,
    count_working           INT             NOT NULL DEFAULT 0,
    count_leisure           INT             NOT NULL DEFAULT 0,
    count_children          INT             NOT NULL DEFAULT 0,
    count_teens             INT             NOT NULL DEFAULT 0,
    count_young_adults      INT             NOT NULL DEFAULT 0,
    count_adults            INT             NOT NULL DEFAULT 0,
    count_seniors           INT             NOT NULL DEFAULT 0,
    count_business_attire   INT             NOT NULL DEFAULT 0,
    count_casual_attire     INT             NOT NULL DEFAULT 0,
    count_using_phone       INT             NOT NULL DEFAULT 0,
    ethnicity_breakdown     NVARCHAR(4000)  NULL,
    avg_confidence_score    DECIMAL(5,4)    NULL,
    created_at              DATETIME2       NOT NULL DEFAULT '1900-01-01'
)
WITH
(
    DISTRIBUTION = HASH(feed_id),
    CLUSTERED COLUMNSTORE INDEX
);
GO

-- =============================================================================
-- Table: traffic.daily_aggregates
-- Daily rollups
-- =============================================================================
IF OBJECT_ID('traffic.daily_aggregates', 'U') IS NULL
CREATE TABLE traffic.daily_aggregates
(
    aggregate_id            BIGINT          NOT NULL,
    feed_id                 INT             NOT NULL,
    date_key                DATE            NOT NULL,

    total_count             INT             NOT NULL DEFAULT 0,
    peak_hour               INT             NULL,
    peak_count              INT             NULL,
    count_male              INT             NOT NULL DEFAULT 0,
    count_female            INT             NOT NULL DEFAULT 0,
    count_working           INT             NOT NULL DEFAULT 0,
    count_leisure           INT             NOT NULL DEFAULT 0,
    count_children          INT             NOT NULL DEFAULT 0,
    count_teens             INT             NOT NULL DEFAULT 0,
    count_young_adults      INT             NOT NULL DEFAULT 0,
    count_adults            INT             NOT NULL DEFAULT 0,
    count_seniors           INT             NOT NULL DEFAULT 0,
    count_business_attire   INT             NOT NULL DEFAULT 0,
    count_casual_attire     INT             NOT NULL DEFAULT 0,
    count_using_phone       INT             NOT NULL DEFAULT 0,
    ethnicity_breakdown     NVARCHAR(4000)  NULL,
    avg_confidence_score    DECIMAL(5,4)    NULL,
    created_at              DATETIME2       NOT NULL DEFAULT '1900-01-01'
)
WITH
(
    DISTRIBUTION = HASH(feed_id),
    CLUSTERED COLUMNSTORE INDEX
);
GO

-- =============================================================================
-- Table: traffic.analysis_jobs
-- Tracks each analysis run for observability
-- =============================================================================
IF OBJECT_ID('traffic.analysis_jobs', 'U') IS NULL
CREATE TABLE traffic.analysis_jobs
(
    job_id              NVARCHAR(64)    NOT NULL,
    feed_id             INT             NOT NULL,
    interval_start      DATETIME2       NOT NULL,
    status              NVARCHAR(32)    NOT NULL DEFAULT 'running',
    frames_captured     INT             NULL,
    persons_detected    INT             NULL,
    vlm_calls_made      INT             NULL,
    total_tokens_used   INT             NULL,
    duration_seconds    DECIMAL(10,2)   NULL,
    error_message       NVARCHAR(4000)  NULL,
    started_at          DATETIME2       NOT NULL DEFAULT '1900-01-01',
    completed_at        DATETIME2       NULL
)
WITH
(
    DISTRIBUTION = ROUND_ROBIN,
    CLUSTERED COLUMNSTORE INDEX
);
GO

-- =============================================================================
-- Seed: Default video feeds (TfL JamCams)
-- =============================================================================
-- Synapse Dedicated SQL Pool: INSERT ... VALUES only accepts constant literals —
-- no function calls (not even GETUTCDATE()). Use INSERT ... SELECT instead,
-- which does allow function calls. Each row is a separate idempotent statement.
IF NOT EXISTS (SELECT 1 FROM traffic.video_feeds WHERE feed_id = 1)
    INSERT INTO traffic.video_feeds (feed_id, feed_name, feed_url, location_name, latitude, longitude, timezone, created_at, updated_at)
    SELECT 1, 'Piccadilly Circus', 'https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/00001.07450.mp4', 'Piccadilly Circus, London, UK', 51.5096, -0.13484, 'Europe/London', GETUTCDATE(), GETUTCDATE();
GO

IF NOT EXISTS (SELECT 1 FROM traffic.video_feeds WHERE feed_id = 2)
    INSERT INTO traffic.video_feeds (feed_id, feed_name, feed_url, location_name, latitude, longitude, timezone, created_at, updated_at)
    SELECT 2, 'Oxford Street / Orchard Street', 'https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/00001.08858.mp4', 'Oxford Street / Orchard Street, London, UK', 51.514, -0.15409, 'Europe/London', GETUTCDATE(), GETUTCDATE();
GO

IF NOT EXISTS (SELECT 1 FROM traffic.video_feeds WHERE feed_id = 3)
    INSERT INTO traffic.video_feeds (feed_id, feed_name, feed_url, location_name, latitude, longitude, timezone, created_at, updated_at)
    SELECT 3, 'Hyde Park Corner', 'https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/00001.08750.mp4', 'Hyde Park Corner, London, UK', 51.5033, -0.15099, 'Europe/London', GETUTCDATE(), GETUTCDATE();
GO

IF NOT EXISTS (SELECT 1 FROM traffic.video_feeds WHERE feed_id = 4)
    INSERT INTO traffic.video_feeds (feed_id, feed_name, feed_url, location_name, latitude, longitude, timezone, created_at, updated_at)
    SELECT 4, 'Westminster Bridge', 'https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/00001.04502.mp4', 'Westminster Bridge, London, UK', 51.5009, -0.11762, 'Europe/London', GETUTCDATE(), GETUTCDATE();
GO

IF NOT EXISTS (SELECT 1 FROM traffic.video_feeds WHERE feed_id = 5)
    INSERT INTO traffic.video_feeds (feed_id, feed_name, feed_url, location_name, latitude, longitude, timezone, created_at, updated_at)
    SELECT 5, 'Tower Bridge Approach', 'https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/00001.03500.mp4', 'Tower Bridge Approach, London, UK', 51.509, -0.07368, 'Europe/London', GETUTCDATE(), GETUTCDATE();
GO

-- =============================================================================
-- Views for common queries
-- =============================================================================

-- Recent 24-hour summary per feed
IF OBJECT_ID('traffic.vw_recent_24h', 'V') IS NOT NULL
    DROP VIEW traffic.vw_recent_24h;
GO

CREATE VIEW traffic.vw_recent_24h AS
SELECT
    f.feed_name,
    f.location_name,
    SUM(ia.total_count)            AS total_pedestrians,
    SUM(ia.count_male)             AS total_male,
    SUM(ia.count_female)           AS total_female,
    SUM(ia.count_working)          AS total_working,
    SUM(ia.count_leisure)          AS total_leisure,
    SUM(ia.count_children)         AS total_children,
    SUM(ia.count_teens)            AS total_teens,
    SUM(ia.count_young_adults)     AS total_young_adults,
    SUM(ia.count_adults)           AS total_adults,
    SUM(ia.count_seniors)          AS total_seniors,
    SUM(ia.count_business_attire)  AS total_business_attire,
    SUM(ia.count_casual_attire)    AS total_casual_attire,
    SUM(ia.count_using_phone)      AS total_using_phone,
    AVG(ia.avg_confidence_score)   AS avg_confidence,
    COUNT(*)                       AS intervals_analyzed
FROM traffic.interval_aggregates ia
JOIN traffic.video_feeds f ON f.feed_id = ia.feed_id
WHERE ia.interval_start >= DATEADD(HOUR, -24, GETUTCDATE())
  AND ia.processing_status = 'complete'
GROUP BY f.feed_name, f.location_name;
GO

-- Hourly trend for last 7 days
IF OBJECT_ID('traffic.vw_hourly_trend_7d', 'V') IS NOT NULL
    DROP VIEW traffic.vw_hourly_trend_7d;
GO

CREATE VIEW traffic.vw_hourly_trend_7d AS
SELECT
    f.feed_name,
    f.location_name,
    DATEPART(HOUR, ia.interval_start)    AS hour_of_day,
    DATEPART(WEEKDAY, ia.interval_start) AS day_of_week,
    AVG(CAST(ia.total_count AS FLOAT))   AS avg_pedestrians,
    AVG(ia.pct_male)                     AS avg_pct_male,
    AVG(ia.pct_female)                   AS avg_pct_female,
    AVG(ia.pct_working)                  AS avg_pct_working,
    AVG(ia.pct_using_phone)              AS avg_pct_using_phone
FROM traffic.interval_aggregates ia
JOIN traffic.video_feeds f ON f.feed_id = ia.feed_id
WHERE ia.interval_start >= DATEADD(DAY, -7, GETUTCDATE())
  AND ia.processing_status = 'complete'
GROUP BY
    f.feed_name,
    f.location_name,
    DATEPART(HOUR, ia.interval_start),
    DATEPART(WEEKDAY, ia.interval_start);
GO

PRINT 'Schema initialization complete.';
GO
