# ADMA - Agricultural Data Management & Analytics

A comprehensive web-based platform for managing, visualizing, and analyzing agricultural geospatial data. Built on Django with GeoServer integration for advanced GIS capabilities.

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Third-Party Integrations](#third-party-integrations)
- [API Documentation](#api-documentation)
- [Development](#development)
- [Deployment](#deployment)

## Features

### File & Folder Management
- Hierarchical folder structure with unlimited nesting
- Support for multiple file types (GIS, documents, images, spreadsheets)
- Public/private visibility controls
- Drag-and-drop file uploads
- Folder and file renaming

### GIS Data Processing
- Automatic detection and processing of spatial files (Shapefiles, GeoTIFF, GeoJSON, KML)
- Integration with GeoServer for spatial data publishing
- Interactive map visualization using OpenLayers
- Multiple base map options (OpenStreetMap, Satellite, Terrain, Topographic)
- Support for both vector and raster data

### Custom Maps
- Create composite maps by combining multiple spatial layers
- Layer ordering and styling controls
- Map location tracking with navigation overview
- Public map sharing

### Analysis Tools
- **Seeding Tool**: Process agricultural seeding data with GIS outputs
- **Shape to JSON**: Convert shapefiles to GeoJSON format
- **SI Tool**: Sustainability index calculations

### Third-Party Integrations
- **John Deere Operations Center**: Sync field boundaries, operations data, and metadata
- **Realm5 Weather Stations**: Daily weather observations with data visualization

### User Features
- User registration and authentication
- Token-based API access
- Dashboard with file statistics
- Search functionality across files and folders

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Nginx (Reverse Proxy)                 │
└─────────────────────────────────────────────────────────────┘
                              │
         ┌────────────────────┼────────────────────┐
         │                    │                    │
         ▼                    ▼                    ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  Django App     │  │   GeoServer     │  │   Static Files  │
│  (Port 8000)    │  │   (Port 8080)   │  │                 │
└─────────────────┘  └─────────────────┘  └─────────────────┘
         │                    │
         │                    │
         ▼                    ▼
┌─────────────────────────────────────────────────────────────┐
│              PostgreSQL + PostGIS (Port 5432)               │
└─────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────┐  ┌─────────────────┐
│  Celery Worker  │◄─│     Redis       │
│  (Background)   │  │   (Broker)      │
└─────────────────┘  └─────────────────┘
```

### Services

| Service    | Description                                      | Port  |
|------------|--------------------------------------------------|-------|
| nginx      | Reverse proxy, SSL termination, static files     | 80/443|
| django     | Main application server                          | 8000  |
| geoserver  | Spatial data server (WMS/WFS)                    | 8080  |
| postgres   | Database with PostGIS extension                  | 5432  |
| redis      | Celery message broker and caching                | 6379  |
| celery     | Background task processing                       | -     |

## Quick Start

### Prerequisites

- Docker and Docker Compose
- Git

### Installation

1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd adma_geonode_project
   ```

2. Create environment file:
   ```bash
   cp adma_geo/.env.sample adma_geo/.env
   # Edit .env with your configuration
   ```

3. Build and start services:
   ```bash
   cd adma_geo
   docker-compose build
   docker-compose up -d
   ```

4. Run initial setup:
   ```bash
   docker-compose exec django python manage.py migrate
   docker-compose exec django python manage.py create_system_tools
   docker-compose exec django python manage.py createsuperuser
   ```

5. Access the application:
   - Main site: http://localhost/
   - GeoServer: http://localhost/geoserver/
   - Admin: http://localhost/admin/

## Third-Party Integrations

### John Deere Operations Center

Event-driven sync of field data, boundaries, and operations from John Deere via
the Data Subscription Service webhook.

**Setup:**
1. Register the application at [John Deere Developer Portal](https://developer.deere.com/).
2. Configure OAuth2 credentials and organization ID in `.env`:
   - `JD_CLIENT_ID`, `JD_CLIENT_SECRET`, `JD_REFRESH_TOKEN`, `JD_ORG_ID`
3. Configure webhook receiver settings:
   - `JD_WEBHOOK_CALLBACK_URL` — public HTTPS URL for your `/api/v1/webhooks/johndeere/` endpoint
   - `JD_WEBHOOK_USERNAME`, `JD_WEBHOOK_PASSWORD` — random credentials JD will use to authenticate its POSTs to us
4. Bootstrap the John Deere root folder:
   ```
   docker-compose exec django python manage.py setup_johndeere
   ```
5. Optional initial backfill (one-shot):
   ```
   docker-compose exec django python manage.py sync_johndeere --once
   ```
6. Create the JD subscription so events start flowing:
   ```
   docker-compose exec django python manage.py manage_johndeere_webhooks --create
   ```

**Sync model:** event-driven. JD POSTs every field / boundary / field-operation
change to our webhook; each event triggers a targeted refresh of that one
resource. No daily poll.

**Data Synced:**
- Field metadata and boundaries (as shapefiles)
- Field operations (as JSON; can be extended to richer artifacts)
- Soft-delete: JD archive/delete events mark local folders/files `is_archived=True`
  (recoverable via Django admin).

**Ops:**
- List or delete subscriptions: `python manage.py manage_johndeere_webhooks --list` / `--delete <sub_id>`
- Inspect events: Django admin → John Deere Webhook Events

### Realm5 Weather Stations

Syncs weather observation data from Realm5 IoT sensors.

**Setup:**
1. Obtain API key from Realm5
2. Add `REALM5_API_KEY` to `.env`

**Sync Schedule:** Daily at 2:00 AM

**Data Synced:**
- Daily weather observations (JSON files)
- Aggregated daily averages (`all.json`)
- Variables: temperature, humidity, wind speed, dew point, etc.

**Manual Sync:**
```bash
docker-compose exec django python manage.py shell -c "
from filemanager.tasks import sync_realm5_task
sync_realm5_task()
"
```

## API Documentation

ADMA provides RESTful APIs with token-based authentication.

### Authentication

```bash
# Get auth token
curl -X POST http://localhost/api/auth/token/ \
  -d "username=your_username&password=your_password"

# Use token in requests
curl -H "Authorization: Token YOUR_TOKEN" \
  http://localhost/api/files/
```

### Key Endpoints

| Endpoint                          | Method | Description                    |
|-----------------------------------|--------|--------------------------------|
| `/api/auth/token/`                | POST   | Get authentication token       |
| `/api/files/`                     | GET    | List user's files              |
| `/api/files/upload/`              | POST   | Upload a file                  |
| `/api/files/<id>/download/`       | GET    | Download a file                |
| `/api/folders/`                   | GET    | List user's folders            |
| `/api/folders/<id>/download/`     | GET    | Download folder as ZIP         |

See `API_DOCUMENTATION.md` for complete API reference.

## Development

### Running Locally

```bash
cd adma_geo
docker-compose up -d

# View logs
docker-compose logs -f django

# Django shell
docker-compose exec django python manage.py shell

# Run tests
docker-compose exec django python manage.py test
```

### Code Structure

```
adma_geo/
├── adma_geo/           # Project settings
│   ├── settings.py
│   ├── urls.py
│   └── celery.py
├── filemanager/        # Main application
│   ├── models.py       # File, Folder, Map models
│   ├── views.py        # View functions
│   ├── tasks.py        # Celery tasks
│   ├── api_views.py    # REST API views
│   ├── realm5_client.py
│   └── johndeere_client.py
├── templates/          # HTML templates
├── static/             # Static assets
├── media/              # User uploads
└── docker-compose.yml
```

### Adding New Features

1. Create/modify models in `filemanager/models.py`
2. Run migrations: `docker-compose exec django python manage.py makemigrations && docker-compose exec django python manage.py migrate`
3. Add views in `filemanager/views.py`
4. Create templates in `templates/filemanager/`
5. Update URLs in `filemanager/urls.py`

## Deployment

### Production Setup

1. Configure SSL in Nginx

2. Build and deploy using the production compose file:
   ```bash
   cd adma_geo
   docker-compose -f docker-compose-adma.yml up -d --build
   ```

3. Run migrations and collect static files:
   ```bash
   docker-compose -f docker-compose-adma.yml exec django python manage.py migrate
   docker-compose -f docker-compose-adma.yml exec django python manage.py collectstatic --noinput
   docker-compose -f docker-compose-adma.yml exec django python manage.py create_system_tools
   ```

### Backup

```bash
# Database backup
docker-compose exec postgres pg_dump -U adma_user adma_db > backup.sql

# Media files backup
tar -czvf media_backup.tar.gz adma_geo/media/
```

### Monitoring

- Check service status: `docker-compose ps`
- View logs: `docker-compose logs -f <service>`
- Celery tasks: Check Django admin or Celery logs

## License

ADMA 2021-2026. All rights reserved.

## Support

For questions or issues, contact the development team.
