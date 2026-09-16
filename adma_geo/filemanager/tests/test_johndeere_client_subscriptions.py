from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from filemanager.johndeere_client import JohnDeereClient, ResourceUnavailable


class TestApiBaseUrl(TestCase):
    """Moving to production must be a config change, not a code edit."""

    @override_settings(JD_API_BASE_URL='https://partnerapi.deere.com/platform')
    def test_base_url_comes_from_settings(self):
        client = JohnDeereClient('cid', 'csec', 'refresh')
        self.assertEqual(client.API_BASE_URL,
                         'https://partnerapi.deere.com/platform')

    @override_settings(JD_API_BASE_URL=None)
    def test_defaults_to_sandbox(self):
        client = JohnDeereClient('cid', 'csec', 'refresh')
        self.assertEqual(client.API_BASE_URL, JohnDeereClient.SANDBOX_BASE_URL)

    @override_settings(JD_API_BASE_URL='https://partnerapi.deere.com/platform')
    def test_ssrf_guard_follows_the_configured_base(self):
        # The guard on get_resource_by_link compares against the same base, so
        # production links must not be rejected as off-origin.
        client = JohnDeereClient('cid', 'csec', 'refresh')
        client.access_token = 'token'
        with patch.object(JohnDeereClient, '_make_request') as mock_req:
            mock_req.return_value = Mock(status_code=200, json=lambda: {'id': 'F1'},
                                         text='')
            data = client.get_resource_by_link(
                'https://partnerapi.deere.com/platform/organizations/1/fields/F1'
            )
        self.assertEqual(data['id'], 'F1')
        self.assertEqual(mock_req.call_args[0][1], '/organizations/1/fields/F1')


