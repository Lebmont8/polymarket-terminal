"""Loopback-only prepared terminal. Production Hub integration is NOT activated."""
import argparse,json,os,secrets,time,hmac,threading,urllib.request,urllib.parse
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from engine import Journal,Execution,OrderError
from sources import paper_snapshot,json_file
BASE=Path(__file__).resolve().parent
MAX_BODY=16384
class Application:
    def __init__(self,state=None,paper_root=None):
        self.state=Path(state or BASE/"state")
        self.state.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.journal=Journal(self.state/"orders.sqlite")
        self.paper_root=paper_root
        self.token_path=self.state/"internal-token"
        if not self.token_path.exists():
            fd=os.open(self.token_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,"w") as f:f.write(secrets.token_urlsafe(32))
        self.token=self.token_path.read_text().strip()
        self.market_lock=threading.RLock()
        self.markets={}
        self.last_fetch=0
        self.network=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.execution=Execution(self.journal,gate=self.live_reasons)
    def connection(self):
        try:return json_file(BASE/"connection.json")
        except Exception:return {"checked_at":0,"routes":[]}
    def live_reasons(self):
        result=[]
        data=self.connection()
        if time.time()-data.get("checked_at",0)>600:result.append("ACCESS_CHECK_STALE")
        routes=data.get("routes",[])
        if not routes or not any(r.get("endpoint")=="geoblock" and r.get("data",{}).get("blocked") is False for r in routes):
            result.append("NO_ELIGIBLE_ROUTE")
        # No signer/official authenticated SDK connected. Public-data access is not trading authentication.
        result.append("TRADING_ACCOUNT_NOT_CONNECTED")
        return result
    def snapshot(self):
        args={} if self.paper_root is None else {"root":self.paper_root}
        return {"at":time.time(),"papers":paper_snapshot(**args),"orders":self.journal.list(),
                "live":{"available":False,"reasons":self.live_reasons(),"transactions":[]},
                "connection":self.connection(),"integration":"PREPARED_NOT_ACTIVATED"}
    def get_json(self,url):
        with self.network.open(urllib.request.Request(url,headers={"User-Agent":"PolymarketTerminal3465/readonly","Accept":"application/json"}),timeout=5) as r:
            body=r.read(2*1024*1024+1)
            if len(body)>2*1024*1024:raise OrderError("API response too large")
            return json.loads(body)
    def context(self,token,condition):
        if not token.isdigit() or not 1<=len(token)<=78 or not condition.startswith("0x") or len(condition)!=66 or any(c not in "0123456789abcdefABCDEF" for c in condition[2:]):
            raise OrderError("Invalid market identifiers")
        with self.market_lock:
            key=token+":"+condition
            old=self.markets.get(key)
            if old and time.time()-old["received_at"]<2:return old
            if time.monotonic()-self.last_fetch<1:raise OrderError("Market request rate limited; retry later")
            self.last_fetch=time.monotonic()
            book=self.get_json("https://clob.polymarket.com/book?token_id="+token)
            meta=self.get_json("https://clob.polymarket.com/clob-markets/"+condition)
            if str(book.get("asset_id"))!=token or book.get("market")!=condition or meta.get("c")!=condition or not any(str(x.get("t"))==token for x in meta.get("t",[])):
                raise OrderError("Order book identity mismatch")
            bids=[x["price"] for x in book.get("bids",[])]
            asks=[x["price"] for x in book.get("asks",[])]
            from engine import dec
            context={"token_id":token,"condition":condition,"received_at":time.time(),
                     "tick_size":str(book.get("tick_size","")),"min_order_size":str(book.get("min_order_size","")),
                     "accepting_orders":meta.get("ao") is True,
                     "best_bid":max(bids,key=dec) if bids else None,"best_ask":min(asks,key=dec) if asks else None}
            if dec(context["tick_size"])!=dec(str(meta.get("mts",""))) or dec(context["min_order_size"])!=dec(str(meta.get("mos",""))):
                raise OrderError("Market constraints disagree across responses")
            self.markets[key]=context
            if len(self.markets)>20:self.markets.pop(next(iter(self.markets)))
            return context

def handler(app):
 class Handler(BaseHTTPRequestHandler):
    server_version="PolymarketPreparedTerminal"
    def log_message(self,*args):pass
    def reply(self,status,payload,kind="application/json; charset=utf-8"):
        body=json.dumps(payload,ensure_ascii=False).encode() if isinstance(payload,(dict,list)) else payload
        self.send_response(status)
        for k,v in {"content-type":kind,"cache-control":"no-store","x-content-type-options":"nosniff",
                    "referrer-policy":"no-referrer","content-security-policy":"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'","content-length":str(len(body))}.items():self.send_header(k,v)
        self.end_headers();self.wfile.write(body)
    def authorized(self):
        return hmac.compare_digest(self.headers.get("X-Polymarket-Internal",""),app.token)
    def do_GET(self):
        if not self.authorized():return self.reply(401,{"error":"AUTH_REQUIRED"})
        u=urllib.parse.urlsplit(self.path)
        try:
            if u.path=="/polymarket/api/snapshot":return self.reply(200,app.snapshot())
            if u.path=="/polymarket/api/market":
                qs=urllib.parse.parse_qs(u.query)
                return self.reply(200,app.context(qs.get("token_id",[""])[0],qs.get("condition",[""])[0]))
            static={"/polymarket/":("index.html","text/html; charset=utf-8"),
                    "/polymarket/app.js":("app.js","text/javascript; charset=utf-8"),
                    "/polymarket/icon.svg":("icon.svg","image/svg+xml"),
                    "/polymarket/style.css":("style.css","text/css; charset=utf-8")}
            if u.path in static:
                name,kind=static[u.path];return self.reply(200,(BASE/name).read_bytes(),kind)
            return self.reply(404,{"error":"NOT_FOUND"})
        except OrderError as e:return self.reply(400,{"error":str(e)})
        except Exception as e:return self.reply(503,{"error":type(e).__name__})
    def do_POST(self):
        if not self.authorized():return self.reply(401,{"error":"AUTH_REQUIRED"})
        # Production viewer proxy must forward a validated canonical origin and this private internal token.
        if self.headers.get("X-Polymarket-Origin-Verified")!="1":return self.reply(403,{"error":"ORIGIN_REQUIRED"})
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 0<size<=MAX_BODY:return self.reply(413,{"error":"BODY_LIMIT"})
            data=json.loads(self.rfile.read(size))
            path=urllib.parse.urlsplit(self.path).path
            if path=="/polymarket/api/drafts":
                context=app.context(data.get("token_id",""),data.get("condition",""))
                return self.reply(201,app.journal.draft(data,context,data.get("client_id")))
            if path=="/polymarket/api/submit":
                # Reject before any transmission; no generic caller can override the access gate.
                return self.reply(409,{"ok":False,"submitted":False,"reasons":app.live_reasons()})
            if path=="/polymarket/api/cancel":
                return self.reply(200,app.execution.cancel(data.get("id")))
            return self.reply(404,{"error":"NOT_FOUND"})
        except (OrderError,ValueError,TypeError) as e:return self.reply(400,{"error":str(e)})
        except Exception as e:return self.reply(503,{"error":type(e).__name__})
 return Handler

def main():
    os.umask(0o077)
    p=argparse.ArgumentParser()
    p.add_argument("--port",type=int,default=19081)
    p.add_argument("--state")
    args=p.parse_args()
    app=Application(args.state)
    server=ThreadingHTTPServer(("127.0.0.1",args.port),handler(app))
    server.daemon_threads=True
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
if __name__=="__main__":main()
