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
import random
import string
from google import genai
import os
import requests
from typing import List, Optional

gemini = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


# ── ENV ───────────────────────────────────────────────────────────────────────
DATABASE_URL        = os.getenv("DATABASE_URL")
FIREBASE_API_KEY    = os.getenv("FIREBASE_API_KEY")
RAZORPAY_KEY_ID     = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
BREVO_API_KEY       = os.getenv("BREVO_API_KEY")
SENDER_EMAIL        = os.getenv("SENDER_EMAIL", "noreply@urbanease.in")
SENDER_NAME         = os.getenv("SENDER_NAME", "Urban Ease")
IST                 = ZoneInfo("Asia/Kolkata")
_rzp = None

def get_rzp():
    global _rzp
    if _rzp is None:
        _rzp = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    return _rzp

rzp = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

app = FastAPI(title="UrbanEase Admin")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

async def gemini_triage(subject: str, description: str, category: str) -> dict:
    try:
        response = gemini.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"""
You are a support agent for UrbanEase, a home beauty & wellness services app in India.
Analyze this support ticket and respond ONLY with valid JSON, no markdown, no backticks.

Ticket Subject: {subject}
Category: {category}
Description: {description}

Return exactly this JSON shape:
{{
  "priority": "low" | "medium" | "high",
  "suggested_reply": "<friendly 2-3 sentence reply>",
  "resolution_type": "refund" | "reschedule" | "provider_issue" | "technical" | "general",
  "auto_resolvable": true | false,
  "confidence": 0.0 to 1.0
}}
"""
        )
        text = response.text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print("Gemini triage error:", e)
        return None

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
@app.get("/api/admin/dashboard/insights")
async def dashboard_insights(request: Request):
    today = date.today()

    chart = qry("""
        SELECT DATE(created_at), COUNT(*), COALESCE(SUM(amount), 0)
        FROM orders
        WHERE created_at >= NOW() - INTERVAL '7 days'
        GROUP BY DATE(created_at) ORDER BY 1
    """)
    cancels   = qry("SELECT COUNT(*) FROM orders WHERE status='cancelled' AND DATE(created_at)=%s", (today,))
    top_slot  = qry("""
        SELECT time, SUM(booked) AS total
        FROM slot_inventory WHERE date >= %s
        GROUP BY time ORDER BY total DESC LIMIT 1
    """, (today,))
    dead_slots = qry("SELECT COUNT(*) FROM slot_inventory WHERE available=0 AND date >= %s AND is_blocked=FALSE", (today,))
    busy_provs = qry("SELECT COUNT(*) FROM providers WHERE is_busy=TRUE")
    free_provs = qry("SELECT COUNT(*) FROM providers WHERE is_busy=FALSE AND is_active=TRUE")

    context = {
        "today": str(today),
        "last_7_days": [
            {"date": str(r[0]), "orders": int(r[1]), "revenue_inr": round(float(r[2]) / 100, 2)}
            for r in chart
        ],
        "cancellations_today":       int(cancels[0][0])   if cancels   else 0,
        "busiest_slot_time":         top_slot[0][0]        if top_slot  else "N/A",
        "fully_booked_slots_today":  int(dead_slots[0][0]) if dead_slots else 0,
        "providers_busy":            int(busy_provs[0][0]) if busy_provs else 0,
        "providers_free":            int(free_provs[0][0]) if free_provs else 0,
    }

    try:
        response = gemini.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"""
You are a business analyst for UrbanEase, a home beauty & wellness app in India.
Based on this live dashboard data, write exactly 3 short actionable insights for the admin.

Data:
{json.dumps(context, indent=2)}

Rules:
- Each insight must be 1-2 sentences max
- Be specific — use numbers from the data
- Tell the admin what to DO, not just what happened
- Return ONLY a JSON array of 3 strings, no markdown, no backticks

Example: ["insight 1", "insight 2", "insight 3"]
"""
        )
        text = response.text.strip().replace("```json", "").replace("```", "").strip()
        insights = json.loads(text)
        return {"ok": True, "insights": insights, "context": context}

    except Exception as e:
        print("Gemini insights error:", e)
        return {
            "ok": False,
            "insights": [
                f"Today's cancellations: {context['cancellations_today']} — review if slot blocking is needed.",
                f"{context['providers_free']} providers are currently free — consider manual assignment.",
                f"Busiest time slot this week: {context['busiest_slot_time']} — ensure capacity is sufficient."
            ],
            "context": context
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
@app.get("/api/admin/orders/{oid}/refunds")
async def get_refunds(oid: int, request: Request):
    rows = qry("SELECT refund_id FROM orders WHERE id=%s", (oid,))

    if not rows:
        raise HTTPException(404, "Order not found")

    refund_id = rows[0][0]
    if not refund_id:
        return {"refund": None}

    try:
        client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
        refund = client.refund.fetch(refund_id)
        return {"refund": refund}
    except Exception as e:
        raise HTTPException(500, str(e))
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
    rows = qry(f"""
        SELECT
            p.id,
            p.name,
            p.phone,
            p.rating,
            p.total_jobs,
            p.is_active,
            p.is_busy,
            p.last_assigned_at,
            p.created_at,

            p.current_job_id,

            pa.slot_key,
            pa.job_status,

            o.address,
            o.slots,
            o.cart,
            o.status

        FROM providers p

        LEFT JOIN provider_assignments pa
            ON pa.order_id = p.current_job_id
            AND pa.provider_id = p.id

        LEFT JOIN orders o
            ON o.id = p.current_job_id

        {w}

        ORDER BY p.rating DESC NULLS LAST
        LIMIT %s OFFSET %s
    """, p + [limit, off])
    return {
    "total": int(total[0][0]),
    "page": page,
    "data": [
        {
            "id": r[0],
            "name": r[1],
            "phone": r[2],
            "rating": float(r[3]) if r[3] else 0,
            "total_jobs": int(r[4]) if r[4] else 0,

            "is_active": r[5],
            "is_busy": r[6],

            "last_assigned_at": r[7].isoformat() if r[7] else None,
            "created_at": r[8].isoformat() if r[8] else None,

            "current_job_id": r[9],
            "slot_key": r[10],
            "job_status": r[11],

            "address": sj(r[12], {}),
            "slots": sj(r[13], {}),
            "cart": sj(r[14], []),

            "order_status": r[15]
        }
        for r in rows
    ]
}

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
@app.get("/api/admin/slots/forecast")
async def slot_forecast(request: Request):
    today = date.today()

    history = qry("""
        SELECT
            EXTRACT(DOW FROM date) AS day_of_week,
            time,
            ROUND(AVG(booked)::numeric, 2)    AS avg_booked,
            ROUND(AVG(capacity)::numeric, 2)  AS avg_capacity
        FROM slot_inventory
        WHERE date >= %s AND date < %s
        GROUP BY day_of_week, time
        ORDER BY day_of_week, time
    """, (today - timedelta(days=30), today))

    upcoming = qry("""
        SELECT date, time, capacity, booked, available, is_blocked
        FROM slot_inventory
        WHERE date >= %s AND date <= %s
        ORDER BY date, time
    """, (today, today + timedelta(days=7)))

    history_data = [
        {
            "day_of_week": int(r[0]),
            "time": r[1],
            "avg_booked": float(r[2]),
            "avg_capacity": float(r[3]),
            "avg_fill_pct": round(float(r[2]) / float(r[3]) * 100, 1) if r[3] else 0
        }
        for r in history
    ]
    upcoming_data = [
        {
            "date": str(r[0]),
            "day_name": r[0].strftime("%A"),
            "time": r[1],
            "capacity": r[2],
            "booked": r[3],
            "available": r[4],
            "is_blocked": r[5],
            "fill_pct": round(r[3] / r[2] * 100, 1) if r[2] else 0
        }
        for r in upcoming
    ]

    try:
        response = gemini.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"""
You are a scheduling analyst for UrbanEase, a home beauty & wellness app in India.
Analyze booking patterns and forecast demand for the next 7 days.

Historical 30-day averages by day and time:
{json.dumps(history_data, indent=2)}

Next 7 days current state:
{json.dumps(upcoming_data, indent=2)}

Today is {today.strftime("%A, %d %B %Y")}.

Return ONLY valid JSON, no markdown, no backticks, in this exact shape:
{{
  "summary": "<2 sentence overall forecast>",
  "high_demand_slots": [
    {{"date": "YYYY-MM-DD", "time": "HH:MM AM/PM", "reason": "<why>", "action": "<what admin should do>"}}
  ],
  "low_demand_slots": [
    {{"date": "YYYY-MM-DD", "time": "HH:MM AM/PM", "reason": "<why>", "action": "<what admin should do>"}}
  ],
  "weekly_tip": "<1 strategic tip for this week>"
}}

Include up to 4 high_demand and 4 low_demand slots.
"""
        )
        text = response.text.strip().replace("```json", "").replace("```", "").strip()
        forecast = json.loads(text)
        return {"ok": True, "forecast": forecast}

    except Exception as e:
        print("Gemini forecast error:", e)
        raise HTTPException(500, "Forecast generation failed: " + str(e))
# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN COPILOT
# ══════════════════════════════════════════════════════════════════════════════
@app.post("/api/admin/copilot")
async def admin_copilot(request: Request):
    body  = await request.json()
    query = body.get("query", "").strip()
    if not query:
        raise HTTPException(400, "Query required")

    # ── gather live context ──
    today = date.today()

    rev_today  = qry("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid' AND DATE(created_at)=%s", (today,))
    rev_total  = qry("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid'")
    ord_total  = qry("SELECT COUNT(*) FROM orders")
    ord_today  = qry("SELECT COUNT(*) FROM orders WHERE DATE(created_at)=%s", (today,))
    ord_paid   = qry("SELECT COUNT(*) FROM orders WHERE status='paid'")
    ord_cancel = qry("SELECT COUNT(*) FROM orders WHERE status='cancelled'")
    usr_total  = qry("SELECT COUNT(*) FROM users")
    prov_total = qry("SELECT COUNT(*) FROM providers WHERE is_active=TRUE")
    prov_busy  = qry("SELECT COUNT(*) FROM providers WHERE is_busy=TRUE")
    prov_free  = qry("SELECT COUNT(*) FROM providers WHERE is_busy=FALSE AND is_active=TRUE")
    slots_av   = qry("SELECT COUNT(*) FROM slot_inventory WHERE available>0 AND is_blocked=FALSE AND date>=%s", (today,))
    slots_bl   = qry("SELECT COUNT(*) FROM slot_inventory WHERE is_blocked=TRUE AND date>=%s", (today,))

    top_provs  = qry("SELECT name, rating, total_jobs, is_busy FROM providers WHERE is_active=TRUE ORDER BY total_jobs DESC LIMIT 5")
    rec_orders = qry("""
        SELECT o.id, u.name, u.email, o.amount, o.status, o.created_at
        FROM orders o JOIN users u ON o.firebase_uid=u.firebase_uid
        ORDER BY o.created_at DESC LIMIT 10
    """)
    rec_tickets = qry("""
        SELECT t.ticket_id, t.subject, t.status, t.priority, u.name
        FROM support_tickets t JOIN users u ON t.firebase_uid=u.firebase_uid
        ORDER BY t.created_at DESC LIMIT 5
    """)
    week_rev = qry("""
        SELECT DATE(created_at), COALESCE(SUM(amount),0)
        FROM orders WHERE status='paid' AND created_at>=NOW()-INTERVAL '7 days'
        GROUP BY DATE(created_at) ORDER BY 1
    """)

    context = {
        "today": str(today),
        "revenue": {
            "today_inr": round(float(rev_today[0][0]) / 100, 2) if rev_today else 0,
            "total_inr": round(float(rev_total[0][0]) / 100, 2) if rev_total else 0,
        },
        "orders": {
            "total": int(ord_total[0][0]) if ord_total else 0,
            "today": int(ord_today[0][0]) if ord_today else 0,
            "paid":  int(ord_paid[0][0])  if ord_paid  else 0,
            "cancelled": int(ord_cancel[0][0]) if ord_cancel else 0,
        },
        "users": {"total": int(usr_total[0][0]) if usr_total else 0},
        "providers": {
            "total": int(prov_total[0][0]) if prov_total else 0,
            "busy":  int(prov_busy[0][0])  if prov_busy  else 0,
            "free":  int(prov_free[0][0])  if prov_free  else 0,
        },
        "slots": {
            "available": int(slots_av[0][0]) if slots_av else 0,
            "blocked":   int(slots_bl[0][0]) if slots_bl else 0,
        },
        "top_providers": [
            {"name": r[0], "rating": float(r[1]) if r[1] else 0,
             "jobs": int(r[2]) if r[2] else 0, "busy": r[3]}
            for r in top_provs
        ],
        "recent_orders": [
            {"id": r[0], "customer": r[1], "email": r[2],
             "amount_inr": round(float(r[3]) / 100, 2) if r[3] else 0,
             "status": r[4], "date": str(r[5])[:10]}
            for r in rec_orders
        ],
        "recent_tickets": [
            {"ticket_id": r[0], "subject": r[1], "status": r[2],
             "priority": r[3], "user": r[4]}
            for r in rec_tickets
        ],
        "weekly_revenue": [
            {"date": str(r[0]), "amount_inr": round(float(r[1]) / 100, 2)}
            for r in week_rev
        ],
    }

    prompt = f"""
You are an intelligent admin copilot for UrbanEase, a home beauty & wellness services app in India.
You have access to live business data. Answer the admin's question accurately and helpfully.

LIVE BUSINESS DATA:
{json.dumps(context, indent=2)}

ADMIN QUESTION: {query}

RULES:
- Answer in 1-4 sentences max unless a list or table is genuinely needed
- Use specific numbers from the data
- Be direct and actionable — the admin is busy
- If the question is about something not in your data, say so honestly
- Format numbers in Indian style (₹1,234.56)
- If asked to do an action (like "block slots" or "send email"), explain you can show data but actions must be done via the dashboard buttons
- Never make up data that isn't in the context above

Respond in plain text only, no markdown, no asterisks, no bullet symbols.
"""

    try:
        response = gemini.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )

        text = response.text

        return {
            "ok": True,
            "answer": text.strip()
        }
    except Exception as e:
        print("Copilot error:", e)
        raise HTTPException(500, "Copilot failed: " + str(e))
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
# ══════════════════════════════════════════════════════════════════════════════
#  PROMOTIONAL EMAIL CAMPAIGNS
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/admin/promotional-email")
async def promotional_email(request: Request):

    # await admin_only(request)

    body = await request.json()

    subject = body.get("subject", "").strip()
    html = body.get("html", "").strip()

    segment = body.get("segment", "all")
    limit = int(body.get("limit", 500))

    if not subject:
        raise HTTPException(400, "Subject required")

    if not html:
        raise HTTPException(400, "HTML content required")

    # ── USER SEGMENTS ─────────────────────

    if segment == "all":

        rows = qry("""
            SELECT
                email,
                COALESCE(name, 'Customer')
            FROM users
            WHERE email IS NOT NULL
        """)

    elif segment == "active":

        rows = qry("""
            SELECT DISTINCT
                u.email,
                COALESCE(u.name, 'Customer')
            FROM users u
            JOIN orders o
              ON o.firebase_uid = u.firebase_uid
            WHERE
                o.status='paid'
                AND u.email IS NOT NULL
        """)

    elif segment == "inactive":

        rows = qry("""
            SELECT
                email,
                COALESCE(name, 'Customer')
            FROM users
            WHERE
                email IS NOT NULL
                AND firebase_uid NOT IN (
                    SELECT DISTINCT firebase_uid
                    FROM orders
                    WHERE created_at >= NOW() - INTERVAL '60 days'
                )
        """)

    else:
        raise HTTPException(400, "Invalid segment")

    if not rows:
        return {
            "ok": False,
            "message": "No users found"
        }

    rows = rows[:limit]

    sent = 0
    failed = 0
    failures = []

    # ── SEND EMAILS ───────────────────────

    for row in rows:

        email = row[0]
        name = row[1] or "Customer"

        try:

            r = requests.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={
                    "api-key": BREVO_API_KEY,
                    "Content-Type": "application/json"
                },
                json={
                    "sender": {
                        "name": SENDER_NAME,
                        "email": SENDER_EMAIL
                    },

                    "to": [{
                        "email": email,
                        "name": name
                    }],

                    "subject": subject,
                    "htmlContent": html
                },
                timeout=20
            )

            if r.status_code in (200, 201):
                sent += 1
            else:
                failed += 1
                failures.append({
                    "email": email,
                    "error": r.text
                })

        except Exception as e:

            failed += 1

            failures.append({
                "email": email,
                "error": str(e)
            })

        # prevent burst spam
        time.sleep(0.08)

    return {
        "ok": True,
        "segment": segment,
        "total_users": len(rows),
        "sent": sent,
        "failed": failed,
        "failures": failures[:10]
    }

