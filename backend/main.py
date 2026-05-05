from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import psycopg2
from psycopg2 import pool as psycopg2_pool
import os, json, hmac, hashlib, time, requests
import httpx
from datetime import datetime, date, timedelta, timezone
from typing import Optional
import razorpay
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# ── ENV ───────────────────────────────────────────────────────────────────────
DATABASE_URL        = os.getenv("DATABASE_URL")
FIREBASE_API_KEY    = os.getenv("FIREBASE_API_KEY")
RAZORPAY_KEY_ID     = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
BREVO_API_KEY       = os.getenv("BREVO_API_KEY")
SENDER_EMAIL        = os.getenv("SENDER_EMAIL", "noreply@urbanease.in")
SENDER_NAME         = os.getenv("SENDER_NAME", "Urban Ease")
IST                 = ZoneInfo("Asia/Kolkata")

rzp = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

app = FastAPI(title="UrbanEase Admin")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000",
        "http://localhost:3000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── DB POOL ───────────────────────────────────────────────────────────────────
_pool: Optional[psycopg2_pool.ThreadedConnectionPool] = None

def pool():
    global _pool
    if _pool is None:
        _pool = psycopg2_pool.ThreadedConnectionPool(2, 10, dsn=DATABASE_URL)
    return _pool

def qry(sql, params=()):
    global _pool
    for attempt in range(2):
        c = pool().getconn()
        try:
            cur = c.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            pool().putconn(c)
            return rows
        except psycopg2.OperationalError:
            pool().putconn(c, close=True)
            if attempt == 0:
                _pool = None  # force pool rebuild on retry
                continue
            raise
        except Exception:
            pool().putconn(c)
            raise

def exe(sql, params=()):
    global _pool
    for attempt in range(2):
        c = pool().getconn()
        try:
            cur = c.cursor()
            cur.execute(sql, params)
            c.commit()
            cur.close()
            pool().putconn(c)
            return
        except psycopg2.OperationalError:
            pool().putconn(c, close=True)
            if attempt == 0:
                _pool = None  # force pool rebuild on retry
                continue
            raise
        except Exception:
            pool().putconn(c)
            raise

def sj(v, d):
    if v is None: return d
    if isinstance(v, (dict, list)): return v
    try: return json.loads(v)
    except: return d

def t24(s):
    p, m = s.strip().split(" "); h, mn = map(int, p.split(":"))
    if m == "PM" and h != 12: h += 12
    if m == "AM" and h == 12: h = 0
    return f"{h:02d}:{mn:02d}"

# ── FIREBASE AUTH ─────────────────────────────────────────────────────────────
async def verify_token(request: Request):
    ah = request.headers.get("Authorization", "")
    if not ah.startswith("Bearer "): raise HTTPException(401, "Missing token")
    tok = ah.split(" ", 1)[1].strip()
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={FIREBASE_API_KEY}"
    async with httpx.AsyncClient(timeout=5) as cl:
        r = await cl.post(url, json={"idToken": tok})
    if r.status_code != 200: raise HTTPException(401, "Invalid token")
    users = r.json().get("users")
    if not users: raise HTTPException(401, "No user")
    u = users[0]
    return {"uid": u.get("localId"), "email": u.get("email"), "name": u.get("displayName", "")}

async def admin_only(request: Request):
    
    p = await verify_token(request)
    rows = qry("SELECT role FROM users WHERE firebase_uid = %s", (p["uid"],))
    if not rows or rows[0][0] not in ("admin", "superadmin"):
        raise HTTPException(403, "Admin only")
    return p

# ── SERVE FRONTEND ────────────────────────────────────────────────────────────
FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")



