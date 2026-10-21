import asyncio
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import json
import httpx
import pytest
from finance_demo_api.d365 import D365Client
from finance_demo_api.metadata import MetadataManager,EntitySchema,parse_metadata
from finance_demo_api.errors import FinanceError
from finance_demo_api.settings import Settings


def csdl(tx=True,missing=None,keys=('dataAreaId','CustomerAccount')):
 props={'dataAreaId':'Edm.String','CustomerAccount':'Edm.String','OrganizationName':'Edm.String','SalesCurrencyCode':'Edm.String','CustomerGroupId':'Edm.String','PaymentTerms':'Edm.String','PartyType':'Edm.String'}
 props.pop(missing,None)
 p=''.join(f'<Property Name="{k}" Type="{v}"/>' for k,v in props.items())
 k=''.join(f'<PropertyRef Name="{v}"/>' for v in keys)
 tp={'RecId':'Edm.Int64','dataAreaId':'Edm.String','AccountNum':'Edm.String','Invoice':'Edm.String','Voucher':'Edm.String','TransDate':'Edm.Date','DueDate':'Edm.Date','AmountCur':'Edm.Decimal','TransType':'Edm.String'}
 t='<EntityType Name="ActualTx"><Key><PropertyRef Name="RecId"/></Key>'+''.join(f'<Property Name="{k}" Type="{v}"/>' for k,v in tp.items())+'</EntityType>' if tx else ''
 ts='<EntitySet Name="CustTransBiEntities" EntityType="D.ActualTx"/>' if tx else ''
 return f'''<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices><Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="Demo" Alias="D"><EntityType Name="Base"><Key>{k}</Key>{p}</EntityType><EntityType Name="ActualCustomer" BaseType="D.Base"/>{t}<EntityContainer Name="Container"><EntitySet Name="CustomersV3" EntityType="D.ActualCustomer"/>{ts}</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>'''


def parse(tmp_path,xml):
 p=tmp_path/'metadata.xml';p.write_text(xml)
 return parse_metadata(p,Settings())


def cfg(tmp_path,**kw):return replace(Settings(),cache_directory=str(tmp_path/'cache'),prefer_persistent_cache=False,metadata_retry_backoff_seconds=0,**kw)


def transport(xml,metadata_status=200,tx_status=200,cust_status=200):
 calls=[]
 def handle(req):
  calls.append(req)
  if req.url.host=='login.microsoftonline.com':return httpx.Response(200,json={'access_token':'fictional-token','expires_in':3600})
  if req.url.path.endswith('$metadata'):return httpx.Response(metadata_status,content=xml.encode())
  if req.url.path.endswith('CustTransBiEntities'):return httpx.Response(tx_status,json={'value':[]})
  return httpx.Response(cust_status,json={'value':[]})
 return httpx.AsyncClient(transport=httpx.MockTransport(handle)),calls


def test_alias_namespaces_inheritance_entityset_type_mapping(tmp_path):
 r=parse(tmp_path,csdl())
 assert r['entity_count']==2
 assert r['customers']['type_name']=='Demo.ActualCustomer'
 assert r['customers']['keys']==['dataAreaId','CustomerAccount']
 assert r['transactions']['fields']['account']=='AccountNum'
 assert r['customers']['fields']['name']=='OrganizationName'
 s=EntitySchema.from_dict(r['customers'])
 assert s.key_path('usmf',"O'BRIEN")=="CustomersV3(dataAreaId='usmf',CustomerAccount='O''BRIEN')"


@pytest.mark.parametrize('xml',['<Edmx>','<html>Login</html>','<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><x>&secret;</x>'])
def test_unsafe_truncated_html_xml_rejected(tmp_path,xml):
 with pytest.raises(FinanceError) as e:parse(tmp_path,xml)
 assert e.value.code=='metadata_invalid_xml'
 assert 'passwd' not in str(e.value)


