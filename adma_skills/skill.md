---
name: adma
description: ADMA (Agricultural Data Management & Analytics) platform skill. Manage agricultural files, folders, maps, weather station data, run GIS analysis tools, visualize crop and weather data via REST API and the adma CLI.
metadata:
  version: 1.0.0
  author: ADMA Team
  platform: https://adma.unl.edu
  openclaw:
    emoji: "\U0001F33E"
    requires:
      bins:
        - curl
        - jq
        - python3
  tags:
    - agriculture
    - gis
    - weather
    - data-management
    - visualization
---

# ADMA Platform Skill for Claude

This document teaches Claude how to interact with the ADMA (Agricultural Data Management & Analytics) platform. Use this as a system prompt, CLAUDE.md, or reference document when working with ADMA.

---

## Quick Start with ADMA CLI

An `adma` CLI tool is provided (`adma-cli.sh`). Install it:

```bash
cp adma-cli.sh /usr/local/bin/adma && chmod +x /usr/local/bin/adma
```

Set up credentials (choose one):

```bash
# Option 1: Interactive login
adma login

# Option 2: Environment variables
export ADMA_TOKEN="your-api-token"
export ADMA_URL="https://adma.unl.edu"

# Option 3: Config file
mkdir -p ~/.adma
echo '{"token":"your-token","url":"https://adma.unl.edu"}' > ~/.adma/config.json
```

Then use:
```bash
adma files                              # List all files
adma folders                            # List all folders
adma subfolders <folder_id>             # List subfolders
adma files-in-folder <folder_id>        # Files in a folder
adma file-download <file_id> ./out.csv  # Download a file
adma file-upload ./data.csv <folder_id> # Upload a file
adma search "corn yield"                # Search everything
adma tree                               # Show full folder tree
adma tools                              # List analysis tools
adma tool-run seeding-tool '{"file_id":"...","output_folder_id":"..."}'
adma tool-wait seeding-tool <task_id>   # Wait for tool to finish
adma whoami                             # User profile
adma stats                              # Storage statistics
adma help                               # Full command reference
```

The CLI handles authentication automatically — no need to manage tokens in your commands.

---

## What is ADMA?

ADMA is a Django-based web platform for managing agricultural data. It provides:

- **File & Folder Management** — upload, organize, download agricultural data files
- **GIS/Spatial Data** — shapefiles, GeoJSON, GeoTIFF with automatic GeoServer publishing
- **Maps** — composite map visualizations combining multiple spatial layers
- **Analysis Tools** — seeding analysis, shapefile conversion, stress index calculation, yield summary with ANOVA
- **Weather Station Data** — Realm5 weather sensor data (temperature, humidity, wind, pressure)
- **John Deere Integration** — field boundaries, operations data
- **Search** — full-text search across files, folders, and maps

**Tech Stack:** Django 4.2, PostgreSQL + PostGIS, GeoServer, Celery + Redis, Docker

**Production URL:** https://adma.unl.edu
**Local Dev:** http://localhost:8001

---

## Authentication

ADMA uses token-based authentication for its REST API.

### Get a Token
```bash
curl -X POST https://adma.unl.edu/api/v1/auth/token/ \
  -H "Content-Type: application/json" \
  -d '{"username": "YOUR_USERNAME", "password": "YOUR_PASSWORD"}'
```
Response: `{"token": "abc123...", "user_id": 1, "username": "your_user"}`

### Use the Token
All API calls require: `-H "Authorization: Token YOUR_TOKEN"`

### Quick Setup
```bash
export ADMA_URL="https://adma.unl.edu"
export ADMA_TOKEN="your-token-here"
alias adma-api='curl -s -H "Authorization: Token $ADMA_TOKEN"'
```

---

## Complete REST API Reference

Base URL: `https://adma.unl.edu/api/v1/`

### Files

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `files/` | List user's own files |
| GET | `files/?include_public=true` | Include public files from all users |
| GET | `files/?folder_id=UUID` | Files in a specific folder |
| GET | `files/?folder_id=UUID&include_public=true` | Files in folder including public |
| GET | `files/?file_type=TYPE` | Filter by type: `image`, `text`, `csv`, `spreadsheet`, `document`, `gis`, `other` |
| GET | `files/UUID/metadata/` | Detailed file metadata |
| GET | `files/UUID/download/` | Download file content |
| POST | `files/upload/` | Upload files (multipart form) |
| PATCH | `files/UUID/update/` | Update name/visibility |
| DELETE | `files/UUID/delete/` | Delete file |

