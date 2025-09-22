# 🚀 ADMA Geo Production Deployment Guide

## 📋 Prerequisites

### System Requirements
- **OS**: Ubuntu 20.04+ or CentOS 8+ (recommended: Ubuntu 22.04 LTS)
- **RAM**: Minimum 8GB (recommended: 16GB+)
- **Storage**: Minimum 50GB SSD (recommended: 100GB+)
- **CPU**: Minimum 4 cores (recommended: 8+ cores)

### Required Software
- Docker Engine 24.0+
- Docker Compose 2.0+
- Git
- SSL certificate (Let's Encrypt recommended)

## 🛠️ Step 1: Server Setup

### Install Docker and Docker Compose
```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER

# Install Docker Compose
sudo apt install docker-compose-plugin -y

# Verify installation
docker --version
docker compose version
```

### Install Git
```bash
sudo apt install git -y
```

## 📥 Step 2: Clone and Setup Project

### Clone Repository
```bash
# Clone to production directory
sudo mkdir -p /opt/adma-geo
sudo chown $USER:$USER /opt/adma-geo
cd /opt/adma-geo

# Clone the repository
git clone https://github.com/your-username/adma_geonode_project.git .
cd adma_geo
```

## ⚙️ Step 3: Production Configuration

### Create Production Environment File
```bash
# Create production environment file
cat > .env.production << 'EOF'
# Database Configuration
POSTGRES_DB=adma_geo_prod
POSTGRES_USER=adma_geo_prod
POSTGRES_PASSWORD=your_super_secure_password_here
POSTGRES_HOST=db
POSTGRES_PORT=5432

# Django Configuration
DEBUG=False
SECRET_KEY=your_very_long_and_secure_secret_key_here_at_least_50_characters
ALLOWED_HOSTS=your-domain.com,www.your-domain.com,localhost
DJANGO_SETTINGS_MODULE=adma_geo.settings

# Security Settings
SECURE_SSL_REDIRECT=True
SECURE_HSTS_SECONDS=31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS=True
SECURE_HSTS_PRELOAD=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True

# Email Configuration (for error reporting)
EMAIL_HOST=your-smtp-server.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=your-email@domain.com
EMAIL_HOST_PASSWORD=your-email-password

# GeoServer Configuration
GEOSERVER_ADMIN_USER=admin
GEOSERVER_ADMIN_PASSWORD=your_geoserver_admin_password
GEOSERVER_PUBLIC_URL=https://your-domain.com
GEOSERVER_INTERNAL_URL=http://geoserver:8080

# Redis Configuration
REDIS_URL=redis://redis:6379/0

# File Upload Settings
FILE_UPLOAD_MAX_MEMORY_SIZE=52428800  # 50MB
DATA_UPLOAD_MAX_MEMORY_SIZE=52428800  # 50MB
DATA_UPLOAD_MAX_NUMBER_FILES=1000
EOF
```

### Create Production Docker Compose
```bash
# Create production docker-compose file
cat > docker-compose.prod.yml << 'EOF'
version: '3.8'

services:
  # PostgreSQL Database
  db:
    image: postgis/postgis:15-3.3
    container_name: adma_geo_db_prod
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_INITDB_ARGS: "--encoding=UTF-8"
    volumes:
      - adma_geo_db_data_prod:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 30s
      timeout: 10s
      retries: 5
    networks:
      - adma_geo_network

  # Redis Cache
  redis:
    image: redis:7-alpine
    container_name: adma_geo_redis_prod
    restart: unless-stopped
    command: redis-server --appendonly yes
    volumes:
      - adma_geo_redis_data_prod:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 30s
      timeout: 10s
      retries: 5
    networks:
      - adma_geo_network

  # Django Web Application
  django:
    build: .
    container_name: adma_geo_django_prod
    restart: unless-stopped
    env_file: .env.production
    volumes:
      - adma_geo_media_prod:/app/media
      - adma_geo_static_prod:/app/static
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python", "manage.py", "check", "--deploy"]
      interval: 30s
      timeout: 10s
      retries: 5
    networks:
      - adma_geo_network

  # Celery Worker
  celery:
    build: .
    container_name: adma_geo_celery_prod
    restart: unless-stopped
    command: celery -A adma_geo worker --loglevel=info --concurrency=4
    env_file: .env.production
    volumes:
      - adma_geo_media_prod:/app/media
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "celery", "-A", "adma_geo", "inspect", "ping"]
      interval: 60s
      timeout: 30s
      retries: 3
    networks:
      - adma_geo_network

  # Nginx Reverse Proxy
  nginx:
    image: nginx:alpine
    container_name: adma_geo_nginx_prod
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.prod.conf:/etc/nginx/conf.d/default.conf
      - adma_geo_static_prod:/var/www/static
      - adma_geo_media_prod:/var/www/media
      - ./ssl:/etc/nginx/ssl  # SSL certificates
    depends_on:
      - django
    healthcheck:
      test: ["CMD", "nginx", "-t"]
      interval: 30s
      timeout: 10s
      retries: 5
    networks:
      - adma_geo_network

  # GeoServer for spatial data
  geoserver:
    image: kartoza/geoserver:2.24.0
    container_name: adma_geo_geoserver_prod
    restart: unless-stopped
    environment:
      GEOSERVER_ADMIN_USER: ${GEOSERVER_ADMIN_USER}
      GEOSERVER_ADMIN_PASSWORD: ${GEOSERVER_ADMIN_PASSWORD}
      GEOSERVER_DATA_DIR: /opt/geoserver_data
      POSTGRES_HOST: db
      POSTGRES_PORT: 5432
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASS: ${POSTGRES_PASSWORD}
      GEOSERVER_CSRF_WHITELIST: your-domain.com
    volumes:
      - adma_geo_geoserver_data_prod:/opt/geoserver_data
      - adma_geo_media_prod:/opt/geoserver_data/data
    depends_on:
      db:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/geoserver/web/"]
      interval: 60s
      timeout: 30s
      retries: 5
    networks:
      - adma_geo_network

volumes:
  adma_geo_db_data_prod:
  adma_geo_redis_data_prod:
  adma_geo_media_prod:
  adma_geo_static_prod:
  adma_geo_geoserver_data_prod:

networks:
  adma_geo_network:
    driver: bridge
EOF
```

### Create Production Nginx Configuration
```bash
# Create production nginx configuration
cat > nginx.prod.conf << 'EOF'
# Rate limiting
limit_req_zone $binary_remote_addr zone=login:10m rate=10r/m;
limit_req_zone $binary_remote_addr zone=api:10m rate=100r/m;

upstream django_backend {
    server django:8000;
}

upstream geoserver_backend {
    server geoserver:8080;
}

# HTTP to HTTPS redirect
server {
    listen 80;
    server_name your-domain.com www.your-domain.com;
    return 301 https://$server_name$request_uri;
}

# HTTPS server
server {
    listen 443 ssl http2;
    server_name your-domain.com www.your-domain.com;

    # SSL Configuration
    ssl_certificate /etc/nginx/ssl/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/privkey.pem;
    
    # SSL Security Settings
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-RSA-AES256-GCM-SHA512:DHE-RSA-AES256-GCM-SHA512:ECDHE-RSA-AES256-GCM-SHA384:DHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;
    
    # Security Headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    # File upload limits
    client_max_body_size 100M;
    client_body_timeout 300s;
    client_header_timeout 300s;

    # Static files
    location /static/ {
        alias /var/www/static/;
        expires 1y;
        add_header Cache-Control "public, immutable";
        gzip on;
        gzip_types text/css application/javascript image/svg+xml;
    }

    # Media files
    location /media/ {
        alias /var/www/media/;
        expires 1y;
        add_header Cache-Control "public";
    }

    # GeoServer proxy
    location /geoserver/ {
        proxy_pass http://geoserver_backend/geoserver/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }

    # API endpoints with rate limiting
    location /api/ {
        limit_req zone=api burst=20 nodelay;
        proxy_pass http://django_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Login endpoints with stricter rate limiting
    location /accounts/login/ {
        limit_req zone=login burst=5 nodelay;
        proxy_pass http://django_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Main application
    location / {
        proxy_pass http://django_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }
}
EOF
```

## 🔐 Step 4: SSL Certificate Setup

### Option A: Let's Encrypt (Recommended)
```bash
# Install Certbot
sudo apt install certbot -y

# Stop nginx temporarily
sudo docker compose -f docker-compose.prod.yml stop nginx

# Generate SSL certificate
sudo certbot certonly --standalone -d your-domain.com -d www.your-domain.com

# Create SSL directory and copy certificates
mkdir -p ssl
sudo cp /etc/letsencrypt/live/your-domain.com/fullchain.pem ssl/
sudo cp /etc/letsencrypt/live/your-domain.com/privkey.pem ssl/
sudo chown $USER:$USER ssl/*.pem
```

### Option B: Custom SSL Certificate
```bash
# Create SSL directory
mkdir -p ssl

# Copy your SSL certificates to:
# ssl/fullchain.pem (certificate + intermediate)
# ssl/privkey.pem (private key)
```

## 🚀 Step 5: Deploy Application

### Generate Secret Key
```bash
# Generate a secure Django secret key
python3 -c "
import secrets
import string
alphabet = string.ascii_letters + string.digits + '!@#$%^&*(-_=+)'
secret_key = ''.join(secrets.choice(alphabet) for i in range(50))
print('Generated SECRET_KEY:', secret_key)
"
```

### Update Configuration
```bash
# Edit .env.production with your actual values
nano .env.production

# Update nginx.prod.conf with your domain
sed -i 's/your-domain.com/youractual-domain.com/g' nginx.prod.conf
```

### Build and Deploy
```bash
# Build the application
docker compose -f docker-compose.prod.yml build

# Start the services
docker compose -f docker-compose.prod.yml up -d

# Run migrations
docker compose -f docker-compose.prod.yml exec django python manage.py migrate

# Collect static files
docker compose -f docker-compose.prod.yml exec django python manage.py collectstatic --noinput

# Create superuser
docker compose -f docker-compose.prod.yml exec django python manage.py createsuperuser
```

## 🔧 Step 6: Post-Deployment Configuration

### Verify Services
```bash
# Check all services are running
docker compose -f docker-compose.prod.yml ps

# Check logs
docker compose -f docker-compose.prod.yml logs -f

# Test database connection
docker compose -f docker-compose.prod.yml exec django python manage.py dbshell
```

### Setup Monitoring
```bash
# Create monitoring script
cat > monitor.sh << 'EOF'
#!/bin/bash
cd /opt/adma-geo/adma_geo
docker compose -f docker-compose.prod.yml ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
EOF

chmod +x monitor.sh
```

### Setup Automatic SSL Renewal
```bash
# Add cron job for SSL renewal
(crontab -l 2>/dev/null; echo "0 3 * * * /usr/bin/certbot renew --quiet --deploy-hook 'cd /opt/adma-geo/adma_geo && docker compose -f docker-compose.prod.yml restart nginx'") | crontab -
```

## 🛡️ Step 7: Security & Backup

### Firewall Configuration
```bash
# Install and configure UFW
sudo ufw allow ssh
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
```

### Database Backup Script
```bash
# Create backup script
cat > backup.sh << 'EOF'
#!/bin/bash
BACKUP_DIR="/opt/backups/adma-geo"
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p $BACKUP_DIR

# Database backup
docker compose -f docker-compose.prod.yml exec -T db pg_dump -U adma_geo_prod adma_geo_prod > $BACKUP_DIR/db_$DATE.sql

# Media files backup
tar -czf $BACKUP_DIR/media_$DATE.tar.gz -C /var/lib/docker/volumes/adma_geo_adma_geo_media_prod/_data .

# Keep only last 7 days of backups
find $BACKUP_DIR -name "*.sql" -mtime +7 -delete
find $BACKUP_DIR -name "*.tar.gz" -mtime +7 -delete

echo "Backup completed: $DATE"
EOF

chmod +x backup.sh

# Add daily backup cron job
(crontab -l 2>/dev/null; echo "0 2 * * * /opt/adma-geo/adma_geo/backup.sh") | crontab -
```

## 📊 Step 8: Performance Optimization

### System Optimization
```bash
# Increase file limits
echo "fs.file-max = 65536" | sudo tee -a /etc/sysctl.conf
echo "* soft nofile 65536" | sudo tee -a /etc/security/limits.conf
echo "* hard nofile 65536" | sudo tee -a /etc/security/limits.conf

# Apply changes
sudo sysctl -p
```

### Docker Optimization
```bash
# Configure Docker daemon for production
sudo cat > /etc/docker/daemon.json << 'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  },
  "storage-driver": "overlay2"
}
EOF

sudo systemctl restart docker
```

## 🔍 Step 9: Testing & Verification

### Health Checks
```bash
# Test application
curl -I https://your-domain.com
curl -I https://your-domain.com/geoserver/

# Test SSL
curl -I https://your-domain.com --http2

# Check search functionality
docker compose -f docker-compose.prod.yml exec django python manage.py shell -c "
from filemanager.postgres_search import postgres_search
print('Search test:', postgres_search.search_all('test', '1', limit=1))
"
```

## 🆘 Troubleshooting

### Common Issues
```bash
# Check logs
docker compose -f docker-compose.prod.yml logs django
docker compose -f docker-compose.prod.yml logs nginx
docker compose -f docker-compose.prod.yml logs db

# Restart services
docker compose -f docker-compose.prod.yml restart

# Clear Docker cache if needed
docker system prune -a
```

### Performance Issues
```bash
# Monitor resource usage
docker stats

# Check database performance
docker compose -f docker-compose.prod.yml exec db psql -U adma_geo_prod -d adma_geo_prod -c "
SELECT schemaname,tablename,attname,n_distinct,correlation 
FROM pg_stats 
WHERE tablename IN ('filemanager_file', 'filemanager_folder', 'filemanager_map');
"
```

## 📚 Maintenance Commands

### Regular Maintenance
```bash
# Update application
cd /opt/adma-geo/adma_geo
git pull origin main
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d

# Clean up old Docker images
docker image prune -a -f

# View application logs
docker compose -f docker-compose.prod.yml logs -f --tail=100

# Database maintenance
docker compose -f docker-compose.prod.yml exec db psql -U adma_geo_prod -d adma_geo_prod -c "VACUUM ANALYZE;"
```

---

## 🎉 Deployment Complete!

Your ADMA Geo application should now be running at:
- **Main App**: https://your-domain.com
- **GeoServer**: https://your-domain.com/geoserver/
- **Admin**: https://your-domain.com/admin/

**Important Notes:**
1. Replace `your-domain.com` with your actual domain
2. Update all passwords in `.env.production`
3. Configure DNS to point to your server
4. Monitor logs regularly for any issues
5. Keep backups in a separate location

**Security Reminders:**
- Never commit `.env.production` to version control
- Regularly update Docker images
- Monitor security advisories for dependencies
- Use strong passwords for all services
