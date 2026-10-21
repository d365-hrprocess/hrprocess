from dataclasses import replace
from decimal import Decimal
import json
import httpx
import pytest
from finance_demo_api.d365 import D365Client
from finance_demo_api.errors import FinanceError
from finance_demo_api.settings import Settings


@pytest.mark.asyncio
async def test_token_scope_cached_expiry_and_exact_decimal(monkeypatch):
 monkeypatch.setenv('FINANCE_DEMO_CLIENT_SECRET','fictional-secret-VALUE')
 tokens=[];reads=[]
 def handle(req):
  if req.url.host=='login.microsoftonline.com':
   tokens.append(req);return httpx.Response(200,json={'access_token':f'token-{len(tokens)}','expires_in':3600})
  reads.append(req);return httpx.Response(200,content=b'{"value":[{"AmountCur":1234567890.123456789}]}')
 async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
  c=D365Client(Settings(),http);r=await c.json_request('GET','CustomersV3');await c.json_request('GET','CustomersV3')
  assert len(tokens)==1
  assert 'client_secret=fictional-secret-VALUE' in tokens[0].content.decode()
  assert 'org1a43c536.operations.dynamics.com%2F.default' in tokens[0].content.decode()
  assert r['value'][0]['AmountCur']==Decimal('1234567890.123456789')
  c._token_deadline=0;await c.json_request('GET','CustomersV3')
  assert len(tokens)==2 and reads[-1].headers['Authorization']=='Bearer token-2'


@pytest.mark.asyncio
@pytest.mark.parametrize('method',['POST','PATCH','DELETE'])
async def test_write_timeout_never_retried_and_uncertain(monkeypatch,method):
 monkeypatch.setenv('FINANCE_DEMO_CLIENT_SECRET','fictional-secret');count=0
 def handle(req):
  nonlocal count
  if req.url.host=='login.microsoftonline.com':return httpx.Response(200,json={'access_token':'mock','expires_in':3600})
  count+=1;raise httpx.ReadTimeout('sensitive upstream text')
 async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
  with pytest.raises(FinanceError) as e:await D365Client(Settings(),http).request(method,'CustomersV3')
  assert count==1 and e.value.uncertain and not e.value.retryable
  assert 'sensitive' not in str(e.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code',[(403,'d365_access_denied'),(500,'d365_unavailable'),(404,'d365_not_found'),(302,'d365_login_redirect')])
async def test_only_genuine404_means_missing(monkeypatch,status,code):
 monkeypatch.setenv('FINANCE_DEMO_CLIENT_SECRET','fictional-secret')
 def handle(req):
  if req.url.host=='login.microsoftonline.com':return httpx.Response(200,json={'access_token':'mock','expires_in':3600})
  return httpx.Response(status,json={'error':{'message':'sensitive ERP detail'}})
 async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
  with pytest.raises(FinanceError) as e:await D365Client(replace(Settings(),read_retry_attempts=1),http).request('GET','CustomersV3')
  assert e.value.code==code and 'sensitive ERP' not in json.dumps(e.value.as_dict())


@pytest.mark.asyncio
async def test_read_401_refreshes_token_once(monkeypatch):
 monkeypatch.setenv('FINANCE_DEMO_CLIENT_SECRET','fictional-secret');tokens=0;reads=0
 def handle(req):
  nonlocal tokens,reads
  if req.url.host=='login.microsoftonline.com':tokens+=1;return httpx.Response(200,json={'access_token':f'token{tokens}','expires_in':3600})
  reads+=1;return httpx.Response(401 if reads==1 else 200,json={'value':[]})
 async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
  assert await D365Client(Settings(),http).json_request('GET','CustomersV3')=={'value':[]}
  assert reads==2 and tokens==2


def test_missing_secret_and_secret_id_rejected(monkeypatch):
 monkeypatch.setenv('FINANCE_DEMO_CLIENT_SECRET','fb07b3d3-b14c-4880-b49e-04d818160c0f')
 with pytest.raises(FinanceError) as e:Settings().secret()
 assert e.value.code=='finance_secret_invalid'
 monkeypatch.delenv('FINANCE_DEMO_CLIENT_SECRET')
 with pytest.raises(FinanceError) as e:Settings().secret()
 assert e.value.code=='finance_secret_missing'


@pytest.mark.asyncio
async def test_internalpaths_only():
 async with httpx.AsyncClient() as http:
  c=D365Client(Settings(),http)
  for path in ['https://evil.invalid/data','//evil.invalid/x','../api','CustomersV3?filter=evil']:
   with pytest.raises(FinanceError):c.url(path)