**List response:**
```json
{
  "files": [
    {
      "id": "86ca9ce8-7ffe-4627-9051-a13ca63cf826",
      "name": "yield_2025.csv",
      "file_type": "csv",
      "file_size": 45230,
      "is_public": false,
      "is_spatial": false,
      "gis_status": "pending",
      "created_at": "2025-10-30T13:02:40.396507Z",
      "updated_at": "2025-10-30T13:02:41.093338Z"
    }
  ],
  "count": 1
}
```

**Metadata response (`files/UUID/metadata/`):**
```json
{
  "id": "86ca9ce8-...",
  "name": "yield_2025.csv",
  "file_type": "csv",
  "mime_type": "text/csv",
  "file_size": 45230,
  "is_public": false,
  "is_spatial": false,
  "gis_status": "pending",
  "geoserver_layer_name": null,
  "crs": null,
  "owner": "yu.pan@unl.edu",
  "folder_id": "c72ee8fc-be41-4a34-aba0-225e21c3b46b",
  "folder_path": "green/analysis",
  "created_at": "2025-10-30T13:02:40Z",
  "updated_at": "2025-10-30T13:02:41Z"
}
```

**Upload:**
```bash
curl -H "Authorization: Token $ADMA_TOKEN" \
  -F "files=@/path/to/data.csv" \
  -F "folder_id=FOLDER_UUID" \
  -F "is_public=false" \
  "$ADMA_URL/api/v1/files/upload/"
```

**Update:**
```bash
curl -X PATCH -H "Authorization: Token $ADMA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "renamed.csv", "is_public": true}' \
  "$ADMA_URL/api/v1/files/UUID/update/"
```

### Folders

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `folders/` | List user's root folders |
| GET | `folders/?include_public=true` | Include public folders |
| GET | `folders/?parent_id=UUID&include_public=true` | List subfolders |
| POST | `folders/create/` | Create a new folder |
| GET | `folders/UUID/info/` | Folder details (file count, total size) |
| GET | `folders/UUID/download/` | Download as ZIP archive |
| PATCH | `folders/UUID/update/` | Update name/visibility |
| DELETE | `folders/UUID/delete/` | Delete folder and contents |

**List response:**
```json
{
  "folders": [
    {
      "id": "abc123-def456-...",
      "name": "realm5",
      "is_public": true,
      "created_at": "2025-01-27T18:41:24Z",
      "updated_at": "2025-01-31T21:26:14Z"
    }
  ],
  "count": 1
}
```

**Create:**
```bash
curl -X POST -H "Authorization: Token $ADMA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "My Analysis", "parent_id": null, "is_public": false}' \
  "$ADMA_URL/api/v1/folders/create/"
```

**Folder info response:**
```json
{
  "folder_id": "abc123-...",
  "folder_name": "realm5",
  "zip_filename": "realm5.zip",
  "file_count": 1747,
  "total_size": 23456789,
  "total_size_display": "22.4 MB",
  "is_public": true,
  "download_url": "/api/v1/folders/abc123/download/"
}
```

### Maps

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `maps/` | List user's maps |
| GET | `maps/?include_public=true` | Include public maps |
| POST | `maps/create/` | Create a new map |
| PATCH | `maps/UUID/update/` | Update map |
| DELETE | `maps/UUID/delete/` | Delete map |

**Create:**
```bash
curl -X POST -H "Authorization: Token $ADMA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Field Analysis 2025", "description": "Spring planting analysis", "is_public": false}' \
  "$ADMA_URL/api/v1/maps/create/"
```

### Analysis Tools

ADMA provides four built-in agricultural analysis tools. They run asynchronously via Celery.

| Tool Slug | Name | Purpose |
|-----------|------|---------|
| `seeding-tool` | Seeding Tool | Process point shapefiles → seeding polygons, boundaries, summary statistics |
| `shape-to-json` | Shape to JSON | Convert ESRI Shapefiles (.shp) → GeoJSON format |
| `si-tool` | SI Tool | Calculate Stress Index from NDRE imagery + buffer sector data |
| `yield-summary` | Yield Summary | Treatment analysis with ANOVA, NUE, PFP, MNR economic metrics |

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `tools/` | List available tools with descriptions and configs |
| POST | `tools/SLUG/run/` | Start a tool (returns task_id) |
| GET | `tools/SLUG/status/TASK_ID/` | Check task status |

**Tool parameters:**

Seeding Tool:
```json
{"file_id": "UUID_OF_SHAPEFILE", "output_folder_id": "UUID_OF_OUTPUT_FOLDER"}
```

Shape to JSON:
```json
{"file_id": "UUID_OF_SHAPEFILE", "output_folder_id": "UUID_OF_OUTPUT_FOLDER"}
```

