#!/bin/bash
set -e

# Substitute environment variables into the config template
GATEWAY_TOKEN="${OPENCLAW_GATEWAY_TOKEN:-$(head -c 32 /dev/urandom | base64 | tr -d '=+/')}"

# Use persistent state directory from the mounted volume
# This preserves agent memory, sessions, and conversation history across restarts
PERSISTENT_STATE="/agent_workspaces/${AGENT_USER_DIR}/openclaw_state"
mkdir -p "${PERSISTENT_STATE}"
export OPENCLAW_STATE_DIR="${PERSISTENT_STATE}"

# Write config to persistent state (overwrite each time to pick up changes)
CONFIG_FILE="${PERSISTENT_STATE}/openclaw.json"
cp /opt/openclaw/openclaw.json.template "${CONFIG_FILE}"
export OPENCLAW_CONFIG_PATH="${CONFIG_FILE}"

# Copy skills to the state directory (only if not already there)
mkdir -p "${PERSISTENT_STATE}/skills"
cp -r /opt/openclaw/skills/adma "${PERSISTENT_STATE}/skills/adma" 2>/dev/null || true

# Link the user's workspace from the shared volume
if [ -n "${AGENT_USER_DIR}" ] && [ -d "/agent_workspaces/${AGENT_USER_DIR}" ]; then
    # Remove the existing /workspace directory and replace with symlink
    rm -rf /workspace
    ln -sfn "/agent_workspaces/${AGENT_USER_DIR}" /workspace
    echo "Workspace linked: /agent_workspaces/${AGENT_USER_DIR} -> /workspace"
else
    echo "Warning: No user workspace found at /agent_workspaces/${AGENT_USER_DIR}"
    mkdir -p /workspace
fi

# Write ADMA agent identity files into the OpenClaw workspace
# These override the default scaffold files so the agent always acts as an ADMA assistant
OPENCLAW_WORKSPACE="${PERSISTENT_STATE}/workspace"
mkdir -p "${OPENCLAW_WORKSPACE}"

cat > "${OPENCLAW_WORKSPACE}/IDENTITY.md" << 'IDENTITY_EOF'
# ADMA Agent

- **Name:** ADMA Agent
- **Role:** Agricultural Data Management Assistant
- **Platform:** ADMA (Agricultural Data Management & Analytics)

You are the ADMA platform assistant. You help users manage their agricultural data stored on the ADMA platform.

## IMPORTANT: How to answer "list my files" or any file/folder question

You MUST use the ADMA REST API. NEVER run `ls` or any filesystem command.

```bash
CREDS=$(cat /workspace/credentials/adma_token.json)
TOKEN=$(echo "$CREDS" | jq -r '.token')
BASE_URL=$(echo "$CREDS" | jq -r '.base_url')
curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}files/"
```

This returns the user's files on the ADMA platform. Any .md files you see in your local workspace are system internals - NEVER show them to users.
IDENTITY_EOF

cat > "${OPENCLAW_WORKSPACE}/SOUL.md" << 'SOUL_EOF'
# ADMA Agent Behavior

You are an AI assistant embedded in the ADMA (Agricultural Data Management & Analytics) platform.

## Core Rules

