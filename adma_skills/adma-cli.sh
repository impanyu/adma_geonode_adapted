#!/bin/bash
#
# ADMA CLI — Command-line interface for the ADMA platform
#
# Setup:
#   1. Copy this script somewhere in your PATH:
#      cp adma-cli.sh /usr/local/bin/adma && chmod +x /usr/local/bin/adma
#
#   2. Configure your credentials (choose one):
#      a) Environment variables:
#         export ADMA_TOKEN="your-api-token"
#         export ADMA_URL="https://adma.unl.edu"
#
#      b) Config file (~/.adma/config.json):
#         mkdir -p ~/.adma
#         echo '{"token":"your-api-token","url":"https://adma.unl.edu"}' > ~/.adma/config.json
#
#      c) Login interactively:
#         adma login
#
# Usage:
#   adma <command> [arguments]
#   adma help
#

set -euo pipefail

# ---- Config loading ----

CONFIG_DIR="${ADMA_CONFIG_DIR:-$HOME/.adma}"
CONFIG_FILE="${CONFIG_DIR}/config.json"

load_config() {
  # Priority: env vars > config file
  if [ -n "${ADMA_TOKEN:-}" ] && [ -n "${ADMA_URL:-}" ]; then
    TOKEN="$ADMA_TOKEN"
    BASE_URL="${ADMA_URL}/api/v1/"
    WEB_URL="$ADMA_URL"
    return
  fi

  if [ -f "$CONFIG_FILE" ]; then
    TOKEN=$(jq -r '.token // empty' "$CONFIG_FILE" 2>/dev/null || true)
    local url=$(jq -r '.url // empty' "$CONFIG_FILE" 2>/dev/null || true)
    if [ -n "$TOKEN" ] && [ -n "$url" ]; then
      BASE_URL="${url}/api/v1/"
      WEB_URL="$url"
      return
    fi
  fi

  # Check workspace credentials (for agent containers)
  if [ -f "/workspace/credentials/adma_token.json" ]; then
    TOKEN=$(jq -r '.token' /workspace/credentials/adma_token.json)
    BASE_URL=$(jq -r '.base_url' /workspace/credentials/adma_token.json)
    WEB_URL=$(jq -r '.web_url' /workspace/credentials/adma_token.json)
    return
  fi

  echo "Error: No ADMA credentials configured."
  echo ""
  echo "Set up credentials with one of:"
  echo "  1. adma login"
  echo "  2. export ADMA_TOKEN=your-token ADMA_URL=https://adma.unl.edu"
  echo "  3. echo '{\"token\":\"your-token\",\"url\":\"https://adma.unl.edu\"}' > ~/.adma/config.json"
  exit 1
}

api() {
  curl -s -H "Authorization: Token $TOKEN" "$@"
}

api_json() {
  curl -s -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" "$@"
}

# ---- Commands ----

cmd_login() {
  echo "ADMA Login"
  echo ""
  read -p "ADMA URL [https://adma.unl.edu]: " url
  url="${url:-https://adma.unl.edu}"
  read -p "Username: " username
  read -s -p "Password: " password
  echo ""

  RESULT=$(curl -s -X POST "${url}/api/v1/auth/token/" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$username\",\"password\":\"$password\"}")

  TOKEN_VAL=$(echo "$RESULT" | jq -r '.token // empty')
  if [ -z "$TOKEN_VAL" ]; then
    echo "Error: Login failed."
    echo "$RESULT" | jq '.' 2>/dev/null || echo "$RESULT"
    exit 1
  fi

  mkdir -p "$CONFIG_DIR"
  echo "{\"token\":\"$TOKEN_VAL\",\"url\":\"$url\",\"username\":\"$username\"}" > "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE"
  echo "Logged in as $username. Credentials saved to $CONFIG_FILE"
}

cmd_logout() {
  rm -f "$CONFIG_FILE"
  echo "Logged out. Credentials removed."
}

cmd_whoami() {
  load_config
  api "${BASE_URL}user/profile/" | jq '.'
}

cmd_stats() {
  load_config
  api "${BASE_URL}user/stats/" | jq '.'
}

