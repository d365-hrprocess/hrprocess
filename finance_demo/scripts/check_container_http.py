"""Read-only HTTP checks against a locally started complete finance/HR container.

The default target is the local regression container. No ERP mutations are sent.
"""
import argparse
import json
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit


def request(base, path, *, key=None, method='GET', data=None):
    headers = {'X-Finance-Demo-Key': key} if key else {}
    req = urllib.request.Request(base + path, headers=headers, method=method, data=data)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        payload = response.read(32768)
        return response.status, response.headers, payload


def check(base):
    records = []
    for _ in range(60):
        try:
            if request(base, '/healthz')[0] == 200:
                break
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    else:
        raise AssertionError('Original HR /healthz did not become ready')
    for path in ['/', '/hr', '/jobs', '/api/health', '/api/jobs', '/healthz']:
        status, headers, body = request(base, path)
        assert status == 200, (path, status)
        records.append({'path': path, 'status': status})
    status, _, body = request(base, '/finance/health')
    assert status == 200 and json.loads(body) in [{'status': 'ok'}, {'status': 'disabled'}]
    records.append({'path': '/finance/health', 'status': status})
    for path in ['/finance/ready', '/finance/metadata/status', '/finance/swagger2.json',
                 '/finance/customers', '/finance/customers/AST-001',
                 '/finance/customers/AST-001/transactions', '/finance/finance/customers']:
        for key in [None, 'invalid-test-key']:
            status, headers, body = request(base, path, key=key)
            assert status in [401, 503], (path, status)
            assert headers.get('Content-Type', '').startswith('application/json'), path
            payload = json.loads(body)
            assert payload['success'] is False and 'data' not in payload, path
        records.append({'path': path, 'status': status, 'unauthorized_data_blocked': True})
    for path in ['/finance', '/finance/']:
        status, headers, body = request(base, path)
        assert status in [401, 503] and not headers.get('Location'), (path, status)
        assert headers.get('Content-Type', '').startswith('application/json')
    status, headers, _ = request(base, '/api/applications', method='POST', data=b'')
    assert status in [401, 403, 422], status
    records.append({'path': '/api/applications', 'status': status, 'unauthorized_upload_blocked': True})
    print(json.dumps({'passed': True, 'checks': records}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:18080')
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost'} or url.path not in {'', '/'}:
        parser.error('This regression harness accepts only a local HTTP container origin.')
    check(args.base_url.rstrip('/'))
