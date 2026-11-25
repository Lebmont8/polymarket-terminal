"""Bounded read-only readers. Never import or call paper workers."""
from pathlib import Path
import json,sqlite3,time,os
ROOT=Path(os.environ.get("POLYMARKET_PAPER_ROOT",str(Path(__file__).resolve().parent/"sample-data")))
PAPERS=[
 ("btc","BTC основной","polymarket-forward-2881","paper.sqlite"),
 ("btc-limits","BTC лимитные входы","polymarket-limits-2916","paper-v3.sqlite"),
 ("hype","HYPE правила","polymarket-hype-paper-3213","paper.sqlite"),
 ("hype-seven","HYPE семь + combined","polymarket-hype-seven-3295","paper.sqlite")]
def json_file(path,limit=2*1024*1024):
    with open(path,"rb") as f:
        body=f.read(limit+1)
    if len(body)>limit: raise ValueError("source file size limit")
    return json.loads(body)
def paper_snapshot(root=ROOT):
    out=[]
    for key,title,directory,db_name in PAPERS:
        p=Path(root)/directory
        result={"id":key,"title":title,"mode":"paper","read_only":True,"accounts":[],"activity":[]}
        try:
            s=json_file(p/"status.json")
            at=s.get("at",0)
            result.update(phase=s.get("phase","UNKNOWN"),at=at,age_seconds=max(0,time.time()-at),stale=time.time()-at>180,
                          last_error=s.get("last_error"),execution_claim=s.get("execution_claim"),
                          source_active_error=(s.get("source_health") or {}).get("active_error"),
                          real_orders=s.get("real_orders"),coverage=s.get("coverage"))
            accounts=s.get("accounts")
            if not isinstance(accounts,dict):
                accounts={a:s[a] for a in ("fixed5","bank5pct") if isinstance(s.get(a),dict)}
            for name,a in accounts.items():
                result["accounts"].append({"name":name,**{k:a.get(k) for k in ("cash","cost_equity","net_settled","settled","wins","open","dd_pct")}})
            c=sqlite3.connect((p/db_name).as_uri()+"?mode=ro",uri=True,timeout=.15)
            try:
                c.execute("PRAGMA query_only=ON")
                c.execute("PRAGMA busy_timeout=150")
                c.row_factory=sqlite3.Row
                rows=c.execute("SELECT * FROM fills ORDER BY rowid DESC LIMIT 60").fetchall()
                for row in rows:
                    x=dict(row)
                    try: detail=json.loads(x.get("data") or "{}")
                    except (ValueError,TypeError): detail={}
                    result["activity"].append({
                        "mode":"paper","account":x.get("account",x.get("strategy")),
                        "condition":x.get("condition"),"shares":x.get("shares"),
                        "cost":x.get("cost"),"fee":x.get("fee"),"payout":x.get("payout"),
                        "source_status":x.get("status"),
                        "slug":detail.get("slug"),"side":detail.get("side"),"transaction_hash":None})
            finally: c.close()
        except Exception as e:
            result.update(read_error=type(e).__name__,stale=True)
        out.append(result)
    return out
