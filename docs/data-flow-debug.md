# Data Flow Verification & Debug Guide

End-to-end walkthrough of how data moves through the Foot Traffic Analyzer, with verification commands and debug steps at each stage.

---

## Data Flow Overview

```
Video Feed URLs (TfL JamCams)
     │
     ▼
[1] video_scheduler (Timer, every 5 min)
     │  reads active feeds from Synapse
     ▼
[2] VideoCapture
     │  downloads TfL JamCam MP4 clip (or JPEG still fallback)
     │  extracts N frames via OpenCV
     │  uploads JPEGs → Azure Blob Storage (video-frames/)
     ▼
[3] VLMAnalyzer (Azure OpenAI gpt-5.3-chat)
     │  sends base64 frame + system prompt
     │  receives structured JSON (persons array)
     ▼
[4] SynapseClient
     │  INSERT → traffic.raw_observations   (one row per person)
     │  UPSERT → traffic.interval_aggregates (one row per feed/interval)
     │  INSERT → traffic.analysis_jobs       (job audit log)
     ▼
[5] Streamlit Dashboard
     │  reads aggregates + jobs from Synapse
     │  renders Analytics / Query / Monitor pages
     ▼
[6] AI Query (Azure OpenAI)
     │  natural language → SQL → results
```

---

## Stage 1 — Timer Trigger & Feed Resolution

**What happens:** `video_scheduler` fires every 5 minutes and reads active feeds from `traffic.video_feeds`.

### Verify the scheduler is running

```bash
# Check the Functions Container App is up and has 1 replica
az containerapp show \
  --name <func-container-app-name> \
  --resource-group <resource-group> \
  --query "properties.template.scale" -o json

# Stream live logs from the Functions container
az containerapp logs show \
  --name <func-container-app-name> \
  --resource-group <resource-group> \
  --follow --tail 50
```

**Expected log lines every 5 minutes:**
```
Video scheduler triggered at 2026-04-03T20:00:00+00:00 | interval: 2026-04-03T20:00:00 -> 2026-04-03T20:05:00
Processing 5 active video feeds
```

### Debug: scheduler not firing

| Symptom | Check |
|---|---|
| No logs at all | Container App has 0 replicas — `minReplicas` must be `1` |
| `Failed to fetch active feeds from Synapse` | Synapse pool is paused or credentials wrong — see Stage 4 |
| `No active video feeds configured` | Synapse feeds table is empty — re-run `database/schema.sql` |
| Timer is past due warning | Container restarted mid-interval — harmless, will self-correct |

### Verify feeds in Synapse

```sql
SELECT feed_id, feed_name, feed_url, is_active
FROM traffic.video_feeds
ORDER BY feed_id;
```

Expected: 5 rows (Piccadilly Circus, Oxford Street, Hyde Park Corner, Westminster Bridge, Tower Bridge Approach) with `is_active = 1`.

---

## Stage 2 — Frame Capture (VideoCapture → Blob Storage)

**What happens:** For each feed, `VideoCapture.capture_frames()` downloads the TfL JamCam MP4 clip from S3, extracts `FRAMES_PER_INTERVAL` frames (default: 5) spread evenly across the clip, resizes to max 1280×720, encodes as JPEG, and uploads to the `video-frames` blob container. If the MP4 download fails, it falls back to the companion JPEG still.

Blob path pattern:
```
video-frames/feed_{id}/YYYY/MM/DD/HH/YYYYMMDD_HHMM_frame{NN}.jpg
```

### Verify frames are landing in Blob Storage

```bash
# List recent frames for feed 1
az storage blob list \
  --account-name <storage-account-name> \
  --container-name video-frames \
  --prefix "feed_1/" \
  --query "[].{name:name, size:properties.contentLength, modified:properties.lastModified}" \
  --output table \
  --auth-mode login | tail -20
```

**Expected:** JPEG blobs appearing every 5 minutes, each 50–300 KB.

### Debug: no frames captured

| Symptom | Likely cause |
|---|---|
| `tfl: download FAILED` | TfL S3 bucket unreachable — check egress from Container App |
| `OpenCV could not open downloaded MP4` | Corrupt or empty MP4 from TfL — JPEG fallback will be attempted |
| `No frames obtained from TfL feed` | Both MP4 and JPEG fallback failed — TfL camera may be offline |
| `Failed to upload frame to blob storage` | Storage connection string wrong or `video-frames` container missing |

