import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from filemanager import server_resources

User = get_user_model()

HOST_HEADER = ('ts,cpu_pct,load1,mem_used_mb,mem_total_mb,swap_used_mb,disk_root_pct,'
               'net_rx_kbps,net_tx_kbps,disk_read_kbps,disk_write_kbps\n')
CONT_HEADER = 'ts,container,cpu_pct,mem_mb,mem_pct,net_io,block_io,pids\n'
NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def iso(seconds_ago):
    return datetime.fromtimestamp(NOW - seconds_ago, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class MetricsDirMixin:
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix='adma-res-')
        self.addCleanup(shutil.rmtree, self.dir)
        override = override_settings(RESOURCE_METRICS_DIR=self.dir)
        override.enable()
        self.addCleanup(override.disable)

    def write(self, host_rows, container_rows):
        with open(os.path.join(self.dir, 'host_metrics.csv'), 'w') as handle:
            handle.write(HOST_HEADER + ''.join(host_rows))
        with open(os.path.join(self.dir, 'container_metrics.csv'), 'w') as handle:
            handle.write(CONT_HEADER + ''.join(container_rows))


class CollectTests(MetricsDirMixin, TestCase):
    def test_reads_the_window_and_reports_the_newest_sample(self):
        host = [f'{iso(s)},{s / 10:.1f},0.5,4000,16000,0,63,100,200,10,20\n' for s in range(2000, -1, -5)]
        self.write(host, [])
        data = server_resources.collect(15, now=NOW)
        self.assertEqual(data['last_sample'], iso(0))
        self.assertEqual(data['sample_age_seconds'], 0)
        self.assertEqual(data['latest']['disk_root_pct'], 63)
        # 15 minutes of 5-second samples, nothing older.
        self.assertEqual(data['bucket_seconds'], 5)
        self.assertEqual(len(data['labels']), 15 * 12 + 1)
        self.assertLessEqual(max(data['host']['cpu_pct']), 90.0)

    def test_long_windows_are_averaged_into_buckets(self):
        host = [f'{iso(s)},{10 if s % 10 else 30},1,1,1,0,1,1,1,1,1\n' for s in range(3600, -1, -5)]
        self.write(host, [])
        data = server_resources.collect(60, now=NOW)
        self.assertEqual(data['bucket_seconds'], 15)
        self.assertLessEqual(len(data['labels']), server_resources.MAX_POINTS + 1)
        # Each bucket mixes the 10s and 30s rows, so no bucket is either extreme.
        self.assertTrue(all(10 < v < 30 for v in data['host']['cpu_pct'][1:-1]))

    def test_small_containers_are_summed_as_other_and_only_running_ones_listed(self):
        rows = []
        for s in (10, 5, 0):
            for i in range(10):
                rows.append(f'{iso(s)},adma_geo_c{i},1.0,{(i + 1) * 100},1,1kB / 1kB,0B / 0B,3\n')
        rows.append(f'{iso(10)},stopped_one,5.0,9999,1,1kB / 1kB,0B / 0B,3\n')
        self.write([f'{iso(s)},1,1,1,1,0,1,1,1,1,1\n' for s in (10, 5, 0)], rows)
        data = server_resources.collect(15, now=NOW)
        names = list(data['containers'])
        self.assertEqual(len(names), server_resources.CHARTED_CONTAINERS + 1)
        self.assertEqual(names[0], 'stopped_one')                   # biggest first
        self.assertEqual(names[-1], 'other (3)')
        self.assertEqual(data['containers']['other (3)']['mem'][-1], 100 + 200 + 300)
        self.assertEqual(len(data['running']), 10)
        self.assertNotIn('stopped_one', [c['name'] for c in data['running']])
        self.assertEqual(data['running'][0]['name'], 'adma_geo_c9')

    def test_missing_files_mean_no_data_rather_than_an_error(self):
        data = server_resources.collect(15, now=NOW)
        self.assertIsNone(data['latest'])
        self.assertEqual(data['labels'], [])
        self.assertEqual(data['running'], [])


class RowsSinceTests(MetricsDirMixin, TestCase):
    def test_backward_reading_matches_a_full_read_across_block_edges(self):
        host = [f'{iso(s)},1,1,1,1,0,1,1,1,1,1\n' for s in range(600, -1, -5)]
        self.write(host, [])
        path = os.path.join(self.dir, 'host_metrics.csv')
        for block in (7, 64, 100, 4096):
            rows = server_resources._rows_since(path, iso(300), block=block)
            self.assertEqual([r[0] for r in rows], [iso(s) for s in range(300, -1, -5)], block)


class PageAccessTests(MetricsDirMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser('root', 'r@x.com', 'pw')
        self.staff = User.objects.create_user('staffer', 's@x.com', 'pw', is_staff=True)
        self.page = reverse('admin_server_resources')
        self.data = reverse('admin_server_resources_data')

    def test_anonymous_is_sent_to_the_admin_login(self):
        for url in (self.page, self.data):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse('admin:login'), response['Location'])

    def test_staff_who_are_not_superusers_are_refused(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.page).status_code, 403)
        self.assertEqual(self.client.get(self.data).status_code, 403)
        self.assertNotContains(self.client.get(reverse('admin:index')), self.page)

    def test_superuser_sees_the_page_the_data_and_the_index_link(self):
        self.client.force_login(self.superuser)
        page = self.client.get(self.page)
        self.assertContains(page, 'Server resources')
        self.assertContains(page, 'integrity="sha384-')
        self.assertContains(self.client.get(reverse('admin:index')), self.page)
        response = self.client.get(self.data, {'minutes': '9999'})   # not offered: falls back
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['window_minutes'], 15)
        self.assertIn('no-store', response['Cache-Control'])


class SamplerScriptTests(TestCase):
    def test_the_sampler_is_valid_bash(self):
        path = os.path.join(settings.BASE_DIR, 'scripts', 'resource_sampler.sh')
        self.assertEqual(subprocess.run(['bash', '-n', path]).returncode, 0)
