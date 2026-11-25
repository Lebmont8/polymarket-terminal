import unittest,tempfile,time,json,threading,sqlite3,concurrent.futures
from pathlib import Path
from engine import Journal,Execution,OrderError,validate
from sources import paper_snapshot
from server import Application,handler,ThreadingHTTPServer
import urllib.request,urllib.error
def market():
 return {"token_id":"123","received_at":time.time(),"accepting_orders":True,"tick_size":"0.01","min_order_size":"5","best_ask":"0.52","best_bid":"0.44"}
def order(**extra):
 return {"token_id":"123","side":"BUY","price":"0.48","size":"10","order_type":"GTC","post_only":True,**extra}
class Fake:
 def __init__(self,fail=False):self.count=0;self.fail=fail
 def submit(self,payload,key):
  self.count+=1
  if self.fail:raise TimeoutError()
  return {"state":"LIVE","order_id":"exchange-"+key}
 def cancel(self,oid):return {"cancelled":True}
class CoreTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.j=Journal(Path(self.tmp.name)/"orders.sqlite")
 def tearDown(self):self.tmp.cleanup()
 def draft(self,**kw):return self.j.draft(order(**kw),market(),"intent1")
 def live(self):
  r=self.draft();f=Fake();Execution(self.j,f,lambda:[]).submit(r["id"],market());return f
 def test_valid_passive_limit(self):self.assertEqual(self.draft()["state"],"DRAFT")
 def test_same_id_same_order(self):
  a=self.draft();b=self.draft();self.assertEqual(a,b);self.assertEqual(len(self.j.list()),1)
 def test_same_id_different_order_refused(self):
  self.draft()
  with self.assertRaises(OrderError):self.draft(price="0.47")
 def test_bad_tick(self):
  with self.assertRaises(OrderError):self.draft(price="0.481")
 def test_small_lot(self):
  with self.assertRaises(OrderError):self.draft(size="4")
 def test_excess_quantity_precision(self):
  with self.assertRaises(OrderError):self.draft(size="5.001")
 def test_post_only_cross_refused(self):
  with self.assertRaises(OrderError):self.draft(price="0.52")
 def test_post_only_fok_refused(self):
  with self.assertRaises(OrderError):self.draft(order_type="FOK")
 def test_gtd_expiry_padding(self):
  with self.assertRaises(OrderError):self.draft(order_type="GTD",expiration=int(time.time())+30)
  self.assertEqual(validate(order(order_type="GTD",expiration=int(time.time())+180),market())["order_type"],"GTD")
 def test_nan_rejected(self):
  with self.assertRaises(OrderError):self.draft(price="NaN")
 def test_float_rejected(self):
  with self.assertRaises(OrderError):self.draft(price=.48)
 def test_stale_context(self):
  m=market();m["received_at"]-=10
  with self.assertRaises(OrderError):validate(order(),m)
 def test_inactive_context(self):
  m=market();m["accepting_orders"]=False
  with self.assertRaises(OrderError):validate(order(),m)
 def test_identity_mismatch(self):
  m=market();m["token_id"]="999"
  with self.assertRaises(OrderError):validate(order(),m)
 def test_gate_prevents_network(self):
  self.draft();f=Fake();r=Execution(self.j,f,lambda:["GEO_BLOCK"]).submit("intent1",market())
  self.assertFalse(r["submitted"]);self.assertEqual(f.count,0)
 def test_notional_cap(self):
  self.draft();f=Fake();r=Execution(self.j,f,lambda:[],max_notional="1").submit("intent1",market())
  self.assertIn("NOTIONAL_LIMIT",r["reasons"]);self.assertEqual(f.count,0)
 def test_timeout_never_blind_resubmit(self):
  self.draft();f=Fake(True);e=Execution(self.j,f,lambda:[])
  self.assertIsNone(e.submit("intent1",market())["submitted"])
  with self.assertRaises(OrderError):e.submit("intent1",market())
  self.assertEqual(f.count,1);self.assertEqual(self.j.get("intent1")["state"],"UNKNOWN")
 def test_parallel_submit_only_one_network_call(self):
  self.draft();f=Fake();e=Execution(self.j,f,lambda:[])
  def run(_):
   try:return e.submit("intent1",market())
   except OrderError:return None
  with concurrent.futures.ThreadPoolExecutor(4) as pool:list(pool.map(run,range(4)))
  self.assertEqual(f.count,1)
 def test_late_fill_after_cancel_preserved(self):
  f=self.live();e=Execution(self.j,f,lambda:[])
  e.cancel("intent1")
  self.j.observe("intent1","latefill","exchange-intent1","10","FILLED")
  self.assertEqual(self.j.get("intent1")["state"],"FILLED")
 def test_cancel_ack_after_fill_not_unfill(self):
  self.live()
  self.j.observe("intent1","fill","exchange-intent1","10","FILLED")
  self.j.observe("intent1","cancel","exchange-intent1","10","CANCELLED")
  self.assertEqual(self.j.get("intent1")["state"],"FILLED")
 def test_partial_confirmation_does_not_close_order(self):
  f=self.live()
  self.j.observe("intent1","part","exchange-intent1","3","PARTIAL")
  self.j.observe("intent1","confirm","exchange-intent1","3","CONFIRMED")
  self.assertEqual(self.j.get("intent1")["state"],"PARTIAL")
  self.assertEqual(Execution(self.j,f,lambda:[]).cancel("intent1")["filled"],"3")
 def test_settlement_failure_not_misreported_as_order_rejection(self):
  self.live();self.j.observe("intent1","fill","exchange-intent1","10","FILLED")
  self.j.observe("intent1","settlementfail","exchange-intent1","10","FAILED")
  row=self.j.get("intent1");self.assertEqual(row["state"],"FILLED");self.assertEqual(row["settlement"],"FAILED")
 def test_event_replay_idempotent_and_conflict_refused(self):
  self.live()
  self.j.observe("intent1","part","exchange-intent1","3","PARTIAL")
  self.j.observe("intent1","part","exchange-intent1","3","PARTIAL")
  with self.assertRaises(OrderError):self.j.observe("intent1","part","exchange-intent1","4","PARTIAL")
 def test_overfill_refused(self):
  self.live()
  with self.assertRaises(OrderError):self.j.observe("intent1","bad","exchange-intent1","11","FILLED")
 def test_restart_retains_unknown(self):
  self.draft();Execution(self.j,Fake(True),lambda:[]).submit("intent1",market())
  self.assertEqual(Journal(self.j.path).get("intent1")["state"],"UNKNOWN")
 def test_draft_cancel_no_transport(self):
  self.draft();self.assertEqual(Execution(self.j).cancel("intent1")["state"],"CANCELLED_LOCAL")

