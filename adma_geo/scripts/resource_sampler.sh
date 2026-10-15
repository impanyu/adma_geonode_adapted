#!/usr/bin/env bash
# Host and container resource sampler for the admin "Server resources" page.
#
# Runs on the Docker host (not in a container: it needs the host's network
# and disk counters and `docker stats`). Every INTERVAL seconds it appends one
# row to monitoring/host_metrics.csv and one row per running container to
# monitoring/container_metrics.csv. The code directory is bind-mounted into
# the Django container, so the admin page reads them from /app/monitoring.
# Rows older than KEEP_HOURS are trimmed once an hour.
#
# Keep it running from cron; flock makes the */5 entry a no-op while one is
# already sampling and restarts it after a crash or reboot:
#   */5 * * * * flock -n /tmp/adma_resource_sampler.lock bash /path/to/adma_geo/scripts/resource_sampler.sh
set -u
INTERVAL="${1:-5}"
KEEP_HOURS="${KEEP_HOURS:-24}"
OUT_DIR="${OUT_DIR:-$(cd "$(dirname "$0")/.." && pwd)/monitoring}"
HOST_CSV="$OUT_DIR/host_metrics.csv"
CONT_CSV="$OUT_DIR/container_metrics.csv"
mkdir -p "$OUT_DIR"

[ -s "$HOST_CSV" ] || echo "ts,cpu_pct,load1,mem_used_mb,mem_total_mb,swap_used_mb,disk_root_pct,net_rx_kbps,net_tx_kbps,disk_read_kbps,disk_write_kbps" > "$HOST_CSV"
[ -s "$CONT_CSV" ] || echo "ts,container,cpu_pct,mem_mb,mem_pct,net_io,block_io,pids" > "$CONT_CSV"

read_cpu() { awk '/^cpu /{print $2+$3+$4+$7+$8, $5+$6}' /proc/stat; }
# Physical interfaces only: container veths would count the same bytes twice.
read_net() { awk -F'[: ]+' 'NR>2 && $2!="lo" && $2!~/^(veth|docker|br-|tap|slirp)/ {rx+=$3; tx+=$11} END{print rx+0, tx+0}' /proc/net/dev; }
read_dsk() { awk '$3 ~ /^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+)$/ {r+=$6; w+=$10} END{print r*512, w*512}' /proc/diskstats; }

trim() {  # keep rows newer than KEEP_HOURS; ISO timestamps compare as strings
  local cutoff file
  cutoff=$(date -u -d "-$KEEP_HOURS hours" +%Y-%m-%dT%H:%M:%SZ)
  for file in "$HOST_CSV" "$CONT_CSV"; do
    awk -F, -v c="$cutoff" 'NR==1 || $1>=c' "$file" > "$file.tmp" && mv -f "$file.tmp" "$file"
  done
}

set -- $(read_cpu); pb=$1; pi=$2
set -- $(read_net); prx=$1; ptx=$2
set -- $(read_dsk); pdr=$1; pdw=$2
n=0
while true; do
  sleep "$INTERVAL"
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  set -- $(read_cpu); db=$(($1 - pb)); di=$(($2 - pi)); pb=$1; pi=$2
  cpu=$(awk -v b=$db -v i=$di 'BEGIN{printf "%.1f", (b+i) ? 100*b/(b+i) : 0}')
  set -- $(read_net); rx=$(( ($1 - prx) * 8 / 1000 / INTERVAL )); tx=$(( ($2 - ptx) * 8 / 1000 / INTERVAL )); prx=$1; ptx=$2
  set -- $(read_dsk); dr=$(( ($1 - pdr) / 1024 / INTERVAL )); dw=$(( ($2 - pdw) / 1024 / INTERVAL )); pdr=$1; pdw=$2
  mem=$(free -m | awk '/^Mem:/{u=$3; t=$2} /^Swap:/{s=$3} END{print u","t","s}')
  load=$(cut -d' ' -f1 /proc/loadavg)
  disk=$(df --output=pcent / | tail -1 | tr -dc 0-9)
  echo "$ts,$cpu,$load,$mem,$disk,$rx,$tx,$dr,$dw" >> "$HOST_CSV"
  docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}},{{.NetIO}},{{.BlockIO}},{{.PIDs}}' 2>/dev/null |
    awk -F, -v ts="$ts" '{
      split($3, m, " / "); v = m[1]; mb = v + 0
      if (v ~ /GiB/) mb *= 1024; else if (v ~ /KiB/) mb /= 1024; else if (v !~ /MiB/) mb /= 1048576
      gsub("%", "", $2); gsub("%", "", $4)
      printf "%s,%s,%s,%.1f,%s,%s,%s,%s\n", ts, $1, $2, mb, $4, $5, $6, $7 }' >> "$CONT_CSV"
  n=$((n + 1))
  (( n % (3600 / INTERVAL) == 0 )) && trim
done