cmd_files() {
  load_config
  local params="include_public=true"
  while [ $# -gt 0 ]; do
    case "$1" in
      --folder|-f) params="${params}&folder_id=$2"; shift 2 ;;
      --type|-t)   params="${params}&file_type=$2"; shift 2 ;;
      --own)       params=""; shift ;;
      *)           shift ;;
    esac
  done
  api "${BASE_URL}files/?${params}" | jq '.'
}

cmd_files_in_folder() {
  load_config
  if [ -z "${1:-}" ]; then echo "Usage: adma files-in-folder <folder_id>"; exit 1; fi
  api "${BASE_URL}files/?folder_id=$1&include_public=true" | jq '.'
}

cmd_file_metadata() {
  load_config
  if [ -z "${1:-}" ]; then echo "Usage: adma file-metadata <file_id>"; exit 1; fi
  api "${BASE_URL}files/$1/metadata/" | jq '.'
}

cmd_file_download() {
  load_config
  if [ -z "${1:-}" ] || [ -z "${2:-}" ]; then echo "Usage: adma file-download <file_id> <output_path>"; exit 1; fi
  api -o "$2" "${BASE_URL}files/$1/download/"
  echo "Downloaded to $2"
}

cmd_file_upload() {
  load_config
  if [ -z "${1:-}" ]; then echo "Usage: adma file-upload <file_path> [folder_id] [--public]"; exit 1; fi
  local filepath="$1"
  local folder_id="${2:-}"
  local is_public="false"
  [ "${3:-}" = "--public" ] && is_public="true"

  local cmd="curl -s -H \"Authorization: Token $TOKEN\" -F \"files=@$filepath\" -F \"is_public=$is_public\""
  [ -n "$folder_id" ] && cmd="$cmd -F \"folder_id=$folder_id\""
  cmd="$cmd \"${BASE_URL}files/upload/\""
  eval "$cmd" | jq '.'
}

cmd_file_update() {
  load_config
  if [ -z "${1:-}" ] || [ -z "${2:-}" ]; then echo "Usage: adma file-update <file_id> <json_body>"; exit 1; fi
  api_json -X PATCH -d "$2" "${BASE_URL}files/$1/update/" | jq '.'
}

cmd_file_delete() {
  load_config
  if [ -z "${1:-}" ]; then echo "Usage: adma file-delete <file_id>"; exit 1; fi
  api -X DELETE "${BASE_URL}files/$1/delete/" | jq '.'
}

cmd_folders() {
  load_config
  local params="include_public=true"
  while [ $# -gt 0 ]; do
    case "$1" in
      --parent|-p) params="${params}&parent_id=$2"; shift 2 ;;
      --own)       params=""; shift ;;
      *)           shift ;;
    esac
  done
  api "${BASE_URL}folders/?${params}" | jq '.'
}

cmd_subfolders() {
  load_config
  if [ -z "${1:-}" ]; then echo "Usage: adma subfolders <folder_id>"; exit 1; fi
  api "${BASE_URL}folders/?parent_id=$1&include_public=true" | jq '.'
}

cmd_folder_create() {
  load_config
  if [ -z "${1:-}" ]; then echo "Usage: adma folder-create <name> [parent_id] [--public]"; exit 1; fi
  local name="$1"
  local parent="${2:-null}"
  local is_public="false"
  [ "${3:-}" = "--public" ] && is_public="true"
  [ "$parent" != "null" ] && parent="\"$parent\""
  api_json -X POST -d "{\"name\":\"$name\",\"parent_id\":$parent,\"is_public\":$is_public}" "${BASE_URL}folders/create/" | jq '.'
}

cmd_folder_info() {
  load_config
  if [ -z "${1:-}" ]; then echo "Usage: adma folder-info <folder_id>"; exit 1; fi
  api "${BASE_URL}folders/$1/info/" | jq '.'
}

cmd_folder_download() {
  load_config
  if [ -z "${1:-}" ] || [ -z "${2:-}" ]; then echo "Usage: adma folder-download <folder_id> <output.zip>"; exit 1; fi
  api -o "$2" "${BASE_URL}folders/$1/download/"
  echo "Downloaded to $2"
}

