#!/bin/bash

# 🚀 ADMA Geo Production Deployment Script
# Run this script on your production server after initial setup

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
log_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

log_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

log_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

log_error() {
    echo -e "${RED}❌ $1${NC}"
}

# Check if running as root
if [[ $EUID -eq 0 ]]; then
   log_error "This script should not be run as root"
   exit 1
fi

# Configuration
PROJECT_DIR="/opt/adma-geo"
APP_DIR="$PROJECT_DIR/adma_geo"
COMPOSE_FILE="docker-compose.prod.yml"

log_info "Starting ADMA Geo Production Deployment..."

# Check prerequisites
log_info "Checking prerequisites..."

if ! command -v docker &> /dev/null; then
    log_error "Docker is not installed. Please install Docker first."
    exit 1
fi

if ! command -v docker &> /dev/null || ! docker compose version &> /dev/null; then
    log_error "Docker Compose is not installed. Please install Docker Compose first."
    exit 1
fi

if ! command -v git &> /dev/null; then
    log_error "Git is not installed. Please install Git first."
    exit 1
fi

log_success "Prerequisites check passed"

# Function to prompt for input with default value
prompt_input() {
    local prompt="$1"
    local default="$2"
    local result
    
    if [[ -n "$default" ]]; then
        read -p "$prompt [$default]: " result
        result="${result:-$default}"
    else
        read -p "$prompt: " result
    fi
    
    echo "$result"
}

# Function to prompt for password (hidden input)
prompt_password() {
    local prompt="$1"
    local result
    
    while true; do
        read -s -p "$prompt: " result
        echo
        if [[ -n "$result" ]]; then
            break
        else
            log_warning "Password cannot be empty. Please try again."
        fi
    done
    
    echo "$result"
}

# Function to generate secure password
generate_password() {
    openssl rand -base64 32 | tr -d "=+/" | cut -c1-25
}

# Function to generate Django secret key
generate_secret_key() {
    python3 -c "
import secrets
import string
alphabet = string.ascii_letters + string.digits + '!@#$%^&*(-_=+)'
secret_key = ''.join(secrets.choice(alphabet) for i in range(50))
print(secret_key)
"
}

# Create project directory if it doesn't exist
if [[ ! -d "$PROJECT_DIR" ]]; then
    log_info "Creating project directory..."
    sudo mkdir -p "$PROJECT_DIR"
    sudo chown $USER:$USER "$PROJECT_DIR"
fi

cd "$PROJECT_DIR"

# Clone or update repository
if [[ ! -d ".git" ]]; then
    log_info "Cloning repository..."
    REPO_URL=$(prompt_input "Enter repository URL" "https://github.com/your-username/adma_geonode_project.git")
    git clone "$REPO_URL" .
else
    log_info "Updating repository..."
    git pull origin main
fi

cd "$APP_DIR"

# Collect configuration
log_info "Collecting deployment configuration..."

DOMAIN=$(prompt_input "Enter your domain name" "your-domain.com")
DB_PASSWORD=$(prompt_password "Enter database password (or press Enter to generate)")
if [[ -z "$DB_PASSWORD" ]]; then
    DB_PASSWORD=$(generate_password)
    log_info "Generated database password: $DB_PASSWORD"
fi

GEOSERVER_PASSWORD=$(prompt_password "Enter GeoServer admin password (or press Enter to generate)")
if [[ -z "$GEOSERVER_PASSWORD" ]]; then
    GEOSERVER_PASSWORD=$(generate_password)
    log_info "Generated GeoServer password: $GEOSERVER_PASSWORD"
fi

SECRET_KEY=$(generate_secret_key)
log_info "Generated Django secret key"

EMAIL_HOST=$(prompt_input "Enter SMTP host (optional)" "")
EMAIL_USER=$(prompt_input "Enter SMTP username (optional)" "")
EMAIL_PASS=""
if [[ -n "$EMAIL_USER" ]]; then
    EMAIL_PASS=$(prompt_password "Enter SMTP password")
fi

# Create production environment file
log_info "Creating production environment file..."

cat > .env.production << EOF
# Database Configuration
POSTGRES_DB=adma_geo_prod
POSTGRES_USER=adma_geo_prod
POSTGRES_PASSWORD=$DB_PASSWORD
POSTGRES_HOST=db
POSTGRES_PORT=5432

# Django Configuration
DEBUG=False
SECRET_KEY=$SECRET_KEY
ALLOWED_HOSTS=$DOMAIN,www.$DOMAIN,localhost
DJANGO_SETTINGS_MODULE=adma_geo.settings

# Security Settings
SECURE_SSL_REDIRECT=True
SECURE_HSTS_SECONDS=31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS=True
SECURE_HSTS_PRELOAD=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True

# Email Configuration
EMAIL_HOST=$EMAIL_HOST
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=$EMAIL_USER
EMAIL_HOST_PASSWORD=$EMAIL_PASS

# GeoServer Configuration
GEOSERVER_ADMIN_USER=admin
GEOSERVER_ADMIN_PASSWORD=$GEOSERVER_PASSWORD
GEOSERVER_PUBLIC_URL=https://$DOMAIN
GEOSERVER_INTERNAL_URL=http://geoserver:8080

# Redis Configuration
REDIS_URL=redis://redis:6379/0

# File Upload Settings
FILE_UPLOAD_MAX_MEMORY_SIZE=52428800
DATA_UPLOAD_MAX_MEMORY_SIZE=52428800
DATA_UPLOAD_MAX_NUMBER_FILES=1000
EOF

log_success "Environment file created"

# Update nginx configuration with domain
if [[ -f "nginx.prod.conf" ]]; then
    log_info "Updating nginx configuration..."
    sed -i "s/your-domain.com/$DOMAIN/g" nginx.prod.conf
    log_success "Nginx configuration updated"