# ══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/admin/dashboard")
async def dashboard(request: Request):
    # # await admin_only(request)
    today = date.today()
    ms = today.replace(day=1)

    def n(rows): return int(rows[0][0]) if rows else 0
    def f(rows): return float(rows[0][0]) if rows and rows[0][0] else 0.0

    rev_total  = f(qry("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid'"))
    rev_today  = f(qry("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid' AND DATE(created_at)=%s",(today,)))
    rev_month  = f(qry("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid' AND created_at>=%s",(ms,)))
    ord_total  = n(qry("SELECT COUNT(*) FROM orders"))
    ord_today  = n(qry("SELECT COUNT(*) FROM orders WHERE DATE(created_at)=%s",(today,)))
    ord_paid   = n(qry("SELECT COUNT(*) FROM orders WHERE status='paid'"))
    ord_cancel = n(qry("SELECT COUNT(*) FROM orders WHERE status='cancelled'"))
    usr_total  = n(qry("SELECT COUNT(*) FROM users"))
    usr_today  = n(qry("SELECT COUNT(*) FROM users WHERE DATE(created_at)=%s",(today,)))
    prov_total = n(qry("SELECT COUNT(*) FROM providers"))
    prov_act   = n(qry("SELECT COUNT(*) FROM providers WHERE is_active=TRUE"))
    prov_busy  = n(qry("SELECT COUNT(*) FROM providers WHERE is_busy=TRUE"))
    sl_avail   = n(qry("SELECT COUNT(*) FROM slot_inventory WHERE available>0 AND is_blocked=FALSE AND date>=%s",(today,)))
    sl_block   = n(qry("SELECT COUNT(*) FROM slot_inventory WHERE is_blocked=TRUE AND date>=%s",(today,)))

    chart = qry("""
        SELECT DATE(created_at), COALESCE(SUM(amount),0)
        FROM orders WHERE status='paid' AND created_at>=NOW()-INTERVAL '7 days'
        GROUP BY DATE(created_at) ORDER BY 1
    """)
    top_svc = qry("""
    SELECT title, rating FROM (
        SELECT title, rating FROM services
        UNION ALL
        SELECT title, rating FROM men_services
        UNION ALL
        SELECT title, rating FROM packages
        UNION ALL
        SELECT title, rating FROM men_packages
    ) all_svc
    WHERE rating IS NOT NULL
    ORDER BY rating DESC
    LIMIT 6
""")

    return {
        "revenue":  {"total":rev_total/100,"today":rev_today/100,"month":rev_month/100},
        "orders":   {"total":ord_total,"today":ord_today,"paid":ord_paid,"cancelled":ord_cancel},
        "users":    {"total":usr_total,"today":usr_today},
        "providers":{"total":prov_total,"active":prov_act,"busy":prov_busy},
        "slots":    {"available":sl_avail,"blocked":sl_block},
        "chart":    [{"date":str(r[0]),"amount":float(r[1])/100} for r in chart],
        "top_services":[{"title":r[0],"rating":float(r[1])} for r in top_svc],
    }

# ══════════════════════════════════════════════════════════════════════════════
#  ORDERS
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/admin/orders")
async def list_orders(request: Request, page:int=1, limit:int=20, status:str="", search:str=""):
    # # await admin_only(request)
    off = (page-1)*limit
    w, p = "WHERE 1=1", []
    if status: w += " AND o.status=%s"; p.append(status)
    if search:
        w += " AND (o.razorpay_order_id ILIKE %s OR u.email ILIKE %s OR u.name ILIKE %s)"
        p += [f"%{search}%"]*3
    total = qry(f"SELECT COUNT(*) FROM orders o JOIN users u ON o.firebase_uid=u.firebase_uid {w}", p)
    rows  = qry(f"""
        SELECT o.id,o.razorpay_order_id,o.razorpay_payment_id,o.amount,o.status,o.created_at,
               o.address,o.slots,o.cart,o.quantities,u.name,u.email,o.refund_id,o.cancelled_at
        FROM orders o JOIN users u ON o.firebase_uid=u.firebase_uid
        {w} ORDER BY o.created_at DESC LIMIT %s OFFSET %s
    """, p+[limit,off])
    return {
        "total": int(total[0][0]), "page": page,
        "data": [{"id":r[0],"razorpay_order_id":r[1],"razorpay_payment_id":r[2],
                  "amount":float(r[3])/100 if r[3] else 0,"status":r[4],
                  "created_at":r[5].isoformat() if r[5] else None,
                  "address":sj(r[6],{}),"slots":sj(r[7],{}),"cart":sj(r[8],[]),
                  "quantities":sj(r[9],{}),"user_name":r[10],"user_email":r[11],
                  "refund_id":r[12],"cancelled_at":r[13].isoformat() if r[13] else None}
                 for r in rows]
    }