SI Tool:
```json
{"buffer_sectors_shp": "UUID", "ndre_csv": "UUID"}
```

Yield Summary:
```json
{
  "treatment_file_id": "UUID",
  "yield_file_id": "UUID",
  "total_n_values": "50,100,150,200",
  "output_dir_id": "UUID",
  "buffer_distance": -30.0,
  "corn_price": 4.35,
  "n_price": 0.50
}
```

**Running a tool:**
```bash
# Start
RESULT=$(curl -X POST -H "Authorization: Token $ADMA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"file_id":"UUID","output_folder_id":"UUID"}' \
  "$ADMA_URL/api/v1/tools/seeding-tool/run/")
TASK_ID=$(echo "$RESULT" | jq -r '.task_id')

# Poll until done
while true; do
  STATUS=$(curl -s -H "Authorization: Token $ADMA_TOKEN" \
    "$ADMA_URL/api/v1/tools/seeding-tool/status/$TASK_ID/")
  S=$(echo "$STATUS" | jq -r '.status')
  echo "Status: $S"
  [ "$S" = "SUCCESS" ] || [ "$S" = "FAILURE" ] && break
  sleep 5
done

# Check result
echo "$STATUS" | jq '.result'
```

**Status response:**
```json
{"status": "SUCCESS", "result": {"success": true, "output_files": [{"name": "seeded.shp", "id": "uuid"}]}}
```

### User & Search

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `user/profile/` | User info: id, username, email, name, join date |
| GET | `user/stats/` | Storage stats: file/folder/map counts, total bytes |
| GET | `search/?q=KEYWORD` | Search all (files, folders, maps) |
| GET | `search/?q=KEYWORD&type=file` | Search files only |
| GET | `search/?q=KEYWORD&type=folder` | Search folders only |
| GET | `search/?q=KEYWORD&type=map` | Search maps only |

**Profile response:**
```json
{
  "id": 2,
  "username": "yu.pan@unl.edu",
  "email": "yu.pan@unl.edu",
  "first_name": "Yu",
  "last_name": "Pan",
  "date_joined": "2025-09-26T18:42:52Z"
}
```

**Stats response:**
```json
{
  "file_count": 298,
  "folder_count": 21,
  "map_count": 2,
  "total_storage_bytes": 477081346,
  "total_storage_display": "455.0 MB",
  "public_files": 31,
  "spatial_files": 114
}
```

**Search response:**
```json
{
  "files": [{"id": "...", "name": "corn_yield.csv", ...}],
  "folders": [{"id": "...", "name": "2025 Data", ...}],
  "maps": [{"id": "...", "name": "Field Map", ...}]
}
```

---

## ADMA Web URLs

| Resource | URL |
|----------|-----|
| Home | `https://adma.unl.edu/` |
| Dashboard | `https://adma.unl.edu/dashboard/` |
| File detail | `https://adma.unl.edu/file/{uuid}/` |
| File map viewer | `https://adma.unl.edu/file/{uuid}/map/` |
| Folder contents | `https://adma.unl.edu/folder/{uuid}/` |
| Maps list | `https://adma.unl.edu/maps/` |
| Map detail | `https://adma.unl.edu/maps/{uuid}/` |
| Tools page | `https://adma.unl.edu/tools/` |
| Seeding Tool | `https://adma.unl.edu/tools/seeding/` |
| Shape to JSON | `https://adma.unl.edu/tools/shape-to-json/` |
| SI Tool | `https://adma.unl.edu/tools/si/` |
| Yield Summary | `https://adma.unl.edu/tools/yield-summary/` |
| Search | `https://adma.unl.edu/search/?q={query}` |
| Public file | `https://adma.unl.edu/public/file/{uuid}/` |
| Public folder | `https://adma.unl.edu/public/folder/{uuid}/` |
| API docs | `https://adma.unl.edu/api/v1/` |

---

## Data Formats on ADMA

### Supported File Types

| Category | Extensions | ADMA file_type |
|----------|-----------|----------------|
| GIS/Spatial | `.geojson`, `.shp`, `.tiff`, `.tif`, `.gpkg`, `.kml`, `.kmz` | `gis` |
| Tabular | `.csv` | `csv` |
| Spreadsheet | `.xlsx`, `.xls` | `spreadsheet` |
| Documents | `.pdf`, `.doc`, `.docx`, `.ppt`, `.pptx` | `document` |
| Images | `.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.webp` | `image` |
| Text/Code | `.txt`, `.md`, `.py`, `.js`, `.html`, `.css`, `.json`, `.xml` | `text` |