# ══════════════════════════════════════════════════════════════════════════════
#  AI PROMOTIONAL EMAIL GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/admin/ai/generate-email")
async def ai_generate_email(request: Request):

    # await admin_only(request)

    body = await request.json()

    campaign_type = body.get("campaign_type", "offer")
    audience = body.get("audience", "beauty users")
    offer = body.get("offer", "25% OFF")
    objective = body.get("objective", "increase bookings")
    tone = body.get("tone", "luxury futuristic")
    festival = body.get("festival", "")

    prompt = f"""
You are an elite email marketing strategist for UrbanEase,
a premium home beauty & wellness platform in India.

Generate a HIGH CONVERTING futuristic promotional email.

Campaign Details:
- Campaign Type: {campaign_type}
- Audience: {audience}
- Offer: {offer}
- Objective: {objective}
- Tone: {tone}
- Festival/Event: {festival}

Requirements:
- Modern luxury futuristic style
- Premium beauty brand feel
- Strong CTA
- Mobile friendly
- Beautiful HTML email
- Dark neon aesthetic
- Include offer section
- Include CTA button
- Include footer
- Include premium marketing copy
- Make it visually stunning

Return ONLY valid JSON:

{{
  "subject": "...",
  "html": "FULL HTML EMAIL"
}}

No markdown.
No backticks.
"""

    try:

        response = gemini.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt
        )

        text = (
            response.text
            .replace("```json", "")
            .replace("```", "")
            .strip()
        )

        data = json.loads(text)

        return {
            "ok": True,
            "subject": data.get("subject", ""),
            "html": data.get("html", "")
        }

    except Exception as e:

        print("AI EMAIL ERROR:", str(e))

        raise HTTPException(
            500,
            f"AI generation failed: {str(e)}"
        )