@app.get("/api/admin/orders/{oid}")
async def get_order(oid:int, request:Request):
    rows = qry("""
        SELECT 
            o.id,
            o.razorpay_order_id,
            o.razorpay_payment_id,
            o.amount,
            o.status,
            o.address,
            o.slots,
            o.cart,
            o.created_at,
            o.cancelled_at,
            o.refund_id,
            u.name,
            u.email
        FROM orders o
        JOIN users u ON o.firebase_uid=u.firebase_uid
        WHERE o.id=%s
    """,(oid,))

    if not rows:
        raise HTTPException(404,"Not found")

    r = rows[0]

    return {
        "id": r[0],
        "razorpay_order_id": r[1],
        "razorpay_payment_id": r[2],
        "amount": float(r[3])/100 if r[3] else 0,
        "status": r[4],
        "address": sj(r[5], {}),
        "slots": sj(r[6], {}),
        "cart": sj(r[7], []),

        # ✅ SAFE datetime handling
        "created_at": r[8].isoformat() if hasattr(r[8], "isoformat") else str(r[8]),
        "cancelled_at": r[9].isoformat() if r[9] and hasattr(r[9], "isoformat") else None,

        "refund_id": r[10],
        "user_name": r[11],
        "user_email": r[12]
    }

@app.patch("/api/admin/orders/{oid}/status")
async def upd_order_status(oid:int, request:Request):
    # # await admin_only(request)
    body = await request.json()
    st = body.get("status")
    if st not in ("paid","cancelled","completed","refunded"): raise HTTPException(400,"Bad status")
    exe("UPDATE orders SET status=%s WHERE id=%s",(st,oid))
    return {"ok":True}

@app.post("/api/admin/orders/{oid}/refund")
async def refund_order(oid:int, request:Request):
    # # await admin_only(request)
    body = await request.json()
    amt  = int(body.get("amount_paise",0))
    rows = qry("SELECT razorpay_payment_id,amount FROM orders WHERE id=%s",(oid,))
    if not rows: raise HTTPException(404,"Not found")
    pid, total = rows[0]
    try:
        ref = rzp.payment.refund(pid,{"amount": amt or int(total)})
        exe("UPDATE orders SET refund_id=%s,status='refunded' WHERE id=%s",(ref["id"],oid))
        return {"ok":True,"refund_id":ref["id"]}
    except Exception as e: raise HTTPException(500,str(e))

# ══════════════════════════════════════════════════════════════════════════════
#  USERS
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/admin/users")
async def list_users(request:Request, page:int=1, limit:int=20, search:str=""):
    off = (page-1)*limit
    w, p = "WHERE 1=1", []

    if search:
        w += " AND (u.name ILIKE %s OR u.email ILIKE %s)"
        p += [f"%{search}%"]*2

    total = qry(f"SELECT COUNT(*) FROM users u {w}", p)

    rows = qry(f"""
        SELECT u.id,u.firebase_uid,u.name,u.email,u.created_at,
               COUNT(o.id),COALESCE(SUM(o.amount),0)
        FROM users u
        LEFT JOIN orders o ON u.firebase_uid=o.firebase_uid AND o.status='paid'
        {w}
        GROUP BY u.id,u.firebase_uid,u.name,u.email,u.created_at
        ORDER BY u.created_at DESC
        LIMIT %s OFFSET %s
    """, p+[limit,off])

    return {
        "total": int(total[0][0]),
        "page": page,
        "data": [
            {
                "id": r[0],
                "uid": r[1],
                "name": r[2],
                "email": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
                "orders": int(r[5]),
                "ltv": float(r[6])/100
            }
            for r in rows
        ]
    }

@app.patch("/api/admin/users/{uid}/role")
async def upd_role(uid:str, request:Request):
    # await admin_only(request)
    body = await request.json()
    role = body.get("role")
    if role not in ("user","admin","superadmin"): raise HTTPException(400,"Bad role")
    exe("UPDATE users SET role=%s WHERE firebase_uid=%s",(role,uid))
    return {"ok":True}

