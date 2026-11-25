"""Isolated order-intent journal. No signer or live network is configured by default."""
import json, sqlite3, time, uuid, hashlib, threading
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager

class OrderError(ValueError):
    pass

def dec(v):
    if not isinstance(v, str) or len(v)>40:
        raise OrderError("Decimal values must be strings")
    try:
        d=Decimal(v)
    except InvalidOperation:
        raise OrderError("Invalid decimal")
    if not d.is_finite() or len(d.as_tuple().digits)>18 or d.as_tuple().exponent < -12:
        raise OrderError("Non-finite decimal")
    return d

def validate(data, market, now=None):
    now=time.time() if now is None else now
    if not isinstance(data,dict) or not isinstance(market,dict):
        raise OrderError("Invalid order")
    token=data.get("token_id","")
    if not isinstance(token,str) or not token.isdigit() or not 1<=len(token)<=78:
        raise OrderError("Invalid token_id")
    if token!=market.get("token_id"):
        raise OrderError("Market identity mismatch")
    if market.get("accepting_orders") is not True or now-market.get("received_at",0)>5:
        raise OrderError("Market context missing or stale")
    side=data.get("side")
    typ=data.get("order_type","GTC")
    post=data.get("post_only",False)
    if side not in ("BUY","SELL") or typ not in ("GTC","GTD","FOK","FAK") or not isinstance(post,bool):
        raise OrderError("Invalid order type or side")
    if post and typ not in ("GTC","GTD"):
        raise OrderError("Post-only requires GTC/GTD")
    price,size,tick,minimum=[dec(x) for x in (data.get("price"),data.get("size"),market.get("tick_size"),market.get("min_order_size"))]
    if not Decimal(0)<price<Decimal(1) or tick<=0 or price%tick:
        raise OrderError("Price does not conform to market tick")
    if size<minimum or size<=0 or size.as_tuple().exponent < -2:
        raise OrderError("Size is below minimum or has excess precision")
    expiry=data.get("expiration",0)
    if isinstance(expiry,bool) or not isinstance(expiry,int):
        raise OrderError("Invalid expiration")
    if typ=="GTD" and expiry<now+60:
        raise OrderError("GTD requires a future expiration with at least 60 seconds padding")
    if typ!="GTD" and expiry!=0:
        raise OrderError("Expiration is only supported for GTD")
    if post:
        opposite=market.get("best_ask" if side=="BUY" else "best_bid")
        if opposite is not None and ((side=="BUY" and price>=dec(opposite)) or (side=="SELL" and price<=dec(opposite))):
            raise OrderError("Post-only would cross the observed spread")
    return dict(token_id=token,side=side,order_type=typ,post_only=post,price=str(price),size=str(size),expiration=expiry)

