"""Explicit live HTTPS smoke test. Real writes require --allow-writes and a fresh demo account.

Run from finance_demo with its Python 3.13.3 environment. Uses only the same
FINANCE_DEMO_CLIENT_SECRET variable; never prints it or the derived key.
"""
from __future__ import annotations
import argparse
import hashlib
import hmac
import json
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from finance_demo_api.settings import Settings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-writes', action='store_true', help='Explicitly authorize the demo create/update/delete sequence')
    parser.add_argument('--account', help='Fresh TEST-COPILOT- account, required with --allow-writes')
    args = parser.parse_args(argv)
    settings = Settings.load()
    if args.allow_writes and (not args.account or not args.account.startswith(settings.demo_customer_prefix)):
        parser.error('--allow-writes requires a fresh --account starting with TEST-COPILOT-')
    key = hmac.new(settings.secret().encode(), f'finance-demo/copilot/v1|{settings.tenant_id}|{settings.client_id}'.encode(), hashlib.sha256).hexdigest()
    results = []
    may_exist = False
    with httpx.Client(base_url=settings.public_base_url + settings.api_prefix + '/',
                      headers={'X-Finance-Demo-Key': key}, timeout=30, follow_redirects=False) as client:
        def call(name, method, path, *, expected=200, body=None):
            nonlocal may_exist
            response = client.request(method, path, json=body)
            if 'application/json' not in response.headers.get('content-type', ''):
                raise RuntimeError(f'{name}: HTTP {response.status_code} returned a non-JSON response; check ingress/Easy Auth.')
            payload = response.json()
            if response.status_code != expected or (expected < 400 and payload.get('success') is not True):
                code = payload.get('error', {}).get('code', 'unexpected_response')
                raise RuntimeError(f'{name}: HTTP {response.status_code}, code={code}. No write will be retried.')
            results.append({'check': name, 'http_status': response.status_code, 'passed': True})
            return payload
        try:
            call('customer readiness', 'GET', 'ready')
            call('AST-001 customer read', 'GET', 'customers/AST-001')
            call('AST-001 limited recent transactions', 'GET', 'customers/AST-001/transactions?limit=10')
            if args.allow_writes:
                from finance_demo_api.business import validate_account
                validate_account(args.account)
                call('fresh account absence', 'GET', 'customers/' + args.account, expected=404)
                may_exist = True  # A sent/uncertain create can leave a record; never silently clean it up.
                call('create customer', 'POST', 'customers', expected=201, body={
                    'customer_account': args.account, 'organization_name': 'Copilot Demo Customer'})
                created = call('create read-back', 'GET', 'customers/' + args.account)['data']
                assert created['organization_name'] == 'Copilot Demo Customer'
                assert created['currency'] == settings.currency and created['customer_group'] == settings.customer_group
                assert created['payment_terms'] == settings.payment_terms
                call('update customer', 'PATCH', 'customers/' + args.account,
                     body={'organization_name': 'Copilot Demo Customer Updated'})
                updated = call('update read-back', 'GET', 'customers/' + args.account)['data']
                assert updated['organization_name'] == 'Copilot Demo Customer Updated'
                deleted = call('delete customer', 'DELETE', 'customers/' + args.account)['data']
                assert deleted['deleted'] is True and deleted['verified'] is True
                call('deletion verification', 'GET', 'customers/' + args.account, expected=404)
                may_exist = False
        except Exception as error:
            # Never include an upstream body, credential, raw httpx request or token.
            print(json.dumps({'passed': False, 'completed_checks': results,
                              'error_type': type(error).__name__,
                              'message': str(error) if isinstance(error, RuntimeError) else 'Live verification failed; inspect the exact account before another write.',
                              'account_requiring_review': args.account if may_exist else None}))
            return 1
    print(json.dumps({'passed': True, 'checks': results, 'remaining_demo_accounts': [],
                      'copilot_connector_tested': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