```bash
# Check the capture-status.log blob for detailed TfL download diagnostics
az storage blob download \
  --account-name <storage-account-name> \
  --container-name video-frames \
  --name capture-status.log \
  --file /dev/stdout \
  --auth-mode login

# Confirm the video-frames container exists
az storage container show \
  --name video-frames \
  --account-name <storage-account-name> \
  --auth-mode login

# Check the Functions container env var for storage
az containerapp show \
  --name <func-container-app-name> \
  --resource-group <resource-group> \
  --query "properties.template.containers[0].env[?name=='STORAGE_ACCOUNT_NAME']" -o json
```

---

## Stage 3 — VLM Analysis (Azure OpenAI)

**What happens:** `VLMAnalyzer.analyze_frame()` base64-encodes each JPEG and sends it to `gpt-5.3-chat` with a structured system prompt. The model returns a JSON object with a `persons` array (one entry per detected pedestrian) plus scene metadata. Retries up to 3× with exponential backoff on failure.

### Verify OpenAI connectivity

```bash
# Check the deployment exists
az cognitiveservices account deployment show \
  --resource-group <resource-group> \
  --name <openai-account-name> \
  --deployment-name gpt-5.3-chat \
  --query "{name:name, model:properties.model, status:properties.provisioningState}" \
  -o json

# Quick smoke test — call the API directly
curl -s -X POST \
  "https://<openai-account-name>.openai.azure.com/openai/deployments/gpt-5.3-chat/chat/completions?api-version=2025-01-01-preview" \
  -H "api-key: $(az keyvault secret show --vault-name <kv-name> --name openai-api-key --query value -o tsv)" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"ping"}],"max_tokens":5}' \
  | jq '.choices[0].message.content'
```

**Expected:** `"pong"` or similar short response with HTTP 200.

### Trigger an on-demand analysis (bypass the timer)

The `analyze_feed` HTTP function lets you test the full VLM pipeline without waiting for the timer:

```bash
# Get the Functions Container App ingress URL
FUNC_URL=$(az containerapp show \
  --name <func-container-app-name> \
  --resource-group <resource-group> \
  --query "properties.configuration.ingress.fqdn" -o tsv)

# POST to analyze feed 1 (Piccadilly Circus) with 2 frames
curl -s -X POST "https://${FUNC_URL}/api/analyze_feed" \
  -H "Content-Type: application/json" \
  -d '{
    "feed_id": 1,
    "feed_url": "https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/00001.07450.mp4",
    "feed_name": "Piccadilly Circus",
    "num_frames": 2
  }' | jq .
```

**Expected response shape:**
```json
{
  "job_id": "...",
  "status": "success",
  "frames_captured": 2,
  "persons_detected": 14,
  "tokens_used": 2840,
  "duration_seconds": 18.4,
  "aggregate": {
    "total_count": 14,
    "pct_male": 57.14,
    "pct_female": 42.86,
    ...
  }
}
```

### Debug: VLM failures

| Symptom | Likely cause |
|---|---|
| HTTP 401 from OpenAI | `AZURE_OPENAI_API_KEY` secret wrong or Key Vault reference not resolving |
| HTTP 404 `DeploymentNotFound` | Model deployment `gpt-5.3-chat` doesn't exist — run `postprovision.sh` or deploy manually |
| HTTP 429 `RateLimitExceeded` | TPM quota exhausted — reduce `FRAMES_PER_INTERVAL` or request quota increase |
| `JSON parse error` | Model returned malformed JSON — rare, retried automatically up to 3× |
| `status: "failed"`, `error: "No frames could be captured"` | Frame capture failed before VLM was called — debug Stage 2 first |

```bash
# Check token usage in the Functions container logs
az containerapp logs show \
  --name <func-container-app-name> \
  --resource-group <resource-group> \
  --tail 100 \
  --query "[?contains(Log,'VLM analysis complete')]"
```

---

## Stage 4 — Data Storage (Azure Synapse)

**What happens:** After VLM analysis, `SynapseClient` writes three things:
1. **`traffic.raw_observations`** — one row per detected person per frame
2. **`traffic.interval_aggregates`** — one upserted row per feed per 5-minute bucket (DELETE + INSERT pattern)
3. **`traffic.analysis_jobs`** — one audit row per job with status, duration, token count

### Verify Synapse pool is online

```bash
az synapse sql pool show \
  --workspace-name <synapse-workspace-name> \
  --name foottrafficdw \
  --resource-group <resource-group> \
  --query "{status:status, sku:sku.name}" -o json
```