def test_missing_customer_property_and_invalid_keys_fail_closed(tmp_path):
 with pytest.raises(FinanceError) as e:parse(tmp_path,csdl(missing='PaymentTerms'))
 assert e.value.code=='metadata_required_field_missing'
 with pytest.raises(FinanceError) as e:parse(tmp_path,csdl(keys=('CustomerAccount',)))
 assert e.value.code=='metadata_customer_key_invalid'


def test_transaction_missing_is_independent(tmp_path):
 r=parse(tmp_path,csdl(tx=False))
 assert r['customers'] and r['transactions'] is None
 assert r['transaction_schema_error']['code']=='metadata_entity_missing'


@pytest.mark.asyncio
async def test_automatic_warmup_real_probe_tx_denial_independent(tmp_path,monkeypatch):
 monkeypatch.setenv('FINANCE_DEMO_CLIENT_SECRET','fictional-secret')
 c=cfg(tmp_path);http,calls=transport(csdl(),tx_status=403)
 m=MetadataManager(c,D365Client(c,http));m.start()
 for _ in range(200):
  if m.status()['state']!='warming':break
  await asyncio.sleep(.005)
 assert m.require_customers().field('account')=='CustomerAccount'
 assert m.status()['capabilities']['transactions']['state']=='unavailable'
 with pytest.raises(FinanceError):m.require_transactions()
 assert m.status()['write_permissions']=='unverified_until_live_demo_crud'
 await m.close();await http.aclose()


@pytest.mark.asyncio
async def test_cache_reuse_rechecks_live_permissions(tmp_path,monkeypatch):
 monkeypatch.setenv('FINANCE_DEMO_CLIENT_SECRET','fictional-secret')
 c=cfg(tmp_path);http,calls=transport(csdl())
 a=MetadataManager(c,D365Client(c,http));await a.refresh();calls.clear()
 b=MetadataManager(c,D365Client(c,http));await b.refresh()
 assert b.status()['ready'] and b.status()['cache_source']=='cache'
 assert not any(r.url.path.endswith('$metadata') for r in calls)
 assert any(r.url.path.endswith('CustomersV3') for r in calls)
 await http.aclose()


@pytest.mark.asyncio
async def test_stale_cache_preserved_refresh_failure_reported(tmp_path,monkeypatch):
 monkeypatch.setenv('FINANCE_DEMO_CLIENT_SECRET','fictional-secret')
 c=cfg(tmp_path);http,calls=transport(csdl());a=MetadataManager(c,D365Client(c,http));await a.refresh();await http.aclose()
 raw=json.loads(a.cache_path.read_text());raw['downloaded_at']=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat();a.cache_path.write_text(json.dumps(raw));old=a.cache_path.read_bytes()
 http,calls=transport('unavailable',metadata_status=503);b=MetadataManager(c,D365Client(c,http));await b.refresh()
 assert b.status()['ready'] and b.status()['stale']
 assert b.status()['refresh_error']['code']=='d365_unavailable'
 assert b.cache_path.read_bytes()==old
 await http.aclose()


@pytest.mark.asyncio
async def test_corrupt_wrongscope_cache_redownloaded(tmp_path,monkeypatch):
 monkeypatch.setenv('FINANCE_DEMO_CLIENT_SECRET','fictional-secret')
 c=cfg(tmp_path);http,calls=transport(csdl());a=MetadataManager(c,D365Client(c,http));await a.refresh()
 for contents in ['{truncated',a.cache_path.read_text().replace(c.cache_scope,'wrong-scope')]:
  a.cache_path.write_text(contents);calls.clear();b=MetadataManager(c,D365Client(c,http));await b.refresh()
  assert any(r.url.path.endswith('$metadata') for r in calls)
 await http.aclose()


@pytest.mark.asyncio
async def test_singleflight_across_managers(tmp_path,monkeypatch):
 monkeypatch.setenv('FINANCE_DEMO_CLIENT_SECRET','fictional-secret')
 c=cfg(tmp_path);http,calls=transport(csdl());a=MetadataManager(c,D365Client(c,http));b=MetadataManager(c,D365Client(c,http))
 await asyncio.gather(a.refresh(),b.refresh())
 assert a.status()['ready'] and b.status()['ready']
 assert sum(r.url.path.endswith('$metadata') for r in calls)==1
 await http.aclose()