class ReadTests(unittest.TestCase):
 def test_locked_source_is_unknown_not_zero_profit(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/"polymarket-forward-2881";p.mkdir()
   (p/"status.json").write_text(json.dumps({"phase":"RUNNING","at":time.time(),"bank5pct":{"net_settled":"7"}}))
   c=sqlite3.connect(p/"paper.sqlite");c.execute("CREATE TABLE fills(strategy TEXT,condition TEXT,shares TEXT,cost TEXT,fee TEXT,payout TEXT,data TEXT)");c.commit()
   c.execute("BEGIN EXCLUSIVE")
   result=paper_snapshot(Path(tmp))[0]
   self.assertEqual(result["read_error"],"OperationalError");self.assertTrue(result["stale"])
   self.assertEqual(result["accounts"][0]["net_settled"],"7")
   c.rollback();c.close()
 def test_missing_sources_have_explicit_errors(self):
  with tempfile.TemporaryDirectory() as tmp:
   self.assertTrue(all(x.get("read_error")=="FileNotFoundError" for x in paper_snapshot(Path(tmp))))

class HTTPTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory()
  self.app=Application(Path(self.tmp.name)/"state",Path(self.tmp.name)/"papers")
  self.app.context=lambda *args:{**market(),"condition":"0x"+"a"*64}
  self.server=ThreadingHTTPServer(("127.0.0.1",0),handler(self.app))
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
 def tearDown(self):
  self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
 def request(self,path,body=None,auth=True,origin=True):
  headers={}
  if auth:headers["X-Polymarket-Internal"]=self.app.token
  if origin:headers["X-Polymarket-Origin-Verified"]="1"
  req=urllib.request.Request("http://127.0.0.1:"+str(self.server.server_port)+"/polymarket/"+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
  try:r=urllib.request.urlopen(req)
  except urllib.error.HTTPError as e:r=e
  with r:return r.status,r.read()
 def test_anonymous_denied(self):self.assertEqual(self.request("api/snapshot",auth=False)[0],401)
 def test_forged_origin_denied(self):self.assertEqual(self.request("api/drafts",order(),origin=False)[0],403)
 def test_live_submit_explicitly_not_sent(self):
  code,body=self.request("api/submit",{"id":"none"})
  self.assertEqual(code,409);self.assertIs(json.loads(body)["submitted"],False)
 def test_prepare_cancel_is_local_only(self):
  d=order(condition="0x"+"a"*64,client_id="http-intent")
  code,body=self.request("api/drafts",d);self.assertEqual(code,201)
  self.assertEqual(json.loads(body)["state"],"DRAFT")
  code,body=self.request("api/cancel",{"id":"http-intent"});self.assertEqual(code,200)
  self.assertEqual(json.loads(body)["state"],"CANCELLED_LOCAL")
 def test_path_traversal_not_served(self):self.assertEqual(self.request("../engine.py")[0],404)
 def test_snapshot_has_no_fake_transactions(self):
  code,body=self.request("api/snapshot");j=json.loads(body)
  self.assertEqual(j["live"]["transactions"],[]);self.assertFalse(j["live"]["available"])

class ContextTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=Application(Path(self.tmp.name)/"state")
  self.condition="0x"+"a"*64
  self.book={"asset_id":"123","market":self.condition,"tick_size":"0.01","min_order_size":"5","bids":[{"price":"0.44"}],"asks":[{"price":"0.52"}]}
  self.meta={"c":self.condition,"t":[{"t":"123","o":"Up"}],"ao":True,"mts":"0.01","mos":"5"}
  self.app.get_json=lambda u:self.book if "/book?" in u else self.meta
 def tearDown(self):self.tmp.cleanup()
 def test_actual_compact_metadata_schema(self):
  self.assertTrue(self.app.context("123",self.condition)["accepting_orders"])
 def test_compact_market_identity_refused(self):
  self.meta["c"]="0x"+"b"*64
  with self.assertRaises(OrderError):self.app.context("123",self.condition)
 def test_constraint_disagreement_refused(self):
  self.meta["mts"]="0.02"
  with self.assertRaises(OrderError):self.app.context("123",self.condition)

if __name__=="__main__":unittest.main(verbosity=2)
