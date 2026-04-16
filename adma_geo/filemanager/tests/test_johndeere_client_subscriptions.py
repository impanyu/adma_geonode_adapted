from unittest.mock import Mock, patch

from django.test import TestCase

from filemanager.johndeere_client import JohnDeereClient


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
            client_endpoint='https://example.test/api/v1/webhooks/johndeere/',
            username='u',
            password='p',
            event_type_ids=['fieldCreated', 'fieldUpdated'],
            org_id='4193081',
        )
        self.assertEqual(result['id'], 'SUB-123')
        args, kwargs = mock_req.call_args
        self.assertEqual(args[0], 'POST')
        self.assertEqual(args[1], '/eventSubscriptions')
        body = kwargs['json']
        self.assertEqual(body['clientEndpoint']['uri'],
                         'https://example.test/api/v1/webhooks/johndeere/')
        self.assertEqual(body['clientEndpoint']['username'], 'u')
        self.assertEqual(body['clientEndpoint']['password'], 'p')
        self.assertEqual(
            sorted(t for t in body['eventTypeIds']),
            ['fieldCreated', 'fieldUpdated'],
        )
        self.assertEqual(body['scopes'][0]['objectType'], 'organization')
        self.assertEqual(body['scopes'][0]['objectId'], '4193081')

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

    @patch.object(JohnDeereClient, '_make_request')
    def test_delete_subscription_204_returns_true(self, mock_req):
        mock_req.return_value = Mock(status_code=204, text='')
        self.assertTrue(self.client.delete_subscription('SUB-1'))
        mock_req.assert_called_once_with('DELETE', '/eventSubscriptions/SUB-1')

    @patch.object(JohnDeereClient, '_make_request')
    def test_delete_subscription_404_returns_true(self, mock_req):
        mock_req.return_value = Mock(status_code=404, text='not found')
        self.assertTrue(self.client.delete_subscription('SUB-1'))

    @patch.object(JohnDeereClient, '_make_request')
    def test_delete_subscription_other_returns_false(self, mock_req):
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
    def test_get_resource_by_link_returns_none_on_error(self, mock_req):
        mock_req.return_value = Mock(status_code=404, text='not found')
        uri = 'https://sandboxapi.deere.com/platform/organizations/4193081/fields/F1'
        self.assertIsNone(self.client.get_resource_by_link(uri))