# ══════════════════════════════════════════════════════════════════════════════
#  PROVIDERS
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/admin/providers")
async def list_providers(request:Request, page:int=1, limit:int=20, search:str=""):
    # await admin_only(request)
    off=(page-1)*limit; w,p="WHERE 1=1",[]
    if search: w+=" AND (name ILIKE %s OR phone ILIKE %s)"; p+=[f"%{search}%"]*2
    total = qry(f"SELECT COUNT(*) FROM providers {w}",p)
    rows  = qry(f"""
        SELECT id,name,phone,rating,total_jobs,is_active,is_busy,last_assigned_at,created_at
        FROM providers {w} ORDER BY rating DESC NULLS LAST LIMIT %s OFFSET %s
    """,p+[limit,off])
    return {"total":int(total[0][0]),"page":page,"data":[
        {"id":r[0],"name":r[1],"phone":r[2],"rating":float(r[3]) if r[3] else 0,
         "total_jobs":int(r[4]) if r[4] else 0,"is_active":r[5],"is_busy":r[6],
         "last_assigned_at":r[7].isoformat() if r[7] else None,
         "created_at":r[8].isoformat() if r[8] else None}
        for r in rows]}

@app.post("/api/admin/providers")
async def create_provider(request:Request):
    # await admin_only(request)
    b = await request.json()
    name=b.get("name","").strip(); phone=b.get("phone","").strip(); rating=float(b.get("rating",4.0))
    if not name or not phone: raise HTTPException(400,"name+phone required")
    c=pool().getconn()
    try:
        cur=c.cursor()
        cur.execute("INSERT INTO providers(name,phone,rating,total_jobs,is_active,is_busy) VALUES(%s,%s,%s,0,TRUE,FALSE) RETURNING id",(name,phone,rating))
        nid=cur.fetchone()[0]; c.commit(); cur.close()
    finally: pool().putconn(c)
    return {"ok":True,"id":nid}

@app.patch("/api/admin/providers/{pid}")
async def upd_provider(pid:int, request:Request):
    # await admin_only(request)
    b=await request.json(); flds,p=[],[]
    for col in ("name","phone","rating","is_active","is_busy"):
        if col in b: flds.append(f"{col}=%s"); p.append(b[col])
    if not flds: raise HTTPException(400,"No fields")
    exe(f"UPDATE providers SET {','.join(flds)} WHERE id=%s",p+[pid])
    return {"ok":True}

@app.delete("/api/admin/providers/{pid}")
async def del_provider(pid:int, request:Request):
    # await admin_only(request)
    exe("UPDATE providers SET is_active=FALSE WHERE id=%s",(pid,))
    return {"ok":True}

# ══════════════════════════════════════════════════════════════════════════════
#  SLOTS
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/admin/slots")
async def list_slots(request:Request, date_from:str="", date_to:str=""):
    # await admin_only(request)
    td=date.today()
    df=date_from or td.isoformat()
    dt=date_to or (td+timedelta(days=14)).isoformat()
    rows=qry("""
        SELECT date,time,capacity,base_capacity,available,booked,locked,is_blocked,block_reason
        FROM slot_inventory WHERE date BETWEEN %s AND %s ORDER BY date,time
    """,(df,dt))
    return {"data":[{"date":str(r[0]),"time":r[1],"capacity":r[2],"base_capacity":r[3],
                     "available":r[4],"booked":r[5],"locked":r[6],"is_blocked":r[7],"block_reason":r[8]}
                    for r in rows]}

@app.patch("/api/admin/slots/{sd}/{st}/block")
async def block_slot(sd:str, st:str, request:Request):
    # await admin_only(request)
    b=await request.json(); blocked=b.get("is_blocked",True); reason=b.get("reason","admin_block")
    exe("UPDATE slot_inventory SET is_blocked=%s,block_reason=%s WHERE date=%s AND time=%s",
        (blocked,reason if blocked else None,sd,st))
    return {"ok":True}

@app.patch("/api/admin/slots/{sd}/{st}/capacity")
async def upd_capacity(sd:str, st:str, request:Request):
    # await admin_only(request)
    b=await request.json(); cap=int(b.get("capacity",5))
    exe("UPDATE slot_inventory SET capacity=%s,available=GREATEST(0,%s-booked-locked) WHERE date=%s AND time=%s",
        (cap,cap,sd,st))
    return {"ok":True}

