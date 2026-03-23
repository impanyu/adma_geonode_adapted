FROM node:24-bookworm-slim AS build

# Enable corepack for pnpm
RUN corepack enable

# Copy openclaw source
COPY openclaw/ /opt/openclaw/
WORKDIR /opt/openclaw

# Install dependencies and build
ENV CI=true
RUN pnpm install --frozen-lockfile && pnpm build && pnpm ui:build

# Prune dev dependencies
RUN pnpm prune --prod

# ---- Runtime stage ----
FROM node:24-bookworm-slim

RUN corepack enable && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        git \
        python3 \
        python3-pip \
        python3-venv \
        python3-dev \
        build-essential \
        libgdal-dev \
        gdal-bin \
        jq \
    && rm -rf /var/lib/apt/lists/*

# Allow pip install without --break-system-packages and pre-install common packages
RUN rm -f /usr/lib/python3.*/EXTERNALLY-MANAGED && \
    pip3 install pandas numpy matplotlib openpyxl requests

# Copy built openclaw
COPY --from=build /opt/openclaw /opt/openclaw
WORKDIR /opt/openclaw

# Copy ADMA skill into the skills directory
COPY adma_geonode_project/docker/openclaw/skills/adma/ /opt/openclaw/skills/adma/

# Copy config template and entrypoint
COPY adma_geonode_project/docker/openclaw/openclaw.json /opt/openclaw/openclaw.json.template
COPY adma_geonode_project/docker/openclaw/entrypoint.sh /opt/openclaw/entrypoint.sh
RUN chmod +x /opt/openclaw/entrypoint.sh

# Create workspace directory (will be mounted per-user)
RUN mkdir -p /workspace /root/.openclaw

EXPOSE 18789

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:18789/healthz || exit 1

ENTRYPOINT ["/opt/openclaw/entrypoint.sh"]
