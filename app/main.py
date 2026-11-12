import hashlib, hmac, os, secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone

import jwt
import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from pydantic import BaseModel, EmailStr

DB_URL = os.getenv("DATABASE_URL", "postgresql://clinic:clinic@localhost:5433/clinicflow")
SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
TOKEN_MINUTES = 60
STATIC = os.path.join(os.path.dirname(__file__), "..", "static")


def db():
    return psycopg.connect(DB_URL, row_factory=dict_row)


def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"


def check_pw(pw: str, stored: str) -> bool:
    salt, h = stored.split("$")
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex(), h)


def seed():
    """Demo data: one admin and two doctors with Mon-Fri working hours."""
    with db() as c:
        if c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return
        def user(email, role):
            return c.execute("INSERT INTO users(email,password_hash,role) VALUES (%s,%s,%s) RETURNING id",
                             (email, hash_pw("password"), role)).fetchone()["id"]
        user("admin@clinic.test", "admin")
        user("reception@clinic.test", "receptionist")
        for email, name, spec, mins in [("dr.smith@clinic.test", "Dr. Anna Smith", "General practice", 30),
                                        ("dr.lee@clinic.test", "Dr. Paul Lee", "Dentist", 20)]:
            did = c.execute("INSERT INTO doctors(user_id,full_name,specialty,slot_minutes) VALUES (%s,%s,%s,%s) RETURNING id",
                            (user(email, "doctor"), name, spec, mins)).fetchone()["id"]
            for wd in range(5):
                c.execute("INSERT INTO working_hours(doctor_id,weekday,start_time,end_time) VALUES (%s,%s,'09:00','13:00'),(%s,%s,'14:00','17:00')",
                          (did, wd, did, wd))


@asynccontextmanager
async def lifespan(_):
    seed()
    yield