cmd_folder_delete() {
  load_config
  if [ -z "${1:-}" ]; then echo "Usage: adma folder-delete <folder_id>"; exit 1; fi
  api -X DELETE "${BASE_URL}folders/$1/delete/" | jq '.'
}

cmd_maps() {
  load_config
  api "${BASE_URL}maps/?include_public=true" | jq '.'
}

cmd_map_create() {
  load_config
  if [ -z "${1:-}" ]; then echo "Usage: adma map-create <name> [description]"; exit 1; fi
  local desc="${2:-}"
  api_json -X POST -d "{\"name\":\"$1\",\"description\":\"$desc\"}" "${BASE_URL}maps/create/" | jq '.'
}

cmd_map_delete() {
  load_config
  if [ -z "${1:-}" ]; then echo "Usage: adma map-delete <map_id>"; exit 1; fi
  api -X DELETE "${BASE_URL}maps/$1/delete/" | jq '.'
}

cmd_tools() {
  load_config
  api "${BASE_URL}tools/" | jq '.'
}

cmd_tool_run() {
  load_config
  if [ -z "${1:-}" ] || [ -z "${2:-}" ]; then echo "Usage: adma tool-run <slug> <json_params>"; exit 1; fi
  api_json -X POST -d "$2" "${BASE_URL}tools/$1/run/" | jq '.'
}

cmd_tool_status() {
  load_config
  if [ -z "${1:-}" ] || [ -z "${2:-}" ]; then echo "Usage: adma tool-status <slug> <task_id>"; exit 1; fi
  api "${BASE_URL}tools/$1/status/$2/" | jq '.'
}

cmd_tool_wait() {
  load_config
  if [ -z "${1:-}" ] || [ -z "${2:-}" ]; then echo "Usage: adma tool-wait <slug> <task_id>"; exit 1; fi
  local slug="$1" task_id="$2"
  while true; do
    local result=$(api "${BASE_URL}tools/$slug/status/$task_id/")
    local status=$(echo "$result" | jq -r '.status')
    echo "Status: $status"
    [ "$status" = "SUCCESS" ] || [ "$status" = "FAILURE" ] && { echo "$result" | jq '.'; break; }
    sleep 5
  done
}

cmd_search() {
  load_config
  if [ -z "${1:-}" ]; then echo "Usage: adma search <keywords> [--type file|folder|map]"; exit 1; fi
  local query="$1"
  local type_param=""
  [ "${2:-}" = "--type" ] && [ -n "${3:-}" ] && type_param="&type=$3"
  api "${BASE_URL}search/?q=$(echo "$query" | sed 's/ /+/g')${type_param}" | jq '.'
}

cmd_web_url() {
  load_config
  echo "$WEB_URL"
}

cmd_tree() {
  load_config
  echo "ADMA Folder Tree"
  echo "================"
  local folders=$(api "${BASE_URL}folders/?include_public=true")
  echo "$folders" | jq -r '.folders[] | "\(.id)\t\(.name)\t\(.is_public)"' | while IFS=$'\t' read -r fid fname fpub; do
    local fcount=$(api "${BASE_URL}files/?folder_id=$fid&include_public=true" | jq -r '.count')
    local scount=$(api "${BASE_URL}folders/?parent_id=$fid&include_public=true" | jq -r '.count')
    local pub_tag=""
    [ "$fpub" = "true" ] && pub_tag=" [public]"
    echo "📁 $fname ($fcount files, $scount subfolders)$pub_tag"

    # Show subfolders
    api "${BASE_URL}folders/?parent_id=$fid&include_public=true" | jq -r '.folders[] | "\(.id)\t\(.name)\t\(.is_public)"' | while IFS=$'\t' read -r sid sname spub; do
      local sfcount=$(api "${BASE_URL}files/?folder_id=$sid&include_public=true" | jq -r '.count')
      local spub_tag=""
      [ "$spub" = "true" ] && spub_tag=" [public]"
      echo "  📂 $sname ($sfcount files)$spub_tag"
    done
  done
}