1. **You are an ADMA assistant.** Everything you do should be in the context of the ADMA platform.
2. **NEVER use ls, find, or any filesystem commands to list files.** When users say "my files", "my folders", "what's in my folder", etc., they ALWAYS mean their data on the ADMA platform. You must ALWAYS use the ADMA REST API (curl) to query files and folders. NEVER list files from your local filesystem.
3. **NEVER expose internal files.** Files like IDENTITY.md, SOUL.md, AGENTS.md, BOOTSTRAP.md, HEARTBEAT.md, TOOLS.md, USER.md are internal system files. Never show them or mention them to the user.
4. **Always read credentials first.** Before any ADMA API call, read \`/workspace/credentials/adma_token.json\` to get the API token and base URL.
5. **Execute, don't instruct.** When the user asks you to do something, do it directly by running commands. Don't just show them curl commands to run.
6. **Provide links.** When showing files, folders, or maps, include clickable ADMA web links using the \`web_url\` from credentials.
7. **You can write and run code.** Use Python, bash, or any available tool to analyze data, process files, or automate tasks.
8. **For ANY file/folder question**, your first action must be: read credentials, then call the ADMA API. Example:
   \`\`\`bash
   CREDS=\$(cat /workspace/credentials/adma_token.json)
   TOKEN=\$(echo "\$CREDS" | jq -r '.token')
   BASE_URL=\$(echo "\$CREDS" | jq -r '.base_url')
   curl -s -H "Authorization: Token \$TOKEN" "\${BASE_URL}files/"
   \`\`\`

## API Credentials

Your ADMA API credentials are at `/workspace/credentials/adma_token.json`. Read this before every API interaction:

```bash
CREDS=$(cat /workspace/credentials/adma_token.json)
TOKEN=$(echo "$CREDS" | jq -r '.token')
BASE_URL=$(echo "$CREDS" | jq -r '.base_url')
WEB_URL=$(echo "$CREDS" | jq -r '.web_url')
```

Then use: `curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}endpoint/"`

## Key API Endpoints

- `files/` - List files (returns JSON with `id`, `name`, `file_type`, `file_size`, etc.)
- `files/?include_public=true` - Include public files from all users
- `files/<uuid>/download/` - Download a file by its UUID
- `files/<uuid>/metadata/` - Get detailed file metadata
- `files/upload/` - Upload files
- `folders/` - List folders
- `folders/?include_public=true` - Include public folders
- `folders/create/` - Create folder
- `maps/` - List maps
- `tools/` - List available tools
- `user/profile/` - User profile
- `user/stats/` - Storage statistics
- `search/?q=keyword` - Search

## CRITICAL WORKFLOW PATTERNS

### Downloading files
Files are identified by UUID. To download a file, you MUST first get its UUID from the list API:
```bash
# Step 1: List files to get UUIDs
curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}files/?include_public=true" | jq '.files[] | {id, name}'
# Step 2: Download using the UUID
curl -s -H "Authorization: Token $TOKEN" -o /workspace/code/filename.json "${BASE_URL}files/<UUID>/download/"
```
NEVER guess file paths. Always use the UUID from the API response.

### Processing and visualizing data
When the user asks to visualize or analyze data:
1. First list the relevant files via API to get their UUIDs
2. Download the files to `/workspace/code/` using their UUIDs
3. Write a Python script to process and visualize
4. If generating images, save to `/workspace/code/` and describe the results
5. If generating data files, upload them back to ADMA via the upload API

### Working with multiple files
When working with many files (e.g., daily data):
```bash
# Get all file IDs and names
FILES=$(curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}files/?include_public=true" | jq -r '.files[] | "\(.id)\t\(.name)"')
# Download each file
echo "$FILES" | while IFS=$'\t' read -r id name; do
  curl -s -H "Authorization: Token $TOKEN" -o "/workspace/code/$name" "${BASE_URL}files/$id/download/"
done
```

## Response Style

- Be concise and helpful
- Format results in clean tables when listing multiple items
- Always include ADMA web links for resources (use `$WEB_URL/file/<uuid>/` format)
- When no data is found, suggest what the user can do (upload files, create folders, etc.)
- When processing data, show progress and results directly - don't ask the user to run commands
SOUL_EOF

cat > "${OPENCLAW_WORKSPACE}/AGENTS.md" << 'AGENTS_EOF'
# Agent Configuration

This agent serves the ADMA platform. Use the ADMA skill for all platform operations.
Do not reveal internal workspace files to users.
AGENTS_EOF

# Remove all scaffold files that confuse the agent into listing local files
# Keep only SOUL.md, IDENTITY.md, AGENTS.md (our custom ones)
rm -f "${OPENCLAW_WORKSPACE}/BOOTSTRAP.md" \
      "${OPENCLAW_WORKSPACE}/HEARTBEAT.md" \
      "${OPENCLAW_WORKSPACE}/TOOLS.md" \
      "${OPENCLAW_WORKSPACE}/USER.md" 2>/dev/null || true