**Expected:** `"status": "Online"`. If `"Paused"`, resume it:

```bash
az synapse sql pool resume \
  --workspace-name <synapse-workspace-name> \
  --name foottrafficdw \
  --resource-group <resource-group>
```

### Verify data is flowing into Synapse

Connect via `sqlcmd` (or the Synapse Studio query editor):

```bash
# Get password from Key Vault
SYNAPSE_PWD=$(az keyvault secret show \
  --vault-name <kv-name> \
  --name synapse-sql-password \
  --query value -o tsv)

sqlcmd \
  -S <synapse-workspace-name>.sql.azuresynapse.net \
  -d foottrafficdw \
  -U sqladmin \
  -P "$SYNAPSE_PWD" \
  -I -C \
  -Q "SELECT TOP 5 feed_id, interval_start, total_count, processing_status FROM traffic.interval_aggregates ORDER BY interval_start DESC;"
```

**Expected:** Rows with `processing_status = 'complete'` and `total_count > 0`, timestamps within the last 10 minutes.

### Key diagnostic queries

```sql
-- How many intervals have been recorded per feed?
SELECT feed_id, COUNT(*) AS intervals, SUM(total_count) AS total_persons
FROM traffic.interval_aggregates
WHERE processing_status = 'complete'
GROUP BY feed_id;

-- Recent job success/failure rate
SELECT status, COUNT(*) AS cnt, AVG(duration_seconds) AS avg_duration_s
FROM traffic.analysis_jobs
WHERE started_at >= DATEADD(HOUR, -1, GETUTCDATE())
GROUP BY status;

-- Last 10 failed jobs with error messages
SELECT TOP 10 job_id, feed_id, interval_start, error_message, started_at
FROM traffic.analysis_jobs
WHERE status = 'failed'
ORDER BY started_at DESC;

-- Check raw observations are being written
SELECT TOP 5 feed_id, captured_at, gender, age_group, confidence_score
FROM traffic.raw_observations
ORDER BY captured_at DESC;
```

### Debug: Synapse write failures

| Symptom | Likely cause |
|---|---|
| `pyodbc.OperationalError: Login failed` | `SYNAPSE_PASSWORD` secret wrong — retrieve from Key Vault and compare |
| `pyodbc.OperationalError: TCP Provider` | Synapse pool is paused or firewall blocking the Container App's egress IP |
| `Schema 'traffic' not found` | Schema init didn't run — execute `database/schema.sql` manually |
| Jobs logged as `failed` with `error_message` | Check the message — usually a VLM or frame capture error upstream |

```bash
# Verify the Synapse env vars are set on the Functions container
az containerapp show \
  --name <func-container-app-name> \
  --resource-group <resource-group> \
  --query "properties.template.containers[0].env[?starts_with(name,'SYNAPSE')]" -o json
```

---

## Stage 5 — Streamlit Dashboard

**What happens:** The Streamlit app (`streamlit_app/db.py`) connects to Synapse using the same ODBC connection string and queries `traffic.interval_aggregates`, `traffic.analysis_jobs`, and `traffic.video_feeds` to power the three pages.

### Verify the dashboard is reachable

```bash
# Get the dashboard URL
az containerapp show \
  --name <streamlit-container-app-name> \
  --resource-group <resource-group> \
  --query "properties.configuration.ingress.fqdn" -o tsv

# Health check
curl -s -o /dev/null -w "%{http_code}" \
  "https://<dashboard-fqdn>/_stcore/health"
```

**Expected:** `200`

### Verify data appears in the UI

1. Open the dashboard URL in a browser
2. Navigate to **⚙️ Monitor** — you should see recent jobs in the job table
3. Navigate to **📊 Analytics** — charts should render with data from the last 24 hours
4. If the Monitor page shows `"No analysis jobs found"`, the scheduler hasn't run yet or Synapse writes are failing (debug Stage 4)

### Debug: dashboard shows no data

```bash
# Stream Streamlit container logs
az containerapp logs show \
  --name <streamlit-container-app-name> \
  --resource-group <resource-group> \
  --follow --tail 30
```

| Symptom | Likely cause |
|---|---|
| `Login failed for user 'sqladmin'` in logs | Streamlit container has wrong `SYNAPSE_PASSWORD` secret |
| Charts render but show 0 counts | `processing_status` filter — check aggregates have `'complete'` status |
| Dashboard loads but AI Query page errors | `AZURE_OPENAI_API_KEY` or `AZURE_OPENAI_DEPLOYMENT` wrong on Streamlit container |
| HTTP 502 / app won't load | Container is still starting — wait 60s and retry; check replica count |