class TestSubscriptionClient(TestCase):
    def setUp(self):
        self.client = JohnDeereClient('cid', 'csec', 'refresh')
        self.client.access_token = 'token'  # skip refresh

    @patch.object(JohnDeereClient, '_make_request')
    def test_create_subscription_posts_correct_body(self, mock_req):
        mock_req.return_value = Mock(
            status_code=201,
            json=lambda: {'id': 'SUB-123'},
            text='',
        )
        result = self.client.create_subscription(
            event_type_id='field',
            target_uri='https://example.test/api/v1/webhooks/johndeere/',
            org_id='4193081',
        )
        self.assertEqual(result['id'], 'SUB-123')
        args, kwargs = mock_req.call_args
        self.assertEqual(args[0], 'POST')
        self.assertEqual(args[1], '/eventSubscriptions')
        body = kwargs['json']
        # DSS takes one event type per subscription, an https targetEndpoint,
        # and orgId as a filter — not a list of types with embedded credentials.
        self.assertEqual(body['eventTypeId'], 'field')
        self.assertEqual(body['targetEndpoint'],
                         {'targetType': 'https',
                          'uri': 'https://example.test/api/v1/webhooks/johndeere/'})
        self.assertEqual(body['filters'],
                         [{'key': 'orgId', 'values': ['4193081']}])
        self.assertEqual(body['status'], 'Active')
        self.assertNotIn('clientEndpoint', body)

    @patch.object(JohnDeereClient, '_make_request')
    def test_update_delivery_patches_authorization_header(self, mock_req):
        mock_req.return_value = Mock(
            status_code=200,
            json=lambda: {'authorizationHeaderValue': 'Basic abc'},
            content=b'{}',
            text='',
        )
        result = self.client.update_delivery(authorizationHeaderValue='Basic abc')
        args, kwargs = mock_req.call_args
        self.assertEqual(args[0], 'PATCH')
        self.assertEqual(args[1], '/eventSubscriptionDelivery')
        self.assertEqual(kwargs['json'], {'authorizationHeaderValue': 'Basic abc'})
        self.assertEqual(result['authorizationHeaderValue'], 'Basic abc')

    @patch.object(JohnDeereClient, '_make_request')
    def test_update_delivery_raises_on_error(self, mock_req):
        mock_req.return_value = Mock(status_code=400, text='bad', content=b'bad')
        with self.assertRaises(Exception):
            self.client.update_delivery(authorizationHeaderValue='Basic abc')

    @patch.object(JohnDeereClient, '_make_request')
    def test_pagination_follows_links_on_other_jd_hosts(self, mock_req):
        # JD returns nextPage on api.deere.com even when sandboxapi was called.
        # Stripping one configured base URL left the absolute URL in place, and
        # _make_request then glued it onto the base -- so page 2 was never
        # fetched correctly. Anything with more than 10 items hit this.
        page1 = Mock(status_code=200, text='', json=lambda: {
            'values': [{'id': 'A'}],
            'links': [{'rel': 'nextPage',
                       'uri': 'https://api.deere.com/platform/eventSubscriptions'
                              '?pageOffset=10&itemLimit=10'}],
        })
        page2 = Mock(status_code=200, text='',
                     json=lambda: {'values': [{'id': 'B'}], 'links': []})
        mock_req.side_effect = [page1, page2]

        result = self.client.list_subscriptions()
        self.assertEqual([s['id'] for s in result], ['A', 'B'])
        self.assertEqual(mock_req.call_args_list[1][0][1],
                         '/eventSubscriptions?pageOffset=10&itemLimit=10')

    def test_organization_needs_connection(self):
        # Only a 'connections' link means the org has not granted us access.
        self.assertTrue(JohnDeereClient.organization_needs_connection(
            {'links': [{'rel': 'connections', 'uri': 'https://connections/...'}]}
        ))
        self.assertFalse(JohnDeereClient.organization_needs_connection(
            {'links': [{'rel': 'self', 'uri': 'https://x'},
                       {'rel': 'connections', 'uri': 'https://y'}]}
        ))
        self.assertFalse(JohnDeereClient.organization_needs_connection(
            {'links': [{'rel': 'self', 'uri': 'https://x'}]}
        ))

    @patch.object(JohnDeereClient, '_make_request')
    def test_list_subscriptions_handles_pagination(self, mock_req):
        page1 = Mock(
            status_code=200,
            json=lambda: {
                'values': [{'id': 'A'}, {'id': 'B'}],
                'links': [
                    {'rel': 'nextPage',
                     'uri': 'https://sandboxapi.deere.com/platform/eventSubscriptions?offset=2'}
                ],
            },
            text='',
        )
        page2 = Mock(
            status_code=200,
            json=lambda: {'values': [{'id': 'C'}], 'links': []},
            text='',
        )
        mock_req.side_effect = [page1, page2]

        result = self.client.list_subscriptions()
        self.assertEqual([s['id'] for s in result], ['A', 'B', 'C'])

    @patch.object(JohnDeereClient, 'list_subscriptions')
    @patch.object(JohnDeereClient, '_make_request')
    def test_delete_subscription_terminates_via_put(self, mock_req, mock_list):
        # DSS answers 403 to DELETE; Terminated via PUT is the documented way
        # to switch a subscription off, and the PUT needs the whole object back.
        existing = {
            'id': 'SUB-1', 'eventTypeId': 'field', 'status': 'Active',
            'links': [{'rel': 'self', 'uri': 'https://x/eventSubscriptions/SUB-1'}],
        }
        mock_list.return_value = [existing]
        mock_req.return_value = Mock(status_code=204, text='')

        self.assertTrue(self.client.delete_subscription('SUB-1'))

        args, kwargs = mock_req.call_args
        self.assertEqual(args[0], 'PUT')
        self.assertEqual(args[1], '/eventSubscriptions/SUB-1')
        self.assertEqual(kwargs['json']['status'], 'Terminated')
        self.assertEqual(kwargs['json']['links'], existing['links'])

    @patch.object(JohnDeereClient, 'list_subscriptions', return_value=[])
    def test_delete_subscription_missing_returns_true(self, mock_list):
        self.assertTrue(self.client.delete_subscription('SUB-GONE'))

    @patch.object(JohnDeereClient, 'list_subscriptions')
    @patch.object(JohnDeereClient, '_make_request')
    def test_delete_subscription_error_returns_false(self, mock_req, mock_list):
        mock_list.return_value = [{'id': 'SUB-1', 'status': 'Active'}]
        mock_req.return_value = Mock(status_code=500, text='boom')
        self.assertFalse(self.client.delete_subscription('SUB-1'))

    @patch.object(JohnDeereClient, '_make_request')
    def test_get_resource_by_link_strips_base_url(self, mock_req):
        mock_req.return_value = Mock(
            status_code=200,
            json=lambda: {'id': 'F1'},
            text='',
        )
        # URI starting with the sandbox base URL
        uri = 'https://sandboxapi.deere.com/platform/organizations/4193081/fields/F1'
        data = self.client.get_resource_by_link(uri)
        self.assertEqual(data['id'], 'F1')
        args, _ = mock_req.call_args
        self.assertEqual(args[0], 'GET')
        self.assertEqual(args[1], '/organizations/4193081/fields/F1')

    @patch.object(JohnDeereClient, '_make_request')
    def test_get_resource_by_link_returns_none_only_on_404(self, mock_req):
        mock_req.return_value = Mock(status_code=404, text='not found')
        uri = 'https://sandboxapi.deere.com/platform/organizations/4193081/fields/F1'
        self.assertIsNone(self.client.get_resource_by_link(uri))

    @patch.object(JohnDeereClient, '_make_request')
    def test_get_resource_by_link_raises_on_server_error(self, mock_req):
        # A 500 must not read as "deleted": callers archive on None.
        mock_req.return_value = Mock(status_code=500, text='boom')
        uri = 'https://sandboxapi.deere.com/platform/organizations/4193081/fields/F1'
        with self.assertRaises(ResourceUnavailable):
            self.client.get_resource_by_link(uri)

    @patch.object(JohnDeereClient, '_make_request')
    def test_get_resource_by_link_accepts_api_deere_com(self, mock_req):
        # JD returns self links on api.deere.com whichever host was called, so
        # rejecting that host would make every link unusable.
        mock_req.return_value = Mock(status_code=200, json=lambda: {'id': 'F1'}, text='')
        data = self.client.get_resource_by_link(
            'https://api.deere.com/platform/organizations/1/fields/F1'
        )
        self.assertEqual(data['id'], 'F1')
        self.assertEqual(mock_req.call_args[0][1], '/organizations/1/fields/F1')

    @patch.object(JohnDeereClient, '_make_request')
    def test_get_resource_by_link_preserves_query_string(self, mock_req):
        mock_req.return_value = Mock(status_code=200, json=lambda: {}, text='')
        self.client.get_resource_by_link(
            'https://api.deere.com/platform/fields/F1?embed=boundaries'
        )
        self.assertEqual(mock_req.call_args[0][1], '/fields/F1?embed=boundaries')

    @patch.object(JohnDeereClient, '_make_request')
    def test_get_resource_by_link_rejects_foreign_hosts(self, mock_req):
        for uri in (
            'https://169.254.169.254/platform/latest/meta-data/',
            'http://sandboxapi.deere.com/platform/fields/F1',      # not https
            'https://evil.example/platform/fields/F1',
            'https://sandboxapi.deere.com.evil.example/platform/fields/F1',
            'https://sandboxapi.deere.com/elsewhere/fields/F1',    # outside /platform
            'https://sandboxapi.deere.com/platformX/fields/F1',    # prefix-match trap
        ):
            with self.subTest(uri=uri):
                with self.assertRaises(ResourceUnavailable):
                    self.client.get_resource_by_link(uri)
        mock_req.assert_not_called()