### GIS Processing Status

When a spatial file is uploaded, ADMA processes it through GeoServer:
- `pending` — waiting in queue
- `processing` — being processed
- `published` — available in the map viewer
- `error` — processing failed (check `processing_log`)

### Weather Station Data (Realm5)

ADMA integrates with Realm5 weather stations. Data is stored as daily JSON files.

**Folder structure:**
```
realm5/                              (public)
├── Cow/Calf Weather Station/        (device: 01901142, ~465 files)
├── Entomology Weather Station/      (device: 01901145, ~469 files)
└── Farm Shop Weatherstation/        (device: 019004F8, ~813 files)
```

**File naming:** `{device_eui}_{YYYY-MM-DD}.json`

**JSON structure:**
```json
{
  "dev_eui": "019004F8",
  "dev_eui_numeric": "26215672",
  "device_name": "Farm Shop Weatherstation",
  "device_type": "weather_station",
  "date": "2024-01-01",
  "observation_count": 96,
  "observations": [
    {
      "timestamp": "2024-01-01T06:00:00.000+0000",
      "temperature": -8.27,
      "humidity": 96.93,
      "wind_speed": 2.75,
      "wind_gust": 5.07,
      "wind_direction": 137.88,
      "calculated_pressure": 1038.59
    }
  ]
}
```

**Observation fields:**
| Field | Unit | Description |
|-------|------|-------------|
| `timestamp` | ISO 8601 | Observation time (every 15 minutes) |
| `temperature` | °C | Air temperature |
| `humidity` | % | Relative humidity (0-100) |
| `wind_speed` | m/s | Wind speed |
| `wind_gust` | m/s | Wind gust speed |
| `wind_direction` | degrees | Wind direction (0-360, 0=North) |
| `calculated_pressure` | hPa | Barometric pressure |

### John Deere Data

ADMA syncs with John Deere Operations Center:
```
John Deere/                          (public)
├── Field Boundaries/                (GIS boundary files per field)
├── Operations/                      (planting, spraying, harvest records)
└── ...
```

---

## Database Models

### Folder
```
id: UUID (primary key)
name: string
parent: FK to Folder (nullable — null means root)
owner: FK to User
is_public: boolean
is_third_party: boolean
third_party_source: string (e.g., "John Deere", "Realm5")
created_at, updated_at: datetime
```
Unique constraint: `(name, parent, owner)`

### File
```
id: UUID (primary key)
name: string
file: FileField (stored at uploads/{folder_path}/{filename})
folder: FK to Folder (nullable)
owner: FK to User
file_size: bigint (bytes)
file_type: string (image|text|csv|spreadsheet|document|gis|other)
mime_type: string
is_public: boolean
is_spatial: boolean
geoserver_layer_name: string (nullable)
geoserver_workspace: string (default: "adma_geo")
crs: string (coordinate reference system, nullable)
gis_status: string (pending|processing|processed|published|error)
created_at, updated_at: datetime
```
Unique constraint: `(name, folder, owner)`

### Map
```
id: UUID (primary key)
name: string
description: text
owner: FK to User
is_public: boolean
geoserver_layer_group_name: string (unique)
center_lat, center_lng: float
zoom_level: int
bbox_min_lat, bbox_max_lat, bbox_min_lng, bbox_max_lng: float
created_at, updated_at: datetime
```

### MapLayer
```
id: UUID
map: FK to Map
file: FK to File
layer_order: int
opacity: float (0.0-1.0)
is_visible: boolean
style_name: string (nullable)
```

### Tool
```
id: UUID
name: string
slug: string (unique, URL-friendly)
description: text
category: string (gis_processing|format_conversion|analysis|visualization|data_management|other)
status: string (available|coming_soon|beta|deprecated|maintenance)
celery_task_name: string
input_config: JSON
output_config: JSON
usage_count: int
```

---

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌────────────────┐
│   Browser   │────▶│  Nginx :80   │────▶│  Django :8000  │
└─────────────┘     └──────────────┘     └───────┬────────┘
                                                  │
                    ┌──────────────┐     ┌────────▼────────┐
                    │ GeoServer    │     │  PostgreSQL     │
                    │   :8080      │     │  + PostGIS      │
                    └──────────────┘     └─────────────────┘
                                                  │
                    ┌──────────────┐     ┌────────▼────────┐
                    │  Redis       │────▶│  Celery Worker  │
                    └──────────────┘     └─────────────────┘
