"""
Admin "Server resources" page: host and container usage over time.

scripts/resource_sampler.sh runs on the Docker host and appends to two CSVs
in RESOURCE_METRICS_DIR (the code directory is bind-mounted, so the host's
adma_geo/monitoring/ is /app/monitoring here). This module only reads them:
Django has no access to the host's counters or to Docker, and should not.

The page polls the JSON view. Rows are averaged into time buckets so a
24-hour window draws about as many points as a 15-minute one.
"""
import csv
import io
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import render

WINDOWS = (15, 60, 360, 1440)          # minutes offered on the page
MAX_POINTS = 240                       # per series, after bucketing
CHARTED_CONTAINERS = 8                 # the rest are summed as "other"
HOST_FIELDS = ('cpu_pct', 'load1', 'mem_used_mb', 'mem_total_mb', 'swap_used_mb', 'disk_root_pct',
               'net_rx_kbps', 'net_tx_kbps', 'disk_read_kbps', 'disk_write_kbps')


def metrics_dir():
    return getattr(settings, 'RESOURCE_METRICS_DIR', os.path.join(settings.BASE_DIR, 'monitoring'))


def _epoch(ts):
    return datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()


def _rows_since(path, cutoff, block=1 << 16):
    """Rows of a time-ordered CSV whose first column is at or after ``cutoff``.

    Reads backwards from the end, so a short window costs a short read even
    when the file holds a full day of container samples.
    """
    try:
        handle = open(path, 'rb')
    except FileNotFoundError:
        return []
    with handle:
        handle.seek(0, os.SEEK_END)
        pos = handle.tell()
        data = b''
        while pos > 0:
            step = min(block, pos)
            pos -= step
            handle.seek(pos)
            data = handle.read(step) + data
            lines = data.split(b'\n', 2)
            if pos and len(lines) < 3:
                continue                   # no complete line yet
            first = lines[1 if pos else 0][:20].decode(errors='replace')
            if first[:1].isdigit() and first < cutoff:
                break
    rows = []
    for row in csv.reader(io.StringIO(data.decode(errors='replace'))):
        if row and row[0][:1].isdigit() and row[0] >= cutoff and len(row[0]) == 20:
            rows.append(row)
    return rows


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else None


def collect(minutes, now=None):
    now = now or time.time()
    cutoff = datetime.fromtimestamp(now - minutes * 60, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    bucket = max(5, (minutes * 60) // MAX_POINTS)
    directory = metrics_dir()

    host_rows = _rows_since(os.path.join(directory, 'host_metrics.csv'), cutoff)
    cont_rows = _rows_since(os.path.join(directory, 'container_metrics.csv'), cutoff)

    host_buckets = defaultdict(lambda: defaultdict(list))
    for row in host_rows:
        key = int(_epoch(row[0]) // bucket)
        for name, value in zip(HOST_FIELDS, row[1:]):
            host_buckets[key][name].append(_number(value))

    # Containers: average each one per sample time first, so "other" is a sum
    # of averages rather than an average of unrelated containers.
    per_name = defaultdict(lambda: defaultdict(lambda: {'cpu': [], 'mem': []}))
    latest = {}
    for row in cont_rows:
        if len(row) != 8:
            continue
        ts, name, cpu, mem, mem_pct, net_io, block_io, pids = row
        key = int(_epoch(ts) // bucket)
        per_name[name][key]['cpu'].append(_number(cpu))
        per_name[name][key]['mem'].append(_number(mem))
        latest[name] = {'name': name, 'cpu_pct': _number(cpu), 'mem_mb': _number(mem),
                        'mem_pct': _number(mem_pct), 'net_io': net_io, 'block_io': block_io,
                        'pids': pids, 'ts': ts}
    # Only containers seen in the newest sample count as running.
    newest = max((c['ts'] for c in latest.values()), default=None)
    running = sorted((c for c in latest.values() if c['ts'] == newest), key=lambda c: -(c['mem_mb'] or 0))

    keys = sorted(host_buckets)
    labels = [datetime.fromtimestamp((k + 1) * bucket, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') for k in keys]
    host = {name: [_mean(host_buckets[k][name]) for k in keys] for name in HOST_FIELDS}

    def average_memory(name):
        return _mean([_mean(b['mem']) for b in per_name[name].values()]) or 0

    by_size = sorted(per_name, key=average_memory, reverse=True)
    charted, rest = by_size[:CHARTED_CONTAINERS], by_size[CHARTED_CONTAINERS:]
    containers = {}
    for name in charted:
        containers[name] = {
            'cpu': [_mean(per_name[name][k]['cpu']) if k in per_name[name] else None for k in keys],
            'mem': [_mean(per_name[name][k]['mem']) if k in per_name[name] else None for k in keys],
        }
    if rest:
        containers[f'other ({len(rest)})'] = {
            field: [round(sum(_mean(per_name[n][k][field]) or 0 for n in rest if k in per_name[n]), 2)
                    for k in keys]
            for field in ('cpu', 'mem')
        }

    last_ts = host_rows[-1][0] if host_rows else None
    latest_host = dict(zip(HOST_FIELDS, map(_number, host_rows[-1][1:]))) if host_rows else None
    return {
        'window_minutes': minutes,
        'bucket_seconds': bucket,
        'labels': labels,
        'host': host,
        'latest': latest_host,
        'last_sample': last_ts,
        'sample_age_seconds': round(now - _epoch(last_ts)) if last_ts else None,
        'containers': containers,
        'running': running,
        'cpu_count': os.cpu_count(),
    }


def _superuser_only(request):
    # admin_view has already required a staff login; host details are for
    # superusers, not every staff account.
    if not request.user.is_superuser:
        raise PermissionDenied


def resources_page(request):
    _superuser_only(request)
    context = {**admin.site.each_context(request), 'title': 'Server resources', 'windows': WINDOWS}
    return render(request, 'admin/server_resources.html', context)


def resources_data(request):
    _superuser_only(request)
    try:
        minutes = int(request.GET.get('minutes', 15))
    except ValueError:
        minutes = 15
    if minutes not in WINDOWS:
        minutes = 15
    # admin_view marks the response never-cache, so polling always gets fresh data.
    return JsonResponse(collect(minutes))