@app.post("/api/admin/slots/generate")
async def gen_slots(request:Request):
    # await admin_only(request)
    b=await request.json()
    days=int(b.get("days",14)); cap=int(b.get("capacity",5))
    times=b.get("times",["09:00 AM","11:00 AM","01:00 PM","03:00 PM","05:00 PM","07:00 PM"])
    c=pool().getconn()
    try:
        cur=c.cursor(); count=0
        for i in range(days):
            d=date.today()+timedelta(days=i)
            for t in times:
                cur.execute("INSERT INTO slot_inventory(date,time,capacity,base_capacity,available) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(date,time) DO NOTHING",(d,t,cap,cap,cap))
                count+=cur.rowcount
        c.commit(); cur.close()
    finally: pool().putconn(c)
    return {"ok":True,"created":count}

# ══════════════════════════════════════════════════════════════════════════════
#  SERVICES & DISCOUNTS
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/admin/services")
async def list_services(request:Request, category:str=""):
    # await admin_only(request)
    w,p="",[]
    if category: w="WHERE category=%s"; p=[category]
    rows=qry(f"SELECT id,category,title,price,old_price,rating,reviews,badge FROM services {w} ORDER BY category,id",p)
    return {"data":[{"id":r[0],"category":r[1],"title":r[2],"price":r[3],"old_price":r[4],"rating":r[5],"reviews":r[6],"badge":r[7]} for r in rows]}

@app.patch("/api/admin/services/{sid}")
async def upd_service(sid:int, request:Request):
    # await admin_only(request)
    b=await request.json(); flds,p=[],[]
    for col in ("title","price","old_price","rating","reviews","badge","category"):
        if col in b: flds.append(f"{col}=%s"); p.append(b[col])
    exe(f"UPDATE services SET {','.join(flds)} WHERE id=%s",p+[sid])
    return {"ok":True}

@app.get("/api/admin/discounts")
async def list_discounts(request:Request):
    # await admin_only(request)
    rows=qry("SELECT id,code,title,description,is_active,sort_order FROM discounts ORDER BY sort_order")
    return {"data":[{"id":r[0],"code":r[1],"title":r[2],"description":r[3],"is_active":r[4],"sort_order":r[5]} for r in rows]}

@app.post("/api/admin/discounts")
async def create_discount(request:Request):
    # await admin_only(request)
    b=await request.json()
    c=pool().getconn()
    try:
        cur=c.cursor()
        cur.execute("INSERT INTO discounts(code,title,description,is_active,sort_order) VALUES(%s,%s,%s,%s,%s) RETURNING id",
                    (b["code"],b["title"],b.get("description",""),b.get("is_active",True),b.get("sort_order",0)))
        nid=cur.fetchone()[0]; c.commit(); cur.close()
    finally: pool().putconn(c)
    return {"ok":True,"id":nid}

@app.patch("/api/admin/discounts/{did}")
async def upd_discount(did:int, request:Request):
    # await admin_only(request)
    b=await request.json(); flds,p=[],[]
    for col in ("code","title","description","is_active","sort_order"):
        if col in b: flds.append(f"{col}=%s"); p.append(b[col])
    exe(f"UPDATE discounts SET {','.join(flds)} WHERE id=%s",p+[did])
    return {"ok":True}

@app.delete("/api/admin/discounts/{did}")
async def del_discount(did:int, request:Request):
    # await admin_only(request)
    exe("DELETE FROM discounts WHERE id=%s",(did,))
    return {"ok":True}

# ══════════════════════════════════════════════════════════════════════════════
#  ANALYTICS
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/admin/analytics/revenue")
async def analytics_revenue(request:Request, days:int=30):
    # await admin_only(request)
    rows=qry(f"SELECT DATE(created_at),COUNT(*),SUM(amount) FROM orders WHERE status='paid' AND created_at>=NOW()-INTERVAL '{days} days' GROUP BY DATE(created_at) ORDER BY 1")
    return {"data":[{"date":str(r[0]),"orders":int(r[1]),"revenue":float(r[2])/100} for r in rows]}

@app.get("/api/admin/analytics/providers")
async def analytics_providers(request:Request):
    # await admin_only(request)
    rows=qry("""
        SELECT p.name,p.rating,p.total_jobs,p.is_busy,COUNT(pa.id)
        FROM providers p LEFT JOIN provider_assignments pa ON p.id=pa.provider_id
        WHERE p.is_active=TRUE GROUP BY p.id,p.name,p.rating,p.total_jobs,p.is_busy
        ORDER BY p.total_jobs DESC
    """)
    return {"data":[{"name":r[0],"rating":float(r[1]) if r[1] else 0,"jobs":int(r[2]) if r[2] else 0,"busy":r[3],"assignments":int(r[4])} for r in rows]}

