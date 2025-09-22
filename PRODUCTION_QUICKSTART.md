# 🚀 ADMA Geo Production Quick Start

## 📦 One-Command Deployment

For automated deployment on a fresh Ubuntu server:

```bash
# 1. Install prerequisites
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER
sudo apt update && sudo apt install -y git certbot docker-compose-plugin
newgrp docker

# 2. Clone and deploy
git clone https://github.com/your-username/adma_geonode_project.git /opt/adma-geo
cd /opt/adma-geo
sudo chown -R $USER:$USER /opt/adma-geo
./deploy.sh

# 3. Configure DNS to point to your server IP
# 4. Access your app at https://your-domain.com
```

## 🎯 Manual Deployment Steps

### 1. Server Requirements
- **Ubuntu 20.04+** (or similar Linux)
- **8GB+ RAM** (16GB recommended)
- **50GB+ SSD** storage
- **Domain name** pointing to server
- **Ports 80, 443** open

### 2. Essential Commands

```bash
# Server setup
sudo apt update && sudo apt upgrade -y
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
sudo apt install git certbot docker-compose-plugin -y

# Clone project
git clone https://github.com/your-username/adma_geonode_project.git /opt/adma-geo
cd /opt/adma-geo/adma_geo

# Configure environment
cp .env.example .env.production
nano .env.production  # Edit with your settings

# SSL Setup (Let's Encrypt)
mkdir ssl
sudo certbot certonly --standalone -d your-domain.com -d www.your-domain.com
sudo cp /etc/letsencrypt/live/your-domain.com/*.pem ssl/
sudo chown $USER:$USER ssl/*.pem

# Deploy
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml exec django python manage.py migrate
docker compose -f docker-compose.prod.yml exec django python manage.py collectstatic --noinput
docker compose -f docker-compose.prod.yml exec django python manage.py createsuperuser
```

### 3. Configuration Files

**`.env.production`** (minimum required):
```env
POSTGRES_PASSWORD=your_secure_db_password
SECRET_KEY=your_50_char_django_secret_key
ALLOWED_HOSTS=your-domain.com,www.your-domain.com
GEOSERVER_ADMIN_PASSWORD=your_geoserver_password
DEBUG=False
```

**Domain setup in `nginx.prod.conf`**:
```bash
sed -i 's/your-domain.com/yourdomain.com/g' nginx.prod.conf
```

## 🔧 Management Commands

```bash
# Check status
docker compose -f docker-compose.prod.yml ps

# View logs
docker compose -f docker-compose.prod.yml logs -f

# Restart services
docker compose -f docker-compose.prod.yml restart

# Update application
git pull origin main
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d

# Database backup
docker compose -f docker-compose.prod.yml exec db pg_dump -U adma_geo_prod adma_geo_prod > backup.sql

# Restore database
docker compose -f docker-compose.prod.yml exec -i db psql -U adma_geo_prod -d adma_geo_prod < backup.sql
```

## 🛡️ Security Checklist

- [ ] **Firewall configured** (UFW: ports 22, 80, 443 only)
- [ ] **SSL certificate** installed and auto-renewing
- [ ] **Strong passwords** for database and GeoServer
- [ ] **Django SECRET_KEY** generated (50+ characters)
- [ ] **DEBUG=False** in production
- [ ] **ALLOWED_HOSTS** properly configured
- [ ] **Regular backups** scheduled
- [ ] **Docker logs** rotation configured
- [ ] **System updates** scheduled

## 📊 Monitoring

### Health Check Script
```bash
#!/bin/bash
echo "=== Service Status ==="
docker compose -f docker-compose.prod.yml ps

echo "=== Quick Tests ==="
curl -I https://your-domain.com
curl -I https://your-domain.com/geoserver/

echo "=== Resource Usage ==="
docker stats --no-stream
```

### Log Monitoring
```bash
# Real-time logs
docker compose -f docker-compose.prod.yml logs -f

# Error logs only
docker compose -f docker-compose.prod.yml logs | grep -i error

# Application logs
docker compose -f docker-compose.prod.yml logs django

# Database logs
docker compose -f docker-compose.prod.yml logs db
```

## 🚨 Troubleshooting

### Common Issues

**Service won't start:**
```bash
docker compose -f docker-compose.prod.yml logs [service-name]
docker compose -f docker-compose.prod.yml restart [service-name]
```

**SSL certificate issues:**
```bash
sudo certbot renew --dry-run
sudo certbot certificates
```

**Database connection errors:**
```bash
docker compose -f docker-compose.prod.yml exec db psql -U adma_geo_prod -d adma_geo_prod -c "SELECT 1;"
```

**Application not responding:**
```bash
docker compose -f docker-compose.prod.yml exec django python manage.py check --deploy
docker compose -f docker-compose.prod.yml restart django
```

**GeoServer issues:**
```bash
docker compose -f docker-compose.prod.yml logs geoserver
docker compose -f docker-compose.prod.yml restart geoserver
```

### Performance Issues

**High memory usage:**
```bash
# Check container memory
docker stats

# Restart memory-heavy services
docker compose -f docker-compose.prod.yml restart celery django
```

**Slow responses:**
```bash
# Check database performance
docker compose -f docker-compose.prod.yml exec db psql -U adma_geo_prod -d adma_geo_prod -c "
SELECT query, calls, total_time, mean_time 
FROM pg_stat_statements 
ORDER BY total_time DESC LIMIT 10;
"

# Database maintenance
docker compose -f docker-compose.prod.yml exec db psql -U adma_geo_prod -d adma_geo_prod -c "VACUUM ANALYZE;"
```

## 📞 Emergency Recovery

### Complete Service Restart
```bash
cd /opt/adma-geo/adma_geo
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d
```

### Database Recovery
```bash
# From backup
docker compose -f docker-compose.prod.yml stop django celery
docker compose -f docker-compose.prod.yml exec -i db psql -U adma_geo_prod -d adma_geo_prod < latest_backup.sql
docker compose -f docker-compose.prod.yml start django celery
```

### Full System Recovery
```bash
# Worst case: rebuild everything
git pull origin main
docker compose -f docker-compose.prod.yml down
docker system prune -a -f
docker compose -f docker-compose.prod.yml build --no-cache
docker compose -f docker-compose.prod.yml up -d
```

---

## 🎯 Key URLs After Deployment

- **Main Application**: `https://your-domain.com`
- **Admin Panel**: `https://your-domain.com/admin/`
- **GeoServer**: `https://your-domain.com/geoserver/`
- **Search**: `https://your-domain.com/search/`
- **Documentation**: `https://your-domain.com/documentation/`

## 📋 Post-Deployment Tasks

1. **Test all functionality** (login, upload, search, maps)
2. **Configure backup strategy**
3. **Set up monitoring/alerts**
4. **Document access credentials**
5. **Train users on the system**
6. **Schedule regular maintenance**

---

**Remember**: Keep your `.env.production` file secure and never commit it to version control!