```

- **Django** — REST API + web frontend (Bootstrap 5, server-side templates)
- **PostgreSQL + PostGIS** — database with spatial extensions
- **GeoServer** — WMS/WFS spatial data publishing
- **Celery + Redis** — async task processing (file uploads, GIS processing, tool execution)
- **Nginx** — reverse proxy, static files, SSL

---

## Common Workflows for Claude

### 1. Explore a User's Data
```bash
# Get user stats
curl -s -H "Authorization: Token $TOKEN" "$BASE_URL/user/stats/"

# List root folders
curl -s -H "Authorization: Token $TOKEN" "$BASE_URL/folders/?include_public=true" | jq '.folders[] | {id, name, is_public}'

# Browse into a folder
curl -s -H "Authorization: Token $TOKEN" "$BASE_URL/folders/?parent_id=FOLDER_ID&include_public=true"
curl -s -H "Authorization: Token $TOKEN" "$BASE_URL/files/?folder_id=FOLDER_ID&include_public=true"
```

### 2. Download, Process, Upload
```bash
# Download
curl -s -H "Authorization: Token $TOKEN" -o data.csv "$BASE_URL/files/UUID/download/"

# Process with Python
python3 -c "
import pandas as pd
df = pd.read_csv('data.csv')
summary = df.describe()
summary.to_csv('summary.csv')
print(summary)
"

# Upload result
curl -s -H "Authorization: Token $TOKEN" -F "files=@summary.csv" -F "folder_id=UUID" "$BASE_URL/files/upload/"
```

### 3. Weather Data Analysis
```bash
# Find realm5 folder
REALM5=$(curl -s -H "Authorization: Token $TOKEN" "$BASE_URL/search/?q=realm5&type=folder" | jq -r '.folders[0].id')

# Get station folders
curl -s -H "Authorization: Token $TOKEN" "$BASE_URL/folders/?parent_id=$REALM5&include_public=true" | jq '.folders[] | {id, name}'

# Download a day's data
FILE_ID=$(curl -s -H "Authorization: Token $TOKEN" "$BASE_URL/files/?folder_id=STATION_ID&include_public=true" | jq -r '.files[0].id')
curl -s -H "Authorization: Token $TOKEN" -o weather.json "$BASE_URL/files/$FILE_ID/download/"

# Parse
python3 -c "
import json
with open('weather.json') as f:
    data = json.load(f)
for obs in data['observations'][:3]:
    print(f\"{obs['timestamp']}: {obs['temperature']}°C, {obs['humidity']}% humidity\")
"
```

### 4. Run Analysis Tool
```bash
# Find shapefile
SHP=$(curl -s -H "Authorization: Token $TOKEN" "$BASE_URL/files/?file_type=gis&include_public=true" | jq -r '[.files[] | select(.name | endswith(".shp"))][0].id')

# Create output folder
OUT=$(curl -s -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" -d '{"name":"Results"}' "$BASE_URL/folders/create/" | jq -r '.id')

# Run tool
TASK=$(curl -s -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" -d "{\"file_id\":\"$SHP\",\"output_folder_id\":\"$OUT\"}" "$BASE_URL/tools/seeding-tool/run/" | jq -r '.task_id')

# Wait
while [ "$(curl -s -H "Authorization: Token $TOKEN" "$BASE_URL/tools/seeding-tool/status/$TASK/" | jq -r '.status')" != "SUCCESS" ]; do sleep 5; done
```

### 5. Batch Operations
```bash
# Download all CSV files
curl -s -H "Authorization: Token $TOKEN" "$BASE_URL/files/?file_type=csv&include_public=true" | \
  jq -r '.files[] | "\(.id)\t\(.name)"' | \
  while IFS=$'\t' read -r id name; do
    curl -s -H "Authorization: Token $TOKEN" -o "$name" "$BASE_URL/files/$id/download/"
    echo "Downloaded: $name"
  done
```

---

## Tips for Claude

1. **Always use `?include_public=true`** — ADMA has shared public data that users expect to see.
2. **Files are identified by UUID** — always get the UUID from an API list/search response first, never guess.
3. **GIS files need processing time** — after upload, spatial files go through `pending` → `processing` → `published`. The map viewer only works after `published`.
4. **Weather data is daily JSON** — each file is one day, ~96 observations (every 15 min). File name format: `{device_id}_{YYYY-MM-DD}.json`.
5. **Tools are async** — start with POST, get `task_id`, poll status until `SUCCESS` or `FAILURE`.
6. **Large folders** — realm5 has 1700+ files. When downloading many files, consider pagination or filtering by date.
7. **Unique constraints** — file names must be unique within `(name, folder, owner)`. Folder names must be unique within `(name, parent, owner)`.
