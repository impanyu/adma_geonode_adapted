FROM python:3.10-slim-bullseye

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV LD_LIBRARY_PATH="/usr/local/lib:$LD_LIBRARY_PATH"

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libpq-dev \
    postgresql-client \
    build-essential \
    libmagic1 \
    gdal-bin \
    libgdal-dev \
    python3-gdal \
    libgeos-dev \
    libproj-dev \
    proj-data \
    proj-bin \
    libspatialindex-dev \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Install SQLite 3.35+ from source (required for ChromaDB)
RUN cd /tmp && \
    wget https://www.sqlite.org/2024/sqlite-autoconf-3460000.tar.gz && \
    tar xzf sqlite-autoconf-3460000.tar.gz && \
    cd sqlite-autoconf-3460000 && \
    ./configure --prefix=/usr/local && \
    make && \
    make install && \
    cd / && \
    rm -rf /tmp/sqlite-autoconf-3460000* && \
    echo "/usr/local/lib" > /etc/ld.so.conf.d/sqlite3.conf && \
    ldconfig && \
    # Update Python to use new SQLite
    export LD_LIBRARY_PATH="/usr/local/lib:$LD_LIBRARY_PATH"

# Set work directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Create necessary directories
RUN mkdir -p /app/media/uploads /app/staticfiles

# Collect static files (initial collection during build).
# DJANGO_DEV_FALLBACK=1 lets settings.py boot with a placeholder SECRET_KEY
# for this command only; runtime still requires SECRET_KEY in env.
# Same for POSTGRES_PASSWORD and GEOSERVER_ADMIN_PASSWORD — collectstatic
# doesn't actually connect to the DB or GeoServer, so any value works here.
RUN DJANGO_DEV_FALLBACK=1 \
    POSTGRES_PASSWORD=build-noop \
    GEOSERVER_ADMIN_PASSWORD=build-noop \
    python manage.py collectstatic --noinput

# Expose port
EXPOSE 8000

# Default command (overridden by docker-compose)
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