@app.get("/api/admin/analytics/slots")
async def analytics_slots(request:Request):
    # await admin_only(request)
    td=date.today()
    rows=qry("SELECT date,SUM(capacity),SUM(booked),SUM(available) FROM slot_inventory WHERE date>=%s AND date<=%s GROUP BY date ORDER BY date",
             (td,td+timedelta(days=14)))
    return {"data":[{"date":str(r[0]),"capacity":int(r[1]),"booked":int(r[2]),"available":int(r[3])} for r in rows]}

# ══════════════════════════════════════════════════════════════════════════════
#  CRON JOBS (manual trigger)
# ══════════════════════════════════════════════════════════════════════════════
@app.post("/api/admin/cron/generate-slots")
async def cron_gen(request:Request):
    # await admin_only(request)
    
    c=pool().getconn()
    try:
        cur=c.cursor(); td=date.today(); count=0
        times=["09:00 AM","11:00 AM","01:00 PM","03:00 PM","05:00 PM","07:00 PM"]
        for i in range(14):
            for t in times:
                cur.execute("INSERT INTO slot_inventory(date,time,capacity,base_capacity,available) VALUES(%s,%s,5,5,5) ON CONFLICT(date,time) DO NOTHING",(td+timedelta(days=i),t))
                count+=cur.rowcount
        c.commit(); cur.close()
    finally: pool().putconn(c)
    return {"ok":True,"msg":f"Generated {count} new slots"}

@app.post("/api/admin/cron/sync-inventory")
async def cron_sync(request:Request):
    # await admin_only(request)
    exe("UPDATE slot_inventory SET booked=0")
    orders=qry("SELECT slots FROM orders WHERE status!='cancelled'")
    for o in orders:
        sm=sj(o[0],{})
        for slot in sm.values():
            if slot.get("_cancelled"): continue
            d,t=slot.get("date"),slot.get("time")
            if d and t: exe("UPDATE slot_inventory SET booked=booked+1 WHERE date=%s AND time=%s",(d,t))
    exe("UPDATE slot_inventory SET available=capacity-booked-locked, is_blocked=CASE WHEN capacity-booked-locked<=0 THEN TRUE ELSE FALSE END")
    return {"ok":True,"msg":"Inventory synced"}

@app.post("/api/admin/cron/block-slots")
async def cron_block(request:Request):
    # await admin_only(request)
    now=datetime.now(timezone.utc)
    rows=qry("SELECT date,time,available,capacity,booked,locked FROM slot_inventory")
    cnt=0
    for r in rows:
        d,t,av,cap,bk,lk=r
        try:
            sdt=datetime.fromisoformat(f"{d}T{t24(t)}").replace(tzinfo=timezone.utc)
        except: continue
        h=(sdt-now).total_seconds()/3600
        reason=None
        if h<=0: reason="past"
        elif h<2: reason="cutoff"
        elif av<=0: reason="full"
        elif h<4 and av<=1: reason="last_seat_hold"
        elif bk>=cap*0.9: reason="high_demand"
        if reason: exe("UPDATE slot_inventory SET is_blocked=TRUE,block_reason=%s WHERE date=%s AND time=%s",(reason,d,t))
        else: exe("UPDATE slot_inventory SET is_blocked=FALSE,block_reason=NULL WHERE date=%s AND time=%s",(d,t))
        cnt+=1
    return {"ok":True,"msg":f"Processed {cnt} slots"}