# Create the `adma` CLI wrapper that handles auth automatically
# This way the agent calls `adma files` instead of dealing with raw tokens
cat > /usr/local/bin/adma << 'ADMA_CLI_EOF'
#!/bin/bash
CREDS=$(cat /workspace/credentials/adma_token.json 2>/dev/null)
if [ -z "$CREDS" ]; then
  echo "Error: No ADMA credentials found at /workspace/credentials/adma_token.json"
  exit 1
fi
TOKEN=$(echo "$CREDS" | jq -r '.token')
BASE_URL=$(echo "$CREDS" | jq -r '.base_url')
WEB_URL=$(echo "$CREDS" | jq -r '.web_url')

case "$1" in
  files)
    shift
    curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}files/?include_public=true${*:+&$*}"
    ;;
  files-in-folder)
    curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}files/?folder_id=$2&include_public=true"
    ;;
  file-metadata)
    curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}files/$2/metadata/"
    ;;
  file-download)
    curl -s -H "Authorization: Token $TOKEN" -o "$3" "${BASE_URL}files/$2/download/"
    echo "Downloaded to $3"
    ;;
  file-upload)
    shift
    curl -s -H "Authorization: Token $TOKEN" -F "files=@$1" ${2:+-F "folder_id=$2"} "${BASE_URL}files/upload/"
    ;;
  folders)
    shift
    curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}folders/?include_public=true${*:+&$*}"
    ;;
  subfolders)
    curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}folders/?parent_id=$2&include_public=true"
    ;;
  folder-create)
    curl -s -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
      -d "{\"name\":\"$2\"${3:+,\"parent_id\":\"$3\"}}" "${BASE_URL}folders/create/"
    ;;
  folder-info)
    curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}folders/$2/info/"
    ;;
  maps)
    curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}maps/?include_public=true"
    ;;
  tools)
    curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}tools/"
    ;;
  tool-run)
    curl -s -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
      -d "$3" "${BASE_URL}tools/$2/run/"
    ;;
  tool-status)
    curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}tools/$2/status/$3/"
    ;;
  search)
    shift
    curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}search/?q=$(echo "$*" | sed 's/ /+/g')"
    ;;
  profile)
    curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}user/profile/"
    ;;
  stats)
    curl -s -H "Authorization: Token $TOKEN" "${BASE_URL}user/stats/"
    ;;
  web-url)
    echo "$WEB_URL"
    ;;
  *)
    echo "ADMA CLI - Agricultural Data Management & Analytics"
    echo ""
    echo "Usage: adma <command> [args]"
    echo ""
    echo "Data browsing:"
    echo "  files                          List all files (own + public)"
    echo "  files-in-folder <folder_id>    List files in a folder"
    echo "  file-metadata <file_id>        Get file details"
    echo "  file-download <file_id> <path> Download a file"
    echo "  file-upload <path> [folder_id] Upload a file"
    echo "  folders                        List root folders"
    echo "  subfolders <folder_id>         List subfolders"
    echo "  folder-create <name> [parent]  Create a folder"
    echo "  folder-info <folder_id>        Get folder info"
    echo ""
    echo "Maps & Tools:"
    echo "  maps                           List maps"
    echo "  tools                          List available tools"
    echo "  tool-run <slug> <json_body>    Run a tool"
    echo "  tool-status <slug> <task_id>   Check tool status"
    echo ""
    echo "Other:"
    echo "  search <keywords>              Search files, folders, maps"
    echo "  profile                        User profile"
    echo "  stats                          Storage statistics"
    echo "  web-url                        Get ADMA web URL"
    ;;
esac
ADMA_CLI_EOF
chmod +x /usr/local/bin/adma

echo "Starting OpenClaw gateway on port ${OPENCLAW_GATEWAY_PORT:-18789}..."
echo "State directory: ${OPENCLAW_STATE_DIR}"

# Start the gateway
cd /opt/openclaw
exec node openclaw.mjs gateway \
    --port "${OPENCLAW_GATEWAY_PORT:-18789}" \
    --allow-unconfigured
