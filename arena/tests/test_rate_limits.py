from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Barrier
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.core.management import call_command
from django.db import OperationalError, close_old_connections
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from arena.middleware import RateLimitMiddleware
from arena.models import RateLimitBucket
from arena.rate_limits import client_address, consume, purge_expired


@override_settings(RATE_LIMIT_TRUST_VERCEL_IP=False)
class RateLimitTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.downstream = Mock(side_effect=lambda request: HttpResponse('ok'))

    def post(self, path='/auth/start/', address='192.0.2.1', **headers):
        # A new middleware object represents another worker/cold invocation.
        return RateLimitMiddleware(self.downstream)(self.factory.post(path, REMOTE_ADDR=address, **headers))

    def test_auth_limit_survives_worker_recreation_and_local_cache_clear(self):
        for _ in range(20):
            self.assertEqual(self.post().status_code, 200)
            cache.clear()
        response = self.post()
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response['Retry-After']), 0)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(self.downstream.call_count, 20)
        self.assertEqual(RateLimitBucket.objects.get().count, 21)
        self.assertNotIn('192.0.2.1', RateLimitBucket.objects.get().key)

    def test_auth_paths_share_limit_but_other_address_and_post_scope_are_separate(self):
        paths = ['/login/', '/signup/', '/admin/login/', '/auth/complete/']
        for number in range(20):
            self.assertEqual(self.post(paths[number % len(paths)]).status_code, 200)
        self.assertEqual(self.post('/auth/start/').status_code, 429)
        self.assertEqual(self.post('/auth/start/', address='192.0.2.2').status_code, 200)
        self.assertEqual(self.post('/account/game/').status_code, 200)
        self.assertEqual(RateLimitBucket.objects.count(), 3)

    def test_normal_post_limit_remains_120(self):
        for _ in range(120):
            self.assertEqual(self.post('/team/create/').status_code, 200)
        self.assertEqual(self.post('/team/create/').status_code, 429)

    def test_fixed_window_expiry_and_denied_request_do_not_extend_window(self):
        start = timezone.now()
        with patch('arena.rate_limits.timezone.now', return_value=start):
            self.assertEqual(consume('192.0.2.1', 'test', 1, 60), (True, 60))
        with patch('arena.rate_limits.timezone.now', return_value=start + timedelta(seconds=15)):
            self.assertEqual(consume('192.0.2.1', 'test', 1, 60), (False, 45))
        with patch('arena.rate_limits.timezone.now', return_value=start + timedelta(seconds=60)):
            self.assertEqual(consume('192.0.2.1', 'test', 1, 60), (True, 60))
        self.assertEqual(RateLimitBucket.objects.count(), 1)
        self.assertEqual(RateLimitBucket.objects.get().count, 1)

    def test_denied_counter_saturates(self):
        for _ in range(10):
            consume('192.0.2.1', 'test', 2, 60)
        self.assertEqual(RateLimitBucket.objects.get().count, 3)

    def test_untrusted_forwarded_headers_do_not_choose_bucket(self):
        request = self.factory.post('/', REMOTE_ADDR='192.0.2.1', HTTP_X_FORWARDED_FOR='198.51.100.1',
                                    HTTP_X_VERCEL_FORWARDED_FOR='198.51.100.2')
        self.assertEqual(client_address(request), '192.0.2.1')

    @override_settings(RATE_LIMIT_TRUST_VERCEL_IP=True)
    def test_trusted_vercel_header_is_required_and_must_be_single_ip(self):
        request = self.factory.post('/', REMOTE_ADDR='192.0.2.1', HTTP_X_VERCEL_FORWARDED_FOR='2001:db8:0:0::1')
        self.assertEqual(client_address(request), '2001:db8::1')
        for value in ('', 'fake', '198.51.100.2, 192.0.2.1'):
            request.META['HTTP_X_VERCEL_FORWARDED_FOR'] = value
            self.assertEqual(client_address(request), 'unknown')

    def test_mapped_ipv6_cannot_create_second_bucket(self):
        request = self.factory.post('/', REMOTE_ADDR='::ffff:192.0.2.1')
        self.assertEqual(client_address(request), '192.0.2.1')

    def test_database_failure_fails_closed_without_running_view(self):
        with patch('arena.middleware.consume', side_effect=OperationalError('unavailable')):
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response['Retry-After'], '5')
        self.downstream.assert_not_called()

    def test_reads_and_only_exact_webhook_path_skip_public_counter(self):
        middleware = RateLimitMiddleware(self.downstream)
        with self.assertNumQueries(0):
            self.assertEqual(middleware(self.factory.get('/auth/status/')).status_code, 200)
            self.assertEqual(middleware(self.factory.post('/telegram/webhook/')).status_code, 200)
        self.assertEqual(self.post('/telegram/webhook/extra/').status_code, 200)
        self.assertEqual(RateLimitBucket.objects.count(), 1)

    def test_expired_cleanup_is_bounded_and_preserves_live_counters(self):
        now = timezone.now()
        RateLimitBucket.objects.bulk_create([
            RateLimitBucket(key='expired1', count=2, expires_at=now - timedelta(seconds=2)),
            RateLimitBucket(key='expired2', count=2, expires_at=now - timedelta(seconds=1)),
            RateLimitBucket(key='live', count=2, expires_at=now + timedelta(seconds=60)),
        ])
        self.assertEqual(purge_expired(batch_size=1), 1)
        output = StringIO()
        call_command('purge_rate_limits', stdout=output)
        self.assertIn('removed: 1', output.getvalue())
        self.assertEqual(list(RateLimitBucket.objects.values_list('key', flat=True)), ['live'])


class ConcurrentRateLimitTests(TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_first_requests_observe_one_shared_limit(self):
        # Runs on PostgreSQL; SQLite has no row locks and is tested separately.
        barrier = Barrier(8)

        def worker(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return consume('192.0.2.1', 'concurrency', 3, 60)[0]
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=8) as pool:
            decisions = list(pool.map(worker, range(8)))
        self.assertEqual(sum(decisions), 3)
        self.assertEqual(RateLimitBucket.objects.count(), 1)
        self.assertEqual(RateLimitBucket.objects.get().count, 4)
