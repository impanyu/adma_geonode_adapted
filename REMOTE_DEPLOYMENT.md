# ADMA Geo Remote Server Deployment Guide

## Prerequisites

- Remote server with Ubuntu 20.04+ or similar Linux distribution
- Root or sudo access
- At least 4GB RAM and 20GB disk space
- Domain name or IP address for the server

## Step 1: Server Preparation

### 1.1 Connect to your remote server
```bash
ssh username@your-server-ip
# or
ssh username@your-domain.com
```

### 1.2 Update system packages
```bash
sudo apt update && sudo apt upgrade -y
```

### 1.3 Install Docker
```bash
# Remove old versions if they exist
sudo apt remove docker docker-engine docker.io containerd runc

# Install dependencies
sudo apt install -y apt-transport-https ca-certificates curl gnupg lsb-release

# Add Docker's official GPG key
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg

# Add Docker repository
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io

# Add your user to docker group
sudo usermod -aG docker $USER

# Enable Docker to start on boot
sudo systemctl enable docker
sudo systemctl start docker
```

### 1.4 Install Docker Compose
```bash
# Download Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose

# Make it executable
sudo chmod +x /usr/local/bin/docker-compose

# Verify installation
docker --version
docker-compose --version
```

### 1.5 Install Git
```bash
sudo apt install -y git
```

## Step 2: Deploy the Application

### 2.1 Clone the repository
```bash
# Clone your repository (replace with your actual repository URL)
git clone https://github.com/yourusername/adma_geonode_project.git
cd adma_geonode_project/adma_geo

# Or if you're transferring from local machine:
# Use scp to copy the adma_geo folder to the server
```

### 2.2 Create production environment file
```bash
# Create environment file for production
cat > .env.production << EOF
DEBUG=False
SECRET_KEY=your-super-secret-production-key-change-this
POSTGRES_DB=adma_geo
POSTGRES_USER=adma_geo
POSTGRES_PASSWORD=your-secure-db-password
POSTGRES_HOST=db
POSTGRES_PORT=5432
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/0
ALLOWED_HOSTS=your-domain.com,your-server-ip,localhost
EOF

# Secure the environment file
chmod 600 .env.production
```

### 2.3 Create production Docker Compose override
```bash
cat > docker-compose.production.yml << EOF
version: '3.8'

services:
  django:
    environment:
      DEBUG: "False"
      SECRET_KEY: "your-super-secret-production-key-change-this"
      ALLOWED_HOSTS: "your-domain.com,your-server-ip,localhost"
    command: >
      sh -c "python manage.py migrate &&
             python manage.py collectstatic --noinput &&
             gunicorn adma_geo.wsgi:application --bind 0.0.0.0:8000 --workers 3"
    restart: unless-stopped

  celery:
    restart: unless-stopped

  db:
    restart: unless-stopped
    environment:
      POSTGRES_PASSWORD: "your-secure-db-password"

  redis:
    restart: unless-stopped

  nginx:
    restart: unless-stopped

  geoserver:
    restart: unless-stopped
EOF
```

### 2.4 Update nginx configuration for production
```bash
cat > nginx.conf << EOF
events {
    worker_connections 1024;
}

http {
    upstream django {
        server django:8000;
    }

    server {
        listen 80;
        server_name your-domain.com your-server-ip;

        client_max_body_size 500M;

        # Security headers
        add_header X-Frame-Options DENY;
        add_header X-Content-Type-Options nosniff;
        add_header X-XSS-Protection "1; mode=block";

        location /static/ {
            alias /var/www/static/;
            expires 30d;
            add_header Cache-Control "public, no-transform";
        }

        location /media/ {
            alias /var/www/media/;
            expires 30d;
            add_header Cache-Control "public, no-transform";
        }

        # GeoServer WMS proxy
        location /wms {
            proxy_pass http://geoserver:8080/geoserver/adma_geo/wms;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-Proto \$scheme;
            proxy_redirect off;
            proxy_read_timeout 300s;
            proxy_connect_timeout 75s;
        }

        # GeoServer REST API proxy
        location /geoserver/ {
            proxy_pass http://geoserver:8080/geoserver/;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-Proto \$scheme;
            proxy_redirect off;
            proxy_read_timeout 300s;
            proxy_connect_timeout 75s;
        }

        location / {
            proxy_pass http://django;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-Proto \$scheme;
            proxy_redirect off;
            proxy_read_timeout 300s;
            proxy_connect_timeout 75s;
        }
    }
}
EOF
```