@app.post("/api/admin/cron/assign-providers")
async def cron_assign(request:Request):
    # await admin_only(request)
    now=datetime.now(timezone.utc)
    orders=qry("SELECT id,slots FROM orders WHERE status!='cancelled'")
    assigned=0
    for order in orders:
        oid,sr=order; sm=sj(sr,{}); upd=False
        for key,slot in sm.items():
            if slot.get("_cancelled") or slot.get("provider",{}).get("phone"): continue
            try: sdt=datetime.fromisoformat(f"{slot['date']}T{t24(slot['time'])}").replace(tzinfo=timezone.utc)
            except: continue
            h=(sdt-now).total_seconds()/3600
            if not(0<h<=24): continue
            pvs=qry("SELECT id,name,phone,rating,total_jobs,last_assigned_at FROM providers WHERE is_active=TRUE AND is_busy=FALSE")
            best,bs=None,-1
            for pv in pvs:
                pid,nm,ph,rat,tj,la=pv
                idle=min(((now-la).total_seconds()/3600) if la else 24,1)
                sc=(rat or 0)*0.6+(1/(tj+1))*0.2+idle*0.2
                if sc>bs: bs=sc; best=pv
            if not best: continue
            pid,nm,ph=best[0],best[1],best[2]
            slot["provider"]={"name":nm,"phone":ph,"assigned_at":now.isoformat()}
            exe("UPDATE providers SET is_busy=TRUE,last_assigned_at=NOW(),total_jobs=total_jobs+1,current_job_id=%s WHERE id=%s",(oid,pid))
            exe("INSERT INTO provider_assignments(provider_id,order_id,slot_key) VALUES(%s,%s,%s)",(pid,oid,key))
            upd=True; assigned+=1
        if upd: exe("UPDATE orders SET slots=%s WHERE id=%s",(json.dumps(sm),oid))
    return {"ok":True,"msg":f"Assigned {assigned} providers"}

@app.post("/api/admin/cron/release-providers")
async def cron_release(request:Request):
    # await admin_only(request)
    now=datetime.now(timezone.utc)
    rows=qry("SELECT pa.id,pa.provider_id,pa.slot_key,o.slots FROM provider_assignments pa JOIN orders o ON pa.order_id=o.id WHERE pa.job_status='assigned'")
    released=0
    for r in rows:
        aid,pvid,sk,sr=r; sm=sj(sr,{}); slot=sm.get(sk)
        if not slot: continue
        try: sdt=datetime.fromisoformat(f"{slot['date']}T{t24(slot['time'])}").replace(tzinfo=timezone.utc)
        except: continue
        if now>sdt:
            exe("UPDATE provider_assignments SET job_status='completed' WHERE id=%s",(aid,))
            exe("UPDATE providers SET is_busy=FALSE,current_job_id=NULL WHERE id=%s",(pvid,))
            released+=1
    return {"ok":True,"msg":f"Released {released} providers"}

@app.post("/api/admin/cron/dynamic-capacity")
async def cron_dyncap(request:Request):
    # await admin_only(request)
    exe("UPDATE slot_inventory SET capacity=CASE WHEN booked>=base_capacity*0.8 THEN base_capacity+2 ELSE base_capacity END")
    return {"ok":True,"msg":"Dynamic capacity applied"}

# ══════════════════════════════════════════════════════════════════════════════
#  BROADCAST EMAIL
# ══════════════════════════════════════════════════════════════════════════════
def send_email(to_email,to_name,subject,html):
    r=requests.post("https://api.brevo.com/v3/smtp/email",
        headers={"api-key":BREVO_API_KEY,"Content-Type":"application/json"},
        json={"sender":{"name":SENDER_NAME,"email":SENDER_EMAIL},
              "to":[{"email":to_email,"name":to_name}],
              "subject":subject,"htmlContent":html})
    return r.status_code in(200,201)

@app.post("/api/admin/broadcast")
async def broadcast(request:Request):
    # await admin_only(request)
    b=await request.json()
    subject=b.get("subject",""); html=b.get("html",""); segment=b.get("segment","all")
    if segment=="active":
        rows=qry("SELECT DISTINCT u.email,u.name FROM users u JOIN orders o ON u.firebase_uid=o.firebase_uid WHERE o.status='paid'")
    else:
        rows=qry("SELECT email,COALESCE(name,'Customer') FROM users WHERE email IS NOT NULL")
    sent=0
    for r in rows:
        if send_email(r[0],r[1] or "Customer",subject,html): sent+=1
    return {"ok":True,"sent":sent}
@app.get("/")
def serve_index():
    return FileResponse(os.path.join(FRONTEND, "index.html"))

@app.get("/{path:path}")
def serve_static(path: str):
    fp = os.path.join(FRONTEND, path)
    if os.path.isfile(fp): return FileResponse(fp)
    return FileResponse(os.path.join(FRONTEND, "index.html"))

if __name__=="__main__":
    import uvicorn
    uvicorn.run("main:app",host="0.0.0.0",port=8001,reload=True)