@app.post("/api/support/create")
async def create_ticket(request: Request):

    user = await verify_token(request)
    body = await request.json()

    subject = body.get("subject")
    description = body.get("description")
    category = body.get("category", "general")
    priority = body.get("priority", "medium")

    if not subject or not description:
        raise HTTPException(status_code=400, detail="Subject and description required")

    ticket_id = "UE-" + ''.join(random.choices(string.digits, k=8))

    c = pool().getconn()
    try:
        cur = c.cursor()
        cur.execute("""
            INSERT INTO support_tickets(ticket_id, firebase_uid, subject, category, priority, description, status)
            VALUES(%s,%s,%s,%s,%s,%s,%s)
            RETURNING id, ticket_id, created_at
        """, (ticket_id, user["uid"], subject, category, priority, description, "open"))
        row = cur.fetchone()
        c.commit()
        
        ticket_db_id   = row[0]
        ticket_id_str  = row[1]
        created_at_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

        priority_colors = {"low": "#10b981", "medium": "#f59e0b", "high": "#ef4444"}
        priority_color  = priority_colors.get(priority, "#6c63ff")

        html_email = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>Support Ticket Raised – UrbanEase</title>
</head>
<body style="margin:0;padding:0;background:#f4f4f8;font-family:'Segoe UI',Arial,sans-serif;">

  <!-- Wrapper -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f8;padding:40px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#6c63ff,#a855f7);border-radius:16px 16px 0 0;padding:36px 40px;text-align:center;">
            <div style="font-size:28px;font-weight:800;color:#fff;letter-spacing:-0.5px;">UrbanEase</div>
            <div style="font-size:13px;color:rgba(255,255,255,0.75);margin-top:4px;letter-spacing:2px;text-transform:uppercase;">Support Centre</div>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="background:#ffffff;padding:40px;">

            <!-- Greeting -->
            <p style="font-size:22px;font-weight:700;color:#1a1a2e;margin:0 0 8px;">
              We've received your request 👋
            </p>
            <p style="font-size:15px;color:#555577;margin:0 0 28px;line-height:1.6;">
              Hi <strong>{user.get('name') or user.get('email','there')}</strong>, your support ticket has been successfully created.
              Our team will review it and get back to you within <strong>24–48 hours</strong>.
            </p>

            <!-- Ticket Card -->
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f7ff;border:1px solid #e0deff;border-radius:12px;margin-bottom:28px;">
              <tr>
                <td style="padding:20px 24px;border-bottom:1px solid #e0deff;">
                  <span style="font-size:11px;color:#8888aa;letter-spacing:1.5px;text-transform:uppercase;">Ticket ID</span>
                  <div style="font-size:20px;font-weight:800;color:#6c63ff;margin-top:4px;font-family:monospace;">{ticket_id_str}</div>
                </td>
              </tr>
              <tr>
                <td style="padding:20px 24px;">
                  <table width="100%" cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="padding-bottom:14px;">
                        <div style="font-size:11px;color:#8888aa;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">Subject</div>
                        <div style="font-size:15px;font-weight:600;color:#1a1a2e;">{subject}</div>
                      </td>
                    </tr>
                    <tr>
                      <td style="padding-bottom:14px;">
                        <div style="font-size:11px;color:#8888aa;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">Description</div>
                        <div style="font-size:14px;color:#444466;line-height:1.6;">{description}</div>
                      </td>
                    </tr>
                    <tr>
                      <td>
                        <table cellpadding="0" cellspacing="0">
                          <tr>
                            <td style="padding-right:12px;">
                              <div style="font-size:11px;color:#8888aa;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;">Priority</div>
                              <span style="background:{priority_color}22;color:{priority_color};border:1px solid {priority_color}55;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600;text-transform:capitalize;">{priority}</span>
                            </td>
                            <td style="padding-right:12px;">
                              <div style="font-size:11px;color:#8888aa;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;">Category</div>
                              <span style="background:#6c63ff22;color:#6c63ff;border:1px solid #6c63ff55;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600;text-transform:capitalize;">{category}</span>
                            </td>
                            <td>
                              <div style="font-size:11px;color:#8888aa;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;">Status</div>
                              <span style="background:#a855f722;color:#a855f7;border:1px solid #a855f755;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600;">Open</span>
                            </td>
                          </tr>
                        </table>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>

            <!-- What Happens Next -->
            <p style="font-size:13px;font-weight:700;color:#1a1a2e;text-transform:uppercase;letter-spacing:1px;margin:0 0 14px;">What happens next?</p>
            <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
              <tr>
                <td style="padding-bottom:12px;">
                  <table cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="width:32px;height:32px;background:#6c63ff;border-radius:50%;text-align:center;vertical-align:middle;color:#fff;font-size:13px;font-weight:700;">1</td>
                      <td style="padding-left:12px;font-size:14px;color:#444466;">Our support team reviews your ticket within <strong>a few hours</strong>.</td>
                    </tr>
                  </table>
                </td>
              </tr>
              <tr>
                <td style="padding-bottom:12px;">
                  <table cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="width:32px;height:32px;background:#a855f7;border-radius:50%;text-align:center;vertical-align:middle;color:#fff;font-size:13px;font-weight:700;">2</td>
                      <td style="padding-left:12px;font-size:14px;color:#444466;">You'll receive an <strong>email reply</strong> once we have an update for you.</td>
                    </tr>
                  </table>
                </td>
              </tr>
              <tr>
                <td>
                  <table cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="width:32px;height:32px;background:#06b6d4;border-radius:50%;text-align:center;vertical-align:middle;color:#fff;font-size:13px;font-weight:700;">3</td>
                      <td style="padding-left:12px;font-size:14px;color:#444466;">Once resolved, the ticket is <strong>closed</strong> and you get a confirmation.</td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>

            <!-- Divider -->
            <hr style="border:none;border-top:1px solid #eeeeee;margin:28px 0;"/>

            <!-- Footer note -->
            <p style="font-size:13px;color:#8888aa;line-height:1.6;margin:0;">
              Please keep your ticket ID <strong style="color:#6c63ff;">{ticket_id_str}</strong> handy for any follow-ups.
              You can reply to this email to add more information to your ticket.
            </p>

          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#f0eeff;border-radius:0 0 16px 16px;padding:24px 40px;text-align:center;">
            <div style="font-size:12px;color:#8888aa;line-height:1.8;">
              This is an automated message from <strong style="color:#6c63ff;">UrbanEase Support</strong>.<br/>
              Raised on {created_at_ist} &nbsp;·&nbsp; Ticket <strong>{ticket_id_str}</strong><br/>
              <span style="font-size:11px;">© 2025 UrbanEase. All rights reserved.</span>
            </div>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>

