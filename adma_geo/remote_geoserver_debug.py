#!/usr/bin/env python3
"""
Remote GeoServer Diagnostic Script
Run this on your remote server to diagnose GeoServer connection issues
"""

import os
import sys
import django
import requests
from requests.auth import HTTPBasicAuth
import json

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'adma_geo.settings')
django.setup()

from django.conf import settings

def test_remote_geoserver():
    """Comprehensive GeoServer diagnostic for remote server"""
    
    print("🔍 Remote GeoServer Connection Diagnostic")
    print("=" * 60)
    
    # Get Django settings
    geoserver_url = getattr(settings, 'GEOSERVER_URL', 'NOT SET')
    admin_user = getattr(settings, 'GEOSERVER_ADMIN_USER', 'NOT SET')
    admin_password = getattr(settings, 'GEOSERVER_ADMIN_PASSWORD', 'NOT SET')
    workspace = getattr(settings, 'GEOSERVER_WORKSPACE', 'adma_geo')
    
    print(f"📍 Django GEOSERVER_URL: {geoserver_url}")
    print(f"👤 Django GEOSERVER_ADMIN_USER: {admin_user}")
    print(f"🔑 Django GEOSERVER_ADMIN_PASSWORD: {'*' * len(str(admin_password)) if admin_password != 'NOT SET' else 'NOT SET'}")
    print(f"🏢 Django GEOSERVER_WORKSPACE: {workspace}")
    print()
    
    # Check environment variables
    print("🌍 Environment Variables Check:")
    env_vars = ['GEOSERVER_URL', 'GEOSERVER_ADMIN_USER', 'GEOSERVER_ADMIN_PASSWORD']
    for var in env_vars:
        value = os.environ.get(var, 'NOT SET')
        if var == 'GEOSERVER_ADMIN_PASSWORD' and value != 'NOT SET':
            value = '*' * len(value)
        print(f"   {var}: {value}")
    print()
    
    if geoserver_url == 'NOT SET':
        print("❌ CRITICAL: GEOSERVER_URL not configured!")
        print("   Add GEOSERVER_URL environment variable to Django container")
        return False
    
    if admin_password == 'NOT SET':
        print("❌ CRITICAL: GEOSERVER_ADMIN_PASSWORD not configured!")
        print("   Add GEOSERVER_ADMIN_PASSWORD environment variable to Django container")
        return False
    
    # Test 1: Basic connectivity to GeoServer container
    print("🧪 Test 1: Basic GeoServer connectivity")
    try:
        # Try internal container connection first
        response = requests.get(f"{geoserver_url}/rest/about/version", 
                              auth=HTTPBasicAuth(admin_user, admin_password),
                              timeout=15)
        
        print(f"   Status Code: {response.status_code}")
        
        if response.status_code == 200:
            print("✅ GeoServer REST API is accessible")
            # Parse version info
            if 'GeoServer' in response.text:
                print("   GeoServer is responding correctly")
            else:
                print("   ⚠️ Unexpected response format")
        elif response.status_code == 401:
            print("❌ Authentication failed - check username/password")
            print(f"   Using: {admin_user}:{admin_password}")
            return False
        elif response.status_code == 404:
            print("❌ GeoServer REST API not found - GeoServer may not be fully started")
            return False
        else:
            print(f"❌ Unexpected status code: {response.status_code}")
            print(f"   Response: {response.text[:200]}")
            return False
            
    except requests.exceptions.ConnectionError as e:
        print(f"❌ Connection refused: {e}")
        print("   Possible causes:")
        print("   - GeoServer container not running")
        print("   - Wrong container name in GEOSERVER_URL")
        print("   - Network connectivity issue")
        return False
    except requests.exceptions.Timeout:
        print("❌ Connection timeout - GeoServer may be starting up")
        return False
    except Exception as e:
        print(f"❌ Connection error: {e}")
        return False
    
    # Test 2: Check workspace
    print("\n🧪 Test 2: Workspace management")
    try:
        workspace_url = f"{geoserver_url}/rest/workspaces/{workspace}"
        response = requests.get(workspace_url,
                              auth=HTTPBasicAuth(admin_user, admin_password),
                              timeout=10)
        
        if response.status_code == 200:
            print(f"✅ Workspace '{workspace}' exists")
        elif response.status_code == 404:
            print(f"⚠️ Workspace '{workspace}' does not exist - attempting to create...")
            
            # Create workspace
            create_data = f"""<?xml version="1.0" encoding="UTF-8"?>
<workspace>
    <name>{workspace}</name>
</workspace>"""
            
            create_response = requests.post(f"{geoserver_url}/rest/workspaces",
                                          data=create_data,
                                          headers={'Content-Type': 'application/xml'},
                                          auth=HTTPBasicAuth(admin_user, admin_password),
                                          timeout=10)
            
            if create_response.status_code == 201:
                print(f"✅ Successfully created workspace '{workspace}'")
            else:
                print(f"❌ Failed to create workspace: {create_response.status_code}")
                print(f"   Response: {create_response.text[:300]}")
                return False
        else:
            print(f"❌ Workspace check failed: {response.status_code}")
            print(f"   Response: {response.text[:200]}")
            return False
            
    except Exception as e:
        print(f"❌ Workspace test error: {e}")
        return False
    
    # Test 3: List all workspaces
    print("\n🧪 Test 3: List workspaces")
    try:
        response = requests.get(f"{geoserver_url}/rest/workspaces",
                              auth=HTTPBasicAuth(admin_user, admin_password),
                              timeout=10)
        
        if response.status_code == 200:
            # Try to parse JSON response
            try:
                data = response.json()
                workspaces = data.get('workspaces', {}).get('workspace', [])
                if isinstance(workspaces, dict):
                    workspaces = [workspaces]
                
                print(f"✅ Found {len(workspaces)} workspace(s):")
                for ws in workspaces:
                    print(f"   - {ws.get('name', 'Unknown')}")
            except:
                print("✅ Workspaces endpoint accessible (XML response)")
        else:
            print(f"❌ Failed to list workspaces: {response.status_code}")
            
    except Exception as e:
        print(f"❌ Workspace listing error: {e}")
    
    # Test 4: Container connectivity check
    print("\n🧪 Test 4: Container network check")
    try:
        # Test if we can reach GeoServer from Django container
        import socket
        
        # Parse hostname and port from URL
        if '://' in geoserver_url:
            url_parts = geoserver_url.split('://')[1]
        else:
            url_parts = geoserver_url
            
        if '/' in url_parts:
            host_port = url_parts.split('/')[0]
        else:
            host_port = url_parts
            
        if ':' in host_port:
            host, port = host_port.split(':')
            port = int(port)
        else:
            host = host_port
            port = 8080
            
        print(f"   Testing socket connection to {host}:{port}")
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((host, port))
        sock.close()
        
        if result == 0:
            print(f"✅ Socket connection to {host}:{port} successful")
        else:
            print(f"❌ Socket connection to {host}:{port} failed")
            print("   GeoServer container may not be running or accessible")
            
    except Exception as e:
        print(f"❌ Socket test error: {e}")
    
    print("\n🎉 GeoServer diagnostic completed!")
    print("\n📋 Next Steps if issues found:")
    print("1. Check container status: docker-compose -f docker-compose-adma.yml ps")
    print("2. Check GeoServer logs: docker-compose -f docker-compose-adma.yml logs geoserver")
    print("3. Restart GeoServer: docker-compose -f docker-compose-adma.yml restart geoserver")
    print("4. Verify environment variables in docker-compose-adma.yml")
    
    return True

if __name__ == "__main__":
    success = test_remote_geoserver()
    sys.exit(0 if success else 1)