app = FastAPI(title="ClinicFlow", lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


def current_user(cred: HTTPAuthorizationCredentials = Depends(bearer)):
    if not cred:
        raise HTTPException(401, "Not authenticated")
    try:
        data = jwt.decode(cred.credentials, SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    with db() as c:
        u = c.execute("SELECT id,email,role,is_active FROM users WHERE id=%s", (int(data["sub"]),)).fetchone()
    if not u or not u["is_active"]:
        raise HTTPException(401, "Account inactive")
    return u


def require(*roles):
    def dep(u=Depends(current_user)):
        if u["role"] not in roles:
            raise HTTPException(403, "Forbidden for role " + u["role"])
        return u
    return dep


class Register(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    date_of_birth: date


class Login(BaseModel):
    email: EmailStr
    password: str


class Book(BaseModel):
    doctor_id: int
    start_at: datetime


@app.post("/auth/register", status_code=201)
def register(r: Register):
    with db() as c:
        try:
            uid = c.execute("INSERT INTO users(email,password_hash,role) VALUES (%s,%s,'patient') RETURNING id",
                            (r.email.lower(), hash_pw(r.password))).fetchone()["id"]
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "Email already registered")
        c.execute("INSERT INTO patients(user_id,full_name,date_of_birth) VALUES (%s,%s,%s)",
                  (uid, r.full_name, r.date_of_birth))
    return {"id": uid}


@app.post("/auth/login")
def login(l: Login):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE email=%s", (l.email.lower(),)).fetchone()
    if not u or not u["is_active"] or not check_pw(l.password, u["password_hash"]):
        raise HTTPException(401, "Wrong credentials")
    exp = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_MINUTES)
    return {"access_token": jwt.encode({"sub": str(u["id"]), "exp": exp}, SECRET, algorithm="HS256"),
            "role": u["role"]}


@app.get("/doctors")
def doctors(specialty: str | None = None, _=Depends(current_user)):
    with db() as c:
        return c.execute("SELECT id,full_name,specialty,slot_minutes FROM doctors "
                         "WHERE (%s::text IS NULL OR specialty ILIKE '%%'||%s||'%%') ORDER BY full_name",
                         (specialty, specialty)).fetchall()


@app.get("/doctors/{doctor_id}/slots")
def slots(doctor_id: int, day: date, _=Depends(current_user)):
    """Free slots = working hours - time off - active appointments (FR8, FR9)."""
    today = date.today()
    if day < today or day > today + timedelta(days=30):
        raise HTTPException(400, "Date must be within the next 30 days")
    with db() as c:
        d = c.execute("SELECT slot_minutes FROM doctors WHERE id=%s", (doctor_id,)).fetchone()
        if not d:
            raise HTTPException(404, "Doctor not found")
        hours = c.execute("SELECT start_time,end_time FROM working_hours WHERE doctor_id=%s AND weekday=%s ORDER BY start_time",
                          (doctor_id, day.weekday())).fetchall()
        lo, hi = datetime.combine(day, time.min).astimezone(), datetime.combine(day + timedelta(days=1), time.min).astimezone()
        busy = [(r["start_at"], r["end_at"]) for r in c.execute(
            "SELECT start_at,end_at FROM appointments WHERE doctor_id=%s AND status IN ('booked','checked_in','completed') "
            "AND start_at<%s AND end_at>%s UNION ALL SELECT start_at,end_at FROM time_off WHERE doctor_id=%s AND start_at<%s AND end_at>%s",
            (doctor_id, hi, lo, doctor_id, hi, lo)).fetchall()]
    step, now, out = timedelta(minutes=d["slot_minutes"]), datetime.now().astimezone(), []
    for h in hours:
        s = datetime.combine(day, h["start_time"]).astimezone()
        end = datetime.combine(day, h["end_time"]).astimezone()
        while s + step <= end:
            if s > now and not any(b0 < s + step and b1 > s for b0, b1 in busy):
                out.append(s.isoformat())
            s += step
    return out


@app.post("/appointments", status_code=201)
def book(b: Book, u=Depends(require("patient"))):
    if b.start_at.tzinfo is None:
        b.start_at = b.start_at.astimezone()
    if b.start_at <= datetime.now(timezone.utc) or b.start_at > datetime.now(timezone.utc) + timedelta(days=31):
        raise HTTPException(400, "Start time out of the bookable range")
    if b.start_at.isoformat() not in slots(b.doctor_id, b.start_at.astimezone().date(), u):
        raise HTTPException(409, "Slot is not available")
    with db() as c:
        p = c.execute("SELECT id FROM patients WHERE user_id=%s", (u["id"],)).fetchone()
        d = c.execute("SELECT slot_minutes FROM doctors WHERE id=%s", (b.doctor_id,)).fetchone()
        try:
            a = c.execute("INSERT INTO appointments(doctor_id,patient_id,start_at,end_at,created_by) "
                          "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                          (b.doctor_id, p["id"], b.start_at, b.start_at + timedelta(minutes=d["slot_minutes"]), u["id"])).fetchone()
        except psycopg.errors.ExclusionViolation as e:
            raise HTTPException(409, "Slot already taken" if "doctor" in str(e) else "You already have an overlapping appointment")
        c.execute("INSERT INTO audit_log(actor_id,action,entity,entity_id,details) VALUES (%s,'book','appointment',%s,%s)",
                  (u["id"], a["id"], psycopg.types.json.Jsonb({"doctor_id": b.doctor_id, "start_at": b.start_at.isoformat()})))
    return {"id": a["id"], "status": "booked"}


@app.get("/appointments/me")
def my_appointments(u=Depends(require("patient"))):
    with db() as c:
        return c.execute("SELECT a.id,a.start_at,a.end_at,a.status,d.full_name AS doctor,d.specialty "
                         "FROM appointments a JOIN doctors d ON d.id=a.doctor_id JOIN patients p ON p.id=a.patient_id "
                         "WHERE p.user_id=%s ORDER BY a.start_at DESC", (u["id"],)).fetchall()


@app.post("/appointments/{appt_id}/cancel")
def cancel(appt_id: int, u=Depends(require("patient"))):
    with db() as c:
        a = c.execute("SELECT a.* FROM appointments a JOIN patients p ON p.id=a.patient_id "
                      "WHERE a.id=%s AND p.user_id=%s", (appt_id, u["id"])).fetchone()
        if not a:
            raise HTTPException(404, "Appointment not found")
        if a["status"] != "booked":
            raise HTTPException(409, f"Cannot cancel an appointment in status {a['status']}")
        if a["start_at"] - datetime.now(timezone.utc) < timedelta(hours=24):
            raise HTTPException(409, "Cancellation is only allowed 24 hours before start")
        c.execute("UPDATE appointments SET status='cancelled' WHERE id=%s", (appt_id,))
        c.execute("INSERT INTO audit_log(actor_id,action,entity,entity_id,details) VALUES (%s,'cancel','appointment',%s,%s)",
                  (u["id"], appt_id, psycopg.types.json.Jsonb({"from": "booked", "to": "cancelled"})))
    return {"id": appt_id, "status": "cancelled"}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