</body>
</html>
"""

        # Send email via Brevo
        user_email = user.get("email")
        user_name  = user.get("name") or user_email or "Customer"
        if user_email:
            send_email(
                to_email=user_email,
                to_name=user_name,
                subject=f"[{ticket_id_str}] We've received your support request – UrbanEase",
                html=html_email
            )

        triage = await gemini_triage(subject, description, category)
        if triage:
            if triage.get("confidence", 0) >= 0.75:
                cur2 = c.cursor()
                cur2.execute("UPDATE support_tickets SET priority=%s WHERE id=%s",
                    (triage["priority"], ticket_db_id))
                c.commit(); cur2.close()
            if triage.get("auto_resolvable") and triage.get("suggested_reply"):
                exe("""INSERT INTO support_ticket_messages
                    (ticket_id, sender_type, sender_name, message)
                    VALUES (%s, 'admin', 'UrbanEase AI', %s)""",
                    (ticket_db_id, triage["suggested_reply"]))

        return {"ok": True, "id": ticket_db_id, "ticket_id": ticket_id_str, "ai_triage": triage}

    except Exception as e:
        print("SUPPORT CREATE ERROR:", str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        pool().putconn(c)
@app.post("/api/admin/support/{tid}/ai-triage")
async def ai_triage_ticket(tid: int, request: Request):
    rows = qry(
        "SELECT subject, description, category FROM support_tickets WHERE id=%s",
        (tid,)
    )
    if not rows:
        raise HTTPException(404, "Not found")

    subject, description, category = rows[0]
    triage = await gemini_triage(subject, description, category)

    if not triage:
        raise HTTPException(500, "AI triage failed")

    if triage.get("confidence", 0) >= 0.75:
        exe(
            "UPDATE support_tickets SET priority=%s, updated_at=NOW() WHERE id=%s",
            (triage["priority"], tid)
        )

    return {"ok": True, "triage": triage}
@app.get("/api/support/my-tickets")
async def my_tickets(request: Request):

    user = await verify_token(request)

    rows = qry("""
        SELECT
            id,
            ticket_id,
            subject,
            category,
            priority,
            status,
            created_at
        FROM support_tickets
        WHERE firebase_uid=%s
        ORDER BY created_at DESC
    """, (user["uid"],))

    return {
        "data": [
            {
                "id": r[0],
                "ticket_id": r[1],
                "subject": r[2],
                "category": r[3],
                "priority": r[4],
                "status": r[5],
                "created_at": r[6].isoformat()
            }
            for r in rows
        ]
    }
@app.get("/api/admin/support")
async def admin_support(request: Request, status: str = "", search: str = ""):
    w, p = "WHERE 1=1", []
    if status:
        w += " AND t.status=%s"; p.append(status)
    if search:
        w += " AND (t.subject ILIKE %s OR u.email ILIKE %s OR u.name ILIKE %s)"
        p += [f"%{search}%"] * 3
    rows = qry(f"""
        SELECT t.id, t.ticket_id, t.subject, t.status, t.priority,
               t.category, t.created_at, u.name, u.email
        FROM support_tickets t
        JOIN users u ON t.firebase_uid=u.firebase_uid
        {w} ORDER BY t.created_at DESC
    """, p)

    return {
        "data": [
            {
                "id": r[0],
                "ticket_id": r[1],
                "subject": r[2],
                "status": r[3],
                "priority": r[4],
                "category": r[5],
                "created_at": r[6].isoformat(),
                "user_name": r[7],
                "user_email": r[8]
            }
            for r in rows
        ]
    }
@app.get("/api/admin/support/{tid}")
async def get_ticket(tid: int, request: Request):
    rows = qry("""
        SELECT t.id, t.ticket_id, t.subject, t.status, t.priority,
               t.category, t.description, t.created_at, u.name, u.email
        FROM support_tickets t
        JOIN users u ON t.firebase_uid=u.firebase_uid
        WHERE t.id=%s
    """, (tid,))
    if not rows:
        raise HTTPException(404, "Not found")
    r = rows[0]
    messages = qry("""
        SELECT id, sender_type, sender_name, message, created_at
        FROM support_ticket_messages
        WHERE ticket_id=%s ORDER BY created_at ASC
    """, (tid,))
    return {
        "ticket": {
            "id": r[0], "ticket_id": r[1], "subject": r[2],
            "status": r[3], "priority": r[4], "category": r[5],
            "description": r[6],
            "created_at": r[7].isoformat(),
            "user_name": r[8], "user_email": r[9]
        },
        "messages": [
            {"id": m[0], "sender_type": m[1], "sender_name": m[2],
             "message": m[3], "created_at": m[4].isoformat()}
            for m in messages
        ]
    }
@app.post("/api/admin/support/{tid}/reply")
async def admin_reply(tid: int, request: Request):
    body = await request.json()
    message = body.get("message")
    if not message:
        raise HTTPException(400, "Message required")
    exe("""
        INSERT INTO support_ticket_messages(ticket_id, sender_type, sender_name, message)
        VALUES(%s, %s, %s, %s)
    """, (tid, "admin", "Admin", message))
    return {"ok": True}
@app.patch("/api/admin/support/{tid}/status")
async def update_ticket_status(tid: int, request: Request):

    body = await request.json()

    status = body.get("status")

    allowed = [
        "open",
        "in_progress",
        "assigned",
        "resolved",
        "closed",
        "rejected"
    ]

    if status not in allowed:
        raise HTTPException(400, "Invalid status")

    exe("""
        UPDATE support_tickets
        SET
            status=%s,
            updated_at=NOW()
        WHERE id=%s
    """, (status, tid))

    return {"ok": True}
@app.post("/api/support/{tid}/message")
async def add_ticket_message(tid: int, request: Request):

    user = await verify_token(request)

    body = await request.json()

    message = body.get("message")

    if not message:
        raise HTTPException(400, "Message required")

    exe("""
        INSERT INTO support_ticket_messages(
            ticket_id,
            sender_type,
            sender_name,
            message
        )
        VALUES(%s,%s,%s,%s)
    """, (
        tid,
        "user",
        user.get("name", "User"),
        message
    ))

    return {"ok": True}
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