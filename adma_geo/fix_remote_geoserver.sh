#!/bin/bash

echo "🔧 Remote GeoServer Fix Script"
echo "=============================="

# Check if docker-compose-adma.yml exists
if [ ! -f "docker-compose-adma.yml" ]; then
    echo "❌ docker-compose-adma.yml not found!"
    echo "   Make sure you're in the correct directory"
    exit 1
fi

echo "📋 Step 1: Checking current container status..."
docker-compose -f docker-compose-adma.yml ps

echo ""
echo "📋 Step 2: Pulling latest changes from git..."
git pull origin main

echo ""
echo "📋 Step 3: Stopping and removing GeoServer..."
docker-compose -f docker-compose-adma.yml stop geoserver
docker-compose -f docker-compose-adma.yml rm -f geoserver

echo ""
echo "📋 Step 4: Checking for corrupted GeoServer volume..."
VOLUME_NAME=$(docker volume ls | grep geoserver | awk '{print $2}' | head -1)
if [ ! -z "$VOLUME_NAME" ]; then
    echo "   Found GeoServer volume: $VOLUME_NAME"
    echo "   Removing corrupted volume..."
    docker volume rm "$VOLUME_NAME" || echo "   Volume removal failed (may be in use)"
else
    echo "   No GeoServer volume found"
fi

echo ""
echo "📋 Step 5: Rebuilding and starting services..."
docker-compose -f docker-compose-adma.yml up -d --build django celery geoserver

echo ""
echo "📋 Step 6: Waiting for GeoServer to initialize..."
sleep 45

echo ""
echo "📋 Step 7: Checking final status..."
docker-compose -f docker-compose-adma.yml ps

echo ""
echo "📋 Step 8: Running diagnostic test..."
docker-compose -f docker-compose-adma.yml exec django python remote_geoserver_debug.py

echo ""
echo "✅ Remote GeoServer fix completed!"
echo ""
echo "🧪 Test Instructions:"
echo "1. Go to your domain (e.g., https://adma.unl.edu)"
echo "2. Upload a vector file"
echo "3. Check for successful auto-publishing"
echo ""
echo "📊 If still failing, check logs:"
echo "   docker-compose -f docker-compose-adma.yml logs django --tail=50"
echo "   docker-compose -f docker-compose-adma.yml logs geoserver --tail=50"