fi

# SSL Certificate setup
log_info "Setting up SSL certificates..."
mkdir -p ssl

if command -v certbot &> /dev/null; then
    USE_LETSENCRYPT=$(prompt_input "Use Let's Encrypt for SSL? (y/n)" "y")
    
    if [[ "$USE_LETSENCRYPT" == "y" || "$USE_LETSENCRYPT" == "Y" ]]; then
        log_info "Generating Let's Encrypt certificate..."
        
        # Stop nginx if running
        docker compose -f $COMPOSE_FILE stop nginx 2>/dev/null || true
        
        # Generate certificate
        sudo certbot certonly --standalone -d "$DOMAIN" -d "www.$DOMAIN" --agree-tos --register-unsafely-without-email --non-interactive
        
        # Copy certificates
        sudo cp "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ssl/
        sudo cp "/etc/letsencrypt/live/$DOMAIN/privkey.pem" ssl/
        sudo chown $USER:$USER ssl/*.pem
        
        log_success "SSL certificate generated"
    else
        log_warning "Please place your SSL certificates in ssl/fullchain.pem and ssl/privkey.pem"
        read -p "Press Enter when certificates are in place..."
    fi
else
    log_warning "Certbot not found. Please install SSL certificates manually in ssl/ directory"
    read -p "Press Enter when certificates are in place..."
fi

# Build and deploy
log_info "Building application..."
docker compose -f $COMPOSE_FILE build

log_info "Starting services..."
docker compose -f $COMPOSE_FILE up -d

# Wait for database to be ready
log_info "Waiting for database to be ready..."
sleep 30

# Run migrations
log_info "Running database migrations..."
docker compose -f $COMPOSE_FILE exec -T django python manage.py migrate

# Collect static files
log_info "Collecting static files..."
docker compose -f $COMPOSE_FILE exec -T django python manage.py collectstatic --noinput

# Create superuser
log_info "Creating superuser account..."
echo "Please create your admin account:"
docker compose -f $COMPOSE_FILE exec django python manage.py createsuperuser

# Set up SSL renewal cron job
if command -v certbot &> /dev/null && [[ "$USE_LETSENCRYPT" == "y" || "$USE_LETSENCRYPT" == "Y" ]]; then
    log_info "Setting up SSL renewal..."
    CRON_CMD="0 3 * * * /usr/bin/certbot renew --quiet --deploy-hook 'cd $APP_DIR && docker compose -f $COMPOSE_FILE restart nginx'"
    (crontab -l 2>/dev/null | grep -v "$CRON_CMD"; echo "$CRON_CMD") | crontab -
    log_success "SSL auto-renewal configured"
fi

# Set up backup cron job
log_info "Setting up automated backups..."
cat > backup.sh << 'EOF'
#!/bin/bash
BACKUP_DIR="/opt/backups/adma-geo"
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p $BACKUP_DIR

# Database backup
docker compose -f docker-compose.prod.yml exec -T db pg_dump -U adma_geo_prod adma_geo_prod > $BACKUP_DIR/db_$DATE.sql

# Media files backup
docker run --rm -v adma_geo_adma_geo_media_prod:/data -v $BACKUP_DIR:/backup alpine tar czf /backup/media_$DATE.tar.gz -C /data .

# Keep only last 7 days of backups
find $BACKUP_DIR -name "*.sql" -mtime +7 -delete
find $BACKUP_DIR -name "*.tar.gz" -mtime +7 -delete

echo "Backup completed: $DATE"
EOF

chmod +x backup.sh

# Add backup cron job
BACKUP_CRON="0 2 * * * $APP_DIR/backup.sh"
(crontab -l 2>/dev/null | grep -v "$BACKUP_CRON"; echo "$BACKUP_CRON") | crontab -

log_success "Automated backups configured"

# Final verification
log_info "Running final verification..."

# Check services
sleep 10
docker compose -f $COMPOSE_FILE ps

# Test application
if curl -f -s "https://$DOMAIN" > /dev/null; then
    log_success "Application is responding at https://$DOMAIN"
else
    log_warning "Application may not be fully ready yet. Please check logs if issues persist."
fi

# Create monitoring script
cat > monitor.sh << 'EOF'
#!/bin/bash
echo "=== ADMA Geo Service Status ==="
docker compose -f docker-compose.prod.yml ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"

echo -e "\n=== Disk Usage ==="
df -h | head -n 1
df -h | grep -E '/|docker'

echo -e "\n=== Memory Usage ==="
free -h

echo -e "\n=== Docker Stats ==="
docker stats --no-stream --format "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"
EOF

chmod +x monitor.sh

# Display deployment summary
log_success "🎉 Deployment completed successfully!"
echo
echo "=== DEPLOYMENT SUMMARY ==="
echo "Domain: https://$DOMAIN"
echo "GeoServer: https://$DOMAIN/geoserver/"
echo "Admin: https://$DOMAIN/admin/"
echo
echo "Database Password: $DB_PASSWORD"
echo "GeoServer Password: $GEOSERVER_PASSWORD"
echo
echo "=== USEFUL COMMANDS ==="
echo "Monitor services: ./monitor.sh"
echo "View logs: docker compose -f $COMPOSE_FILE logs -f"
echo "Restart services: docker compose -f $COMPOSE_FILE restart"
echo "Manual backup: ./backup.sh"
echo
echo "=== IMPORTANT NOTES ==="
echo "1. Save your passwords securely"
echo "2. DNS should point to this server"
echo "3. Firewall ports 80 and 443 should be open"
echo "4. Monitor logs for any issues"
echo "5. Backups are automated daily at 2 AM"
echo
log_success "Your ADMA Geo application is now running in production!"