class Journal:
    def __init__(self,path):
        self.path=str(path)
        self.lock=threading.RLock()
        with self.db() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS orders(
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL,
                exchange_id TEXT, filled TEXT NOT NULL DEFAULT '0', confirmed TEXT NOT NULL DEFAULT '0',
                settlement TEXT NOT NULL DEFAULT 'UNCONFIRMED', created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY, order_id TEXT NOT NULL, at REAL NOT NULL,
                kind TEXT NOT NULL, detail TEXT NOT NULL, dedupe TEXT UNIQUE);
            """)
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path,timeout=2)
        c.row_factory=sqlite3.Row
        c.execute("PRAGMA busy_timeout=2000")
        try:
            with c:
                yield c
        finally:
            c.close()
    def draft(self,data,market,client_id=None):
        p=validate(data,market)
        client_id=client_id or str(uuid.uuid4())
        if not isinstance(client_id,str) or not 1<=len(client_id)<=100 or any(not(x.isalnum() or x in "-_") for x in client_id):
            raise OrderError("Invalid client id")
        payload=json.dumps(p,sort_keys=True,separators=(",",":"))
        with self.lock,self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            old=c.execute("SELECT * FROM orders WHERE id=?",(client_id,)).fetchone()
            if old:
                if old["payload"]!=payload: raise OrderError("Idempotency key conflicts with another order")
                return dict(old)
            t=time.time()
            c.execute("INSERT INTO orders(id,payload,state,created,updated) VALUES(?,?,'DRAFT',?,?)",(client_id,payload,t,t))
            c.execute("INSERT INTO events(order_id,at,kind,detail) VALUES(?,?,'DRAFT','local only; not submitted')",(client_id,t))
        return self.get(client_id)
    def get(self,oid):
        with self.db() as c:
            r=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
            if not r: raise OrderError("Unknown order")
            return dict(r)
    def list(self):
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM orders ORDER BY created DESC LIMIT 100")]
    def transition(self,oid,allowed,state,exchange_id=None):
        with self.lock,self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            r=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
            if not r or r["state"] not in allowed:
                raise OrderError("Order cannot transition from its current state")
            c.execute("UPDATE orders SET state=?,exchange_id=COALESCE(?,exchange_id),updated=? WHERE id=?",(state,exchange_id,time.time(),oid))
            c.execute("INSERT INTO events(order_id,at,kind,detail) VALUES(?,?,?,?)",(oid,time.time(),state,""))
        return self.get(oid)
    def observe(self,oid,event_id,exchange_id,cumulative_filled,kind):
        if kind not in ("LIVE","PARTIAL","FILLED","CANCELLED","CONFIRMED","FAILED"):
            raise OrderError("Unsupported exchange observation")
        if not isinstance(event_id,str) or not event_id or len(event_id)>200 or not isinstance(exchange_id,str) or not exchange_id:
            raise OrderError("Missing exchange observation identity")
        qty=dec(cumulative_filled)
        detail=json.dumps({"quantity":str(qty),"exchange_id":exchange_id},sort_keys=True)
        with self.lock,self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            seen=c.execute("SELECT order_id,kind,detail FROM events WHERE dedupe=?",(event_id,)).fetchone()
            if seen:
                if (seen["order_id"],seen["kind"],seen["detail"])!=(oid,kind,detail):
                    raise OrderError("Conflicting duplicate event")
                return self.get(oid)
            r=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
            if not r or r["state"] not in ("SUBMITTING","UNKNOWN","LIVE","PARTIAL","FILLED","CANCEL_PENDING","CANCELLED") or (r["exchange_id"] and r["exchange_id"]!=exchange_id):
                raise OrderError("Observation identity/state mismatch")
            total=dec(json.loads(r["payload"])["size"])
            old=dec(r["filled"])
            confirmed=dec(r["confirmed"])
            state,filled,settlement=r["state"],old,r["settlement"]
            if qty<0 or qty>total:raise OrderError("Invalid cumulative quantity")
            if kind in ("CONFIRMED","FAILED"):
                if old<=0 or qty>old:raise OrderError("Settlement requires matching evidence")
                if kind=="CONFIRMED":
                    if qty<confirmed:raise OrderError("Confirmed quantity cannot decrease")
                    confirmed=qty
                    settlement="CONFIRMED" if confirmed==old else "PARTIAL"
                else:settlement="FAILED"
            else:
                if qty<old:raise OrderError("Cumulative fill cannot decrease")
                if kind=="FILLED" and qty!=total:raise OrderError("Filled quantity differs from order size")
                if kind=="PARTIAL" and not 0<qty<total:raise OrderError("Invalid partial fill")
                filled=qty
                state=kind
                if qty==total:state="FILLED"
                elif kind=="LIVE" and (old>0 or r["state"]=="CANCELLED"):state=r["state"]
                elif r["state"]=="CANCEL_PENDING" and kind in ("LIVE","PARTIAL"):state="CANCEL_PENDING"
                elif r["state"]=="CANCELLED" and kind=="PARTIAL":state="CANCELLED"
                if filled>confirmed and settlement=="CONFIRMED":settlement="PARTIAL"
            c.execute("UPDATE orders SET state=?,filled=?,confirmed=?,settlement=?,exchange_id=?,updated=? WHERE id=?",
                      (state,str(filled),str(confirmed),settlement,exchange_id,time.time(),oid))
            c.execute("INSERT INTO events(order_id,at,kind,detail,dedupe) VALUES(?,?,?,?,?)",(oid,time.time(),kind,detail,event_id))
        return self.get(oid)

class Execution:
    def __init__(self,journal,transport=None,gate=None,max_notional="10"):
        self.journal=journal
        self.transport=transport
        self.gate=gate or (lambda: ["LIVE_NOT_CONFIGURED"])
        self.max_notional=dec(max_notional)
    def submit(self,oid,market):
        order=self.journal.get(oid)
        if order["state"]!="DRAFT":
            raise OrderError("Already attempted; reconcile instead of resubmitting")
        data=validate(json.loads(order["payload"]),market)
        reasons=list(self.gate())
        if self.transport is None: reasons.append("TRADING_ACCOUNT_NOT_CONNECTED")
        if dec(data["price"])*dec(data["size"])>self.max_notional:
            reasons.append("NOTIONAL_LIMIT")
        if reasons:
            return {"ok":False,"submitted":False,"reasons":reasons,"order":order}
        # Durably reserve the attempt BEFORE any outbound operation.
        self.journal.transition(oid,("DRAFT",),"SUBMITTING")
        try:
            result=self.transport.submit(data,oid)
        except Exception:
            self.journal.transition(oid,("SUBMITTING",),"UNKNOWN")
            return {"ok":False,"submitted":None,"reasons":["OUTCOME_UNKNOWN_RECONCILE_REQUIRED"]}
        state=result.get("state")
        if state not in ("LIVE","PARTIAL","FILLED","REJECTED") or (state!="REJECTED" and not result.get("order_id")):
            self.journal.transition(oid,("SUBMITTING",),"UNKNOWN")
            return {"ok":False,"submitted":None,"reasons":["UNRECOGNIZED_RESPONSE"]}
        if state in ("PARTIAL","FILLED"):
            row=self.journal.observe(oid,"submit:"+oid,result["order_id"],result.get("filled","0"),state)
        else:
            row=self.journal.transition(oid,("SUBMITTING",),state,result.get("order_id"))
        return {"ok":state!="REJECTED","submitted":True,"order":row}
    def cancel(self,oid):
        row=self.journal.get(oid)
        if row["state"]=="DRAFT":
            return self.journal.transition(oid,("DRAFT",),"CANCELLED_LOCAL")
        if row["state"] not in ("LIVE","PARTIAL") or not self.transport:
            raise OrderError("Cancellation needs a known live order and connected transport")
        self.journal.transition(oid,("LIVE","PARTIAL"),"CANCEL_PENDING")
        try:
            result=self.transport.cancel(row["exchange_id"])
        except Exception:
            # Do not assume a cancellation succeeded on timeout.
            return self.journal.get(oid)
        if result.get("cancelled") is True:
            return self.journal.observe(oid,"cancel:"+oid,row["exchange_id"],self.journal.get(oid)["filled"],"CANCELLED")
        return self.journal.get(oid)
