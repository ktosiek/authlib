import time
from copy import deepcopy
from unittest import TestCase, mock
from authlib.common.security import generate_token
from authlib.common.urls import url_encode, add_params_to_uri
from authlib.integrations.requests_client import OAuth2Session, OAuthError
from authlib.oauth2.rfc6749 import MismatchingStateException
from authlib.oauth2.rfc7523 import ClientSecretJWT, PrivateKeyJWT
from ..util import read_key_file


def mock_json_response(payload):
    def fake_send(r, **kwargs):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json = lambda: payload
        return resp
    return fake_send


def mock_assertion_response(ctx, session):
    def fake_send(r, **kwargs):
        ctx.assertIn('client_assertion=', r.body)
        ctx.assertIn('client_assertion_type=', r.body)
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json = lambda: ctx.token
        return resp

    session.send = fake_send


class OAuth2SessionTest(TestCase):

    def setUp(self):
        self.token = {
            'token_type': 'Bearer',
            'access_token': 'a',
            'refresh_token': 'b',
            'expires_in': '3600',
            'expires_at': int(time.time()) + 3600,
        }
        self.client_id = 'foo'

    def test_invalid_token_type(self):
        token = {
            'token_type': 'invalid',
            'access_token': 'a',
            'refresh_token': 'b',
            'expires_in': '3600',
            'expires_at': int(time.time()) + 3600,
        }
        with OAuth2Session(self.client_id, token=token) as sess:
            self.assertRaises(OAuthError, sess.get, 'https://i.b')

    def test_add_token_to_header(self):
        token = 'Bearer ' + self.token['access_token']

        def verifier(r, **kwargs):
            auth_header = r.headers.get(str('Authorization'), None)
            self.assertEqual(auth_header, token)
            resp = mock.MagicMock()
            return resp

        sess = OAuth2Session(client_id=self.client_id, token=self.token)
        sess.send = verifier
        sess.get('https://i.b')

    def test_add_token_to_body(self):
        def verifier(r, **kwargs):
            self.assertIn(self.token['access_token'], r.body)
            resp = mock.MagicMock()
            return resp

        sess = OAuth2Session(
            client_id=self.client_id,
            token=self.token,
            token_placement='body'
        )
        sess.send = verifier
        sess.post('https://i.b')

    def test_add_token_to_uri(self):
        def verifier(r, **kwargs):
            self.assertIn(self.token['access_token'], r.url)
            resp = mock.MagicMock()
            return resp

        sess = OAuth2Session(
            client_id=self.client_id,
            token=self.token,
            token_placement='uri'
        )
        sess.send = verifier
        sess.get('https://i.b')

    def test_create_authorization_url(self):
        url = 'https://example.com/authorize?foo=bar'

        sess = OAuth2Session(client_id=self.client_id)
        auth_url, state = sess.create_authorization_url(url)
        self.assertIn(state, auth_url)
        self.assertIn(self.client_id, auth_url)
        self.assertIn('response_type=code', auth_url)

        sess = OAuth2Session(client_id=self.client_id, prompt='none')
        auth_url, state = sess.create_authorization_url(
            url, state='foo', redirect_uri='https://i.b', scope='profile')
        self.assertEqual(state, 'foo')
        self.assertIn('i.b', auth_url)
        self.assertIn('profile', auth_url)
        self.assertIn('prompt=none', auth_url)

    def test_code_challenge(self):
        sess = OAuth2Session(client_id=self.client_id, code_challenge_method='S256')

        url = 'https://example.com/authorize'
        auth_url, _ = sess.create_authorization_url(
            url, code_verifier=generate_token(48))
        self.assertIn('code_challenge', auth_url)
        self.assertIn('code_challenge_method=S256', auth_url)

    def test_token_from_fragment(self):
        sess = OAuth2Session(self.client_id)
        response_url = 'https://i.b/callback#' + url_encode(self.token.items())
        self.assertEqual(sess.token_from_fragment(response_url), self.token)
        token = sess.fetch_token(authorization_response=response_url)
        self.assertEqual(token, self.token)

    def test_fetch_token_post(self):
        url = 'https://example.com/token'

        def fake_send(r, **kwargs):
            self.assertIn('code=v', r.body)
            self.assertIn('client_id=', r.body)
            self.assertIn('grant_type=authorization_code', r.body)
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.json = lambda: self.token
            return resp

        sess = OAuth2Session(client_id=self.client_id)
        sess.send = fake_send
        self.assertEqual(
            sess.fetch_token(
                url, authorization_response='https://i.b/?code=v'),
            self.token)

        sess = OAuth2Session(
            client_id=self.client_id,
            token_endpoint_auth_method='none',
        )
        sess.send = fake_send
        token = sess.fetch_token(url, code='v')
        self.assertEqual(token, self.token)

        error = {'error': 'invalid_request'}
        sess = OAuth2Session(client_id=self.client_id, token=self.token)
        sess.send = mock_json_response(error)
        self.assertRaises(OAuthError, sess.fetch_access_token, url)

    def test_fetch_token_get(self):
        url = 'https://example.com/token'

        def fake_send(r, **kwargs):
            self.assertIn('code=v', r.url)
            self.assertIn('grant_type=authorization_code', r.url)
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.json = lambda: self.token
            return resp

        sess = OAuth2Session(client_id=self.client_id)
        sess.send = fake_send
        token = sess.fetch_token(
            url, authorization_response='https://i.b/?code=v', method='GET')
        self.assertEqual(token, self.token)

        sess = OAuth2Session(
            client_id=self.client_id,
            token_endpoint_auth_method='none',
        )
        sess.send = fake_send
        token = sess.fetch_token(url, code='v', method='GET')
        self.assertEqual(token, self.token)

        token = sess.fetch_token(url + '?q=a', code='v', method='GET')
        self.assertEqual(token, self.token)

    def test_token_auth_method_client_secret_post(self):
        url = 'https://example.com/token'

        def fake_send(r, **kwargs):
            self.assertIn('code=v', r.body)
            self.assertIn('client_id=', r.body)
            self.assertIn('client_secret=bar', r.body)
            self.assertIn('grant_type=authorization_code', r.body)
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.json = lambda: self.token
            return resp

        sess = OAuth2Session(
            client_id=self.client_id,
            client_secret='bar',
            token_endpoint_auth_method='client_secret_post',
        )
        sess.send = fake_send
        token = sess.fetch_token(url, code='v')
        self.assertEqual(token, self.token)

    def test_access_token_response_hook(self):
        url = 'https://example.com/token'

        def access_token_response_hook(resp):
            self.assertEqual(resp.json(), self.token)
            return resp

        sess = OAuth2Session(client_id=self.client_id, token=self.token)
        sess.register_compliance_hook(
            'access_token_response',
            access_token_response_hook
        )
        sess.send = mock_json_response(self.token)
        self.assertEqual(sess.fetch_token(url), self.token)

    def test_password_grant_type(self):
        url = 'https://example.com/token'

        def fake_send(r, **kwargs):
            self.assertIn('username=v', r.body)
            self.assertIn('grant_type=password', r.body)
            self.assertIn('scope=profile', r.body)
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.json = lambda: self.token
            return resp

        sess = OAuth2Session(client_id=self.client_id, scope='profile')
        sess.send = fake_send
        token = sess.fetch_token(url, username='v', password='v')
        self.assertEqual(token, self.token)

    def test_client_credentials_type(self):
        url = 'https://example.com/token'

        def fake_send(r, **kwargs):
            self.assertIn('grant_type=client_credentials', r.body)
            self.assertIn('scope=profile', r.body)
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.json = lambda: self.token
            return resp

        sess = OAuth2Session(
            client_id=self.client_id,
            client_secret='v',
            scope='profile',
        )
        sess.send = fake_send
        token = sess.fetch_token(url)
        self.assertEqual(token, self.token)

    def test_cleans_previous_token_before_fetching_new_one(self):
        """Makes sure the previous token is cleaned before fetching a new one.
        The reason behind it is that, if the previous token is expired, this
        method shouldn't fail with a TokenExpiredError, since it's attempting
        to get a new one (which shouldn't be expired).
        """
        now = int(time.time())
        new_token = deepcopy(self.token)
        past = now - 7200
        self.token['expires_at'] = past
        new_token['expires_at'] = now + 3600
        url = 'https://example.com/token'

        with mock.patch('time.time', lambda: now):
            sess = OAuth2Session(client_id=self.client_id, token=self.token)
            sess.send = mock_json_response(new_token)
            self.assertEqual(sess.fetch_token(url), new_token)

    def test_mis_match_state(self):
        sess = OAuth2Session('foo')
        self.assertRaises(
            MismatchingStateException,
            sess.fetch_token,
            'https://i.b/token',
            authorization_response='https://i.b/no-state?code=abc',
            state='somestate',
        )

    def test_token_status(self):
        token = dict(access_token='a', token_type='bearer', expires_at=100)
        sess = OAuth2Session('foo', token=token)

        self.assertTrue(sess.token.is_expired)

    def test_token_expired(self):
        token = dict(access_token='a', token_type='bearer', expires_at=100)
        sess = OAuth2Session('foo', token=token)
        self.assertRaises(
            OAuthError,
            sess.get,
            'https://i.b/token',
        )

    def test_missing_token(self):
        sess = OAuth2Session('foo')
        self.assertRaises(
            OAuthError,
            sess.get,
            'https://i.b/token',
        )

    def test_register_compliance_hook(self):
        sess = OAuth2Session('foo')
        self.assertRaises(
            ValueError,
            sess.register_compliance_hook,
            'invalid_hook',
            lambda o: o,
        )

        def protected_request(url, headers, data):
            self.assertIn('Authorization', headers)
            return url, headers, data

        sess = OAuth2Session('foo', token=self.token)
        sess.register_compliance_hook(
            'protected_request',
            protected_request,
        )
        sess.send = mock_json_response({'name': 'a'})
        sess.get('https://i.b/user')

    def test_auto_refresh_token(self):

        def _update_token(token, refresh_token=None, access_token=None):
            self.assertEqual(refresh_token, 'b')
            self.assertEqual(token, self.token)

        update_token = mock.Mock(side_effect=_update_token)
        old_token = dict(
            access_token='a', refresh_token='b',
            token_type='bearer', expires_at=100
        )
        sess = OAuth2Session(
            'foo', token=old_token,
            token_endpoint='https://i.b/token',
            update_token=update_token,
        )
        sess.send = mock_json_response(self.token)
        sess.get('https://i.b/user')
        self.assertTrue(update_token.called)

    def test_auto_refresh_token2(self):

        def _update_token(token, refresh_token=None, access_token=None):
            self.assertEqual(access_token, 'a')
            self.assertEqual(token, self.token)

        update_token = mock.Mock(side_effect=_update_token)
        old_token = dict(
            access_token='a',
            token_type='bearer',
            expires_at=100
        )

        sess = OAuth2Session(
            'foo', token=old_token,
            token_endpoint='https://i.b/token',
            grant_type='client_credentials',
        )
        sess.send = mock_json_response(self.token)
        sess.get('https://i.b/user')
        self.assertFalse(update_token.called)

        sess = OAuth2Session(
            'foo', token=old_token,
            token_endpoint='https://i.b/token',
            grant_type='client_credentials',
            update_token=update_token,
        )
        sess.send = mock_json_response(self.token)
        sess.get('https://i.b/user')
        self.assertTrue(update_token.called)

    def test_revoke_token(self):
        sess = OAuth2Session('a')
        answer = {'status': 'ok'}
        sess.send = mock_json_response(answer)
        resp = sess.revoke_token('https://i.b/token', 'hi')
        self.assertEqual(resp.json(), answer)
        resp = sess.revoke_token(
            'https://i.b/token', 'hi',
            token_type_hint='access_token'
        )
        self.assertEqual(resp.json(), answer)

        def revoke_token_request(url, headers, data):
            self.assertEqual(url, 'https://i.b/token')
            return url, headers, data

        sess.register_compliance_hook(
            'revoke_token_request',
            revoke_token_request,
        )
        sess.revoke_token(
            'https://i.b/token', 'hi',
            body='',
            token_type_hint='access_token'
        )

    def test_introspect_token(self):
        sess = OAuth2Session('a')
        answer = {
            "active": True,
            "client_id": "l238j323ds-23ij4",
            "username": "jdoe",
            "scope": "read write dolphin",
            "sub": "Z5O3upPC88QrAjx00dis",
            "aud": "https://protected.example.net/resource",
            "iss": "https://server.example.com/",
            "exp": 1419356238,
            "iat": 1419350238
        }
        sess.send = mock_json_response(answer)
        resp = sess.introspect_token('https://i.b/token', 'hi')
        self.assertEqual(resp.json(), answer)

    def test_client_secret_jwt(self):
        sess = OAuth2Session(
            'id', 'secret',
            token_endpoint_auth_method='client_secret_jwt'
        )
        sess.register_client_auth_method(ClientSecretJWT())

        mock_assertion_response(self, sess)
        token = sess.fetch_token('https://i.b/token')
        self.assertEqual(token, self.token)

    def test_client_secret_jwt2(self):
        sess = OAuth2Session(
            'id', 'secret',
            token_endpoint_auth_method=ClientSecretJWT(),
        )
        mock_assertion_response(self, sess)
        token = sess.fetch_token('https://i.b/token')
        self.assertEqual(token, self.token)

    def test_private_key_jwt(self):
        client_secret = read_key_file('rsa_private.pem')
        sess = OAuth2Session(
            'id', client_secret,
            token_endpoint_auth_method='private_key_jwt'
        )
        sess.register_client_auth_method(PrivateKeyJWT())
        mock_assertion_response(self, sess)
        token = sess.fetch_token('https://i.b/token')
        self.assertEqual(token, self.token)

    def test_custom_client_auth_method(self):
        def auth_client(client, method, uri, headers, body):
            uri = add_params_to_uri(uri, [
                ('client_id', client.client_id),
                ('client_secret', client.client_secret),
            ])
            uri = uri + '&' + body
            body = ''
            return uri, headers, body

        sess = OAuth2Session(
            'id', 'secret',
            token_endpoint_auth_method='client_secret_uri'
        )
        sess.register_client_auth_method(('client_secret_uri', auth_client))

        def fake_send(r, **kwargs):
            self.assertIn('client_id=', r.url)
            self.assertIn('client_secret=', r.url)
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.json = lambda: self.token
            return resp

        sess.send = fake_send
        token = sess.fetch_token('https://i.b/token')
        self.assertEqual(token, self.token)

    def test_use_client_token_auth(self):
        import requests

        token = 'Bearer ' + self.token['access_token']

        def verifier(r, **kwargs):
            auth_header = r.headers.get(str('Authorization'), None)
            self.assertEqual(auth_header, token)
            resp = mock.MagicMock()
            return resp

        client = OAuth2Session(
            client_id=self.client_id,
            token=self.token
        )

        sess = requests.Session()
        sess.send = verifier
        sess.get('https://i.b', auth=client.token_auth)
