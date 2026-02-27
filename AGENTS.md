# AGENTS.md

## Cursor Cloud specific instructions

### Project overview

ADMA GeoNode Project — an Agricultural Data Management and Analytics platform built as a customized GeoNode instance. It is a Python/Django web application that runs entirely via Docker Compose with interconnected services (Django, Celery, PostgreSQL/PostGIS, GeoServer, Nginx, RabbitMQ).

### Prerequisites

Docker must be installed and running. In Cursor Cloud environments, Docker requires special configuration: `fuse-overlayfs` storage driver and `iptables-legacy` (see Dockerfile-in-Docker setup in environment snapshot).

### Environment setup

1. Generate `.env` from template: `python3 create-envfile.py --noinput --env_type dev --hostname localhost`
2. Build images: `docker compose build`
3. Start stack: `docker compose up -d`
4. Wait ~2-3 minutes for all services to become healthy (check with `docker ps`)

### Running the application

- Application is accessible at `http://localhost/` (via Nginx reverse proxy on port 80)
- Django admin: `http://localhost/admin/` (redirects to login)
- GeoServer: `http://localhost/geoserver/web/`
- Default admin credentials: `admin` / `admin` (set via `--geonodepwd` flag in create-envfile.py)
- GeoServer admin: `admin` / `geoserver`

### Key commands (all via Docker)

- **Lint**: `docker exec django4adma_geonode_project flake8 adma_geonode_project/`
- **Django check**: `docker exec django4adma_geonode_project python manage.py check`
- **Tests**: `docker compose run django python manage.py test --failfast` (or via `make test`)
- **Logs**: `docker compose logs --follow` (or `make logs`)
- **Stop**: `docker compose stop` (or `make down`)
- **Full reset**: `make reset` (brings down, up, waits, and re-syncs DB)

### Gotchas

- The `letsencrypt` container will continuously restart in dev mode — this is expected and not an error (SSL is disabled in dev).
- The base Docker image must be `geonode/geonode-base:latest-ubuntu-24.04` (not 22.04) to match the GDAL version required by GeoNode master.
- Map tiles may not render in the MapStore editor in containerized dev environments due to OpenStreetMap tile access; the MapStore interface and API still work correctly.
- The `requirements.txt` references GeoNode `@master` which is a moving target. If builds break, check for dependency version mismatches.
- Django startup takes ~60 seconds to become healthy (runs migrations, fixtures, and static file collection on first boot).