## Step 3: Launch the Application

### 3.1 Build and start services
```bash
# Build the application images
docker-compose -f docker-compose.yml -f docker-compose.production.yml build

# Start all services
docker-compose -f docker-compose.yml -f docker-compose.production.yml up -d

# Check status
docker-compose ps
```

### 3.2 Wait for services to be ready
```bash
# Check logs for any issues
docker-compose logs -f django

# Wait for database to be ready
docker-compose exec django python manage.py check --database default
```

### 3.3 Create superuser account
```bash
# Create admin user
docker-compose exec django python manage.py createsuperuser

# Or create specific user
docker-compose exec django python manage.py shell -c "
from django.contrib.auth.models import User
User.objects.create_superuser('yu.pan@unl.edu', 'yu.pan@unl.edu', 'your-secure-password')
"
```

## Step 4: Configure Firewall

### 4.1 Set up UFW firewall
```bash
# Enable UFW
sudo ufw enable

# Allow SSH (important - don't lock yourself out!)
sudo ufw allow ssh

# Allow HTTP and HTTPS
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Check status
sudo ufw status
```

## Step 5: Set up SSL (Optional but Recommended)

### 5.1 Install Certbot for Let's Encrypt
```bash
sudo apt install -y certbot

# Get SSL certificate
sudo certbot certonly --standalone -d your-domain.com

# Update nginx configuration to use SSL
# (You'll need to modify nginx.conf to include SSL settings)
```

## Step 6: Monitoring and Maintenance

### 6.1 Set up log rotation
```bash
# View container logs
docker-compose logs

# Follow logs in real-time
docker-compose logs -f

# View specific service logs
docker-compose logs django
docker-compose logs nginx
```

### 6.2 Create backup script
```bash
cat > backup.sh << 'EOF'
#!/bin/bash
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/home/$USER/backups"

mkdir -p $BACKUP_DIR

# Backup database
docker-compose exec -T db pg_dump -U adma_geo adma_geo > $BACKUP_DIR/db_backup_$DATE.sql

# Backup media files
docker run --rm -v adma_geo_adma_geo_media:/data -v $BACKUP_DIR:/backup ubuntu tar czf /backup/media_backup_$DATE.tar.gz -C /data .

echo "Backup completed: $DATE"
EOF

chmod +x backup.sh
```

### 6.3 Set up automatic backups (optional)
```bash
# Add to crontab for daily backups at 2 AM
(crontab -l 2>/dev/null; echo "0 2 * * * /home/$USER/adma_geonode_project/adma_geo/backup.sh") | crontab -
```

## Step 7: Update and Restart Commands

### 7.1 Update application
```bash
# Pull latest changes
git pull origin main

# Rebuild and restart
docker-compose -f docker-compose.yml -f docker-compose.production.yml build
docker-compose -f docker-compose.yml -f docker-compose.production.yml up -d

# Run migrations if needed
docker-compose exec django python manage.py migrate
```

### 7.2 Restart services
```bash
# Restart all services
docker-compose restart

# Restart specific service
docker-compose restart django
docker-compose restart nginx
```

### 7.3 View service status
```bash
# Check container status
docker-compose ps

# Check service health
docker-compose exec django python manage.py check
```

## Troubleshooting

### Common Issues:

1. **Permission errors**: Make sure Docker is running and user is in docker group
```bash
sudo systemctl status docker
groups $USER
```

2. **Port conflicts**: Check if ports 80, 5433, 6380, 8080 are free
```bash
sudo netstat -tulpn | grep :80
```

3. **Memory issues**: Monitor system resources
```bash
free -h
df -h
docker system df
```

4. **Database connection issues**: Check database logs
```bash
docker-compose logs db
```

## Security Checklist

- [ ] Change default passwords
- [ ] Set strong SECRET_KEY
- [ ] Configure firewall (UFW)
- [ ] Set up SSL/HTTPS
- [ ] Regular security updates
- [ ] Monitor logs for suspicious activity
- [ ] Backup data regularly

## Access Points

After successful deployment:
- **Main Application**: http://your-domain.com or http://your-server-ip
- **Admin Panel**: http://your-domain.com/admin/
- **GeoServer**: http://your-domain.com/geoserver/

## Support

If you encounter issues:
1. Check container logs: `docker-compose logs`
2. Verify all services are running: `docker-compose ps`
3. Check system resources: `free -h` and `df -h`
4. Review this deployment guide for missed steps