@pytest.mark.asyncio
async def test_size_and_missingsecret_fail_safely(tmp_path,monkeypatch):
 c=cfg(tmp_path,metadata_maximum_bytes=30);http,calls=transport(csdl());m=MetadataManager(c,D365Client(c,http))
 with pytest.raises(FinanceError) as e:m.require_customers()
 assert e.value.retryable
 monkeypatch.setenv('FINANCE_DEMO_CLIENT_SECRET','fictional-secret');await m.refresh()
 assert m.status()['refresh_error']['code']=='metadata_size_limit'
 monkeypatch.delenv('FINANCE_DEMO_CLIENT_SECRET');m=MetadataManager(c,D365Client(c,http));await m.refresh()
 assert m.status()['refresh_error']['code']=='finance_secret_missing'
 assert 'fictional-secret' not in json.dumps(m.status())
 await http.aclose()

@pytest.mark.asyncio
async def test_valid_cache_is_not_entity_permission_proof(tmp_path,monkeypatch):
 monkeypatch.setenv('FINANCE_DEMO_CLIENT_SECRET','fictional-secret')
 c=cfg(tmp_path);http,calls=transport(csdl());a=MetadataManager(c,D365Client(c,http));await a.refresh();await http.aclose()
 http,calls=transport(csdl(),cust_status=403);b=MetadataManager(c,D365Client(c,http));await b.refresh()
 assert not b.status()['ready']
 assert b.status()['entities']['customers'] is not None
 assert b.status()['capabilities']['customers']['error']['code']=='d365_access_denied'
 with pytest.raises(FinanceError):b.require_customers()
 await http.aclose()


@pytest.mark.asyncio
async def test_metadata_total_timeout_is_bounded(tmp_path,monkeypatch):
 monkeypatch.setenv('FINANCE_DEMO_CLIENT_SECRET','fictional-secret')
 c=cfg(tmp_path,metadata_total_timeout_seconds=.02)
 async def handle(req):
  if req.url.host=='login.microsoftonline.com':return httpx.Response(200,json={'access_token':'fictional','expires_in':3600})
  await asyncio.sleep(1)
  return httpx.Response(200,content=csdl().encode())
 async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
  m=MetadataManager(c,D365Client(c,http));await m.refresh()
  assert m.status()['refresh_error']['code']=='metadata_warmup_timeout'
  assert not m.status()['ready']


@pytest.mark.parametrize("old,new", [
 ('Name="AmountCur" Type="Edm.Decimal"', 'Name="AmountCur" Type="Edm.String"'),
 ('Name="AccountNum" Type="Edm.String"', 'Name="AccountNum" Type="Edm.Int64"'),
])
def test_invalid_transaction_types_do_not_leave_usable_schema(tmp_path, old, new):
 result = parse(tmp_path, csdl().replace(old,new))
 assert result["customers"] is not None
 assert result["transactions"] is None
 assert result["transaction_schema_error"]["code"] == "metadata_transaction_type_invalid"


@pytest.mark.asyncio
async def test_invalid_transaction_schema_never_becomes_readable(tmp_path, monkeypatch):
 monkeypatch.setenv("FINANCE_DEMO_CLIENT_SECRET","fictional-secret")
 c=cfg(tmp_path)
 xml=csdl().replace('Name="AmountCur" Type="Edm.Decimal"','Name="AmountCur" Type="Edm.String"')
 http,calls=transport(xml)
 m=MetadataManager(c,D365Client(c,http));await m.refresh()
 assert m.status()["ready"]
 assert m.status()["capabilities"]["transactions"]["state"]=="unavailable"
 with pytest.raises(FinanceError):m.require_transactions()
 assert not any(r.url.path.endswith("CustTransBiEntities") for r in calls)
 await http.aclose()