cmd_help() {
  cat << 'HELP'
ADMA CLI — Agricultural Data Management & Analytics

Setup:
  adma login                              Interactive login (saves to ~/.adma/config.json)
  adma logout                             Remove saved credentials

  Or set environment variables:
    export ADMA_TOKEN="your-token"
    export ADMA_URL="https://adma.unl.edu"

Account:
  adma whoami                             Show user profile
  adma stats                              Show storage statistics

Files:
  adma files                              List all files (own + public)
  adma files --folder <id>                Files in a specific folder
  adma files --type csv                   Filter by type (csv|gis|image|text|document|spreadsheet|other)
  adma files --own                        Only your own files (no public)
  adma files-in-folder <folder_id>        Files in a folder
  adma file-metadata <file_id>            Detailed file info
  adma file-download <file_id> <path>     Download a file
  adma file-upload <path> [folder_id]     Upload a file
  adma file-update <id> '{"name":"x"}'    Rename or change visibility
  adma file-delete <file_id>              Delete a file

Folders:
  adma folders                            List root folders (own + public)
  adma folders --parent <id>              List subfolders
  adma folders --own                      Only your own folders
  adma subfolders <folder_id>             List subfolders (shorthand)
  adma folder-create <name> [parent_id]   Create a folder
  adma folder-info <folder_id>            Folder details (file count, size)
  adma folder-download <id> <output.zip>  Download as ZIP
  adma folder-delete <folder_id>          Delete folder and contents
  adma tree                               Show full folder tree

Maps:
  adma maps                               List all maps
  adma map-create <name> [description]    Create a map
  adma map-delete <map_id>                Delete a map

Tools:
  adma tools                              List available analysis tools
  adma tool-run <slug> <json_params>      Run a tool (returns task_id)
  adma tool-status <slug> <task_id>       Check task status
  adma tool-wait <slug> <task_id>         Wait for tool completion

Search:
  adma search <keywords>                  Search files, folders, maps
  adma search <keywords> --type file      Search files only (file|folder|map)

Other:
  adma web-url                            Print the ADMA web URL
  adma help                               Show this help

Examples:
  adma login
  adma files --type gis
  adma search "corn yield" --type file
  adma file-download abc123-def456 ./data.csv
  adma folder-create "2025 Analysis"
  adma tool-run seeding-tool '{"file_id":"abc","output_folder_id":"def"}'
  adma tool-wait seeding-tool task-id-here
HELP
}

# ---- Main ----

case "${1:-help}" in
  login)           cmd_login ;;
  logout)          cmd_logout ;;
  whoami)          cmd_whoami ;;
  stats)           cmd_stats ;;
  files)           shift; cmd_files "$@" ;;
  files-in-folder) shift; cmd_files_in_folder "$@" ;;
  file-metadata)   shift; cmd_file_metadata "$@" ;;
  file-download)   shift; cmd_file_download "$@" ;;
  file-upload)     shift; cmd_file_upload "$@" ;;
  file-update)     shift; cmd_file_update "$@" ;;
  file-delete)     shift; cmd_file_delete "$@" ;;
  folders)         shift; cmd_folders "$@" ;;
  subfolders)      shift; cmd_subfolders "$@" ;;
  folder-create)   shift; cmd_folder_create "$@" ;;
  folder-info)     shift; cmd_folder_info "$@" ;;
  folder-download) shift; cmd_folder_download "$@" ;;
  folder-delete)   shift; cmd_folder_delete "$@" ;;
  maps)            cmd_maps ;;
  map-create)      shift; cmd_map_create "$@" ;;
  map-delete)      shift; cmd_map_delete "$@" ;;
  tools)           cmd_tools ;;
  tool-run)        shift; cmd_tool_run "$@" ;;
  tool-status)     shift; cmd_tool_status "$@" ;;
  tool-wait)       shift; cmd_tool_wait "$@" ;;
  search)          shift; cmd_search "$@" ;;
  web-url)         cmd_web_url ;;
  tree)            cmd_tree ;;
  help|--help|-h)  cmd_help ;;
  *)               echo "Unknown command: $1"; echo "Run 'adma help' for usage."; exit 1 ;;
esac