---

## Stage 6 — AI Query (Natural Language → SQL)

**What happens:** The **🔍 Query** page sends the user's question to `ai_query.py`, which calls `gpt-5.3-chat` with a schema-aware system prompt to generate a Synapse SQL query, executes it, and returns results.

### Verify AI Query end-to-end

In the dashboard, navigate to **🔍 Query** and type:

> *"How many people were detected in the last hour?"*

**Expected:** A SQL query is shown, followed by a result table with pedestrian counts.

### Debug: AI Query failures

| Symptom | Likely cause |
|---|---|
| `Error generating SQL` | OpenAI API key wrong on Streamlit container |
| SQL generated but query fails | Model generated invalid Synapse SQL — check the displayed SQL for syntax errors |
| Empty results | No data in `interval_aggregates` yet — wait for the scheduler to run |

---

## End-to-End Health Check (Quick Reference)

Run these in order after a fresh deploy or when something looks wrong:

```bash
# 1. Synapse pool online?
az synapse sql pool show --workspace-name <ws> --name foottrafficdw --resource-group <rg> --query status -o tsv

# 2. Functions container running?
az containerapp show --name <func-ca> --resource-group <rg> --query "properties.template.scale.minReplicas" -o tsv

# 3. OpenAI deployment exists?
az cognitiveservices account deployment show --resource-group <rg> --name <oai-account> --deployment-name gpt-5.3-chat --query properties.provisioningState -o tsv

# 4. Frames landing in blob storage? (should show recent blobs)
az storage blob list --account-name <storage> --container-name video-frames --prefix "feed_1/" --auth-mode login --query "[-3:].{name:name,modified:properties.lastModified}" -o table

# 5. Jobs succeeding in Synapse?
sqlcmd -S <ws>.sql.azuresynapse.net -d foottrafficdw -U sqladmin -P "<pwd>" -I -C \
  -Q "SELECT TOP 5 status, feed_id, interval_start, persons_detected FROM traffic.analysis_jobs ORDER BY started_at DESC;"

# 6. Dashboard healthy?
curl -s -o /dev/null -w "%{http_code}" "https://<dashboard-fqdn>/_stcore/health"
```

All six checks passing = data is flowing end-to-end. ✅

---

## Common Cross-Cutting Issues

### OpenAI client / proxy errors at startup

The `AzureOpenAI` client (via `httpx`) performs proxy detection when it is first constructed. In some Azure Container App environments this causes errors even before any API call is made. To avoid this, **all three OpenAI clients are initialised lazily** — the `AzureOpenAI(...)` object is only created on the first actual API call, not at import or class construction time.

| File | Class | Lazy method |
|---|---|---|
| `functions/shared/vlm_analyzer.py` | `VLMAnalyzer` | `_get_client()` |
| `streamlit_app/ai_query.py` | `AIQueryEngine` | `_get_client()` |

Additionally, `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY` are **optional** in `Settings` (they default to `""` instead of raising `KeyError`). This means `get_settings()` — and therefore `VideoCapture` — can be instantiated without OpenAI credentials, so **frame capture works independently of OpenAI availability**.

If you see `RuntimeError: AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set`, the secrets are missing from the Container App environment — see the section below.

### Secret / env var not resolving

All secrets are stored in Key Vault and injected as Container App secrets at provision time. If a secret changes (e.g. OpenAI key rotated), update the Container App secret and restart the revision:

```bash
# Update a secret value
az containerapp secret set \
  --name <container-app-name> \
  --resource-group <resource-group> \
  --secrets "openai-api-key=<new-value>"

# Restart to pick up the new secret
az containerapp revision restart \
  --name <container-app-name> \
  --resource-group <resource-group> \
  --revision <revision-name>
```

### Schema not initialized

If `traffic` schema or tables are missing, re-run the schema script:

```bash
sqlcmd \
  -S <synapse-workspace>.sql.azuresynapse.net \
  -d foottrafficdw \
  -U sqladmin \
  -P "<password-from-keyvault>" \
  -i database/schema.sql \
  -I -C
```

### Checking container logs for errors

```bash
# Stream recent errors from the Functions container
az containerapp logs show \
  --name <func-container-app-name> \
  --resource-group <resource-group> \
  --tail 50

# Stream recent errors from the Streamlit container
az containerapp logs show \
  --name <streamlit-container-app-name> \
  --resource-group <resource-group> \
  --tail 50
```
