import os
import secrets
import shutil
import smtplib
import uuid
from datetime import datetime, date
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, status, Form, File, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Date, text
from sqlalchemy.orm import declarative_base, sessionmaker

CLINIC_TZ = ZoneInfo("Asia/Karachi")

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///clinic.db")
engine = create_engine(DATABASE_URL)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)

# Clinic booking rules
SLOT_TIMES = ["19:30", "20:00", "20:30", "21:00", "21:30"]  # shared pool for physical AND online
SLOT_CAPACITY = 3  # combined patients (physical + online) allowed per slot
OPEN_WEEKDAYS = {0, 1, 2, 3, 4}  # Monday=0 ... Friday=4

# Payment screenshots saved to persistent App Service disk (no paid storage needed)
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "payment_screenshots")
Path(SCREENSHOT_DIR).mkdir(parents=True, exist_ok=True)

# Email notification settings
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFY_EMAILS = [e.strip() for e in os.environ.get("NOTIFY_EMAILS", "").split(",") if e.strip()]


class Inquiry(Base):
    __tablename__ = "inquiries"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    phone = Column(String)
    preferred_time = Column(String)  # legacy, unused
    appointment_date = Column(Date)
    appointment_time = Column(String)
    appointment_type = Column(String, default="physical")  # "physical" or "online"
    payment_screenshot_path = Column(String)  # filename only, nullable
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="New")


Base.metadata.create_all(engine)

with engine.connect() as conn:
    conn.execute(text("ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS appointment_date DATE"))
    conn.execute(text("ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS appointment_time VARCHAR"))
    conn.execute(text("ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS appointment_type VARCHAR DEFAULT 'physical'"))
    conn.execute(text("ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS payment_screenshot_path VARCHAR"))
    conn.commit()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBasic()


def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(credentials.username, os.environ.get("ADMIN_USER", ""))
    correct_pass = secrets.compare_digest(credentials.password, os.environ.get("ADMIN_PASS", ""))
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


def is_slot_in_past(target_date: date, slot_time: str) -> bool:
    hour, minute = map(int, slot_time.split(":"))
    slot_dt = datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=CLINIC_TZ)
    return slot_dt <= datetime.now(CLINIC_TZ)


def slot_counts_for_date(db, target_date: date):
    """Combined count across BOTH physical and online bookings for a date — shared capacity pool."""
    rows = db.query(Inquiry).filter(Inquiry.appointment_date == target_date).all()
    counts = {t: 0 for t in SLOT_TIMES}
    for row in rows:
        if row.appointment_time in counts:
            counts[row.appointment_time] += 1
    return counts


def send_payment_notification(name, phone, appt_date, appt_time):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and NOTIFY_EMAILS):
        return  # not configured yet, skip silently rather than crash the booking
    body = (
        f"New online appointment awaiting payment verification.\n\n"
        f"Name: {name}\nPhone: {phone}\nDate: {appt_date}\nTime: {appt_time}\n\n"
        f"Check admin.html to review the payment screenshot and confirm."
    )
    msg = MIMEText(body)
    msg["Subject"] = f"Payment verification needed — {name} ({appt_date} {appt_time})"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(NOTIFY_EMAILS)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, NOTIFY_EMAILS, msg.as_string())
    except Exception:
        pass  # never let an email failure break the booking itself


@app.get("/available-slots")
def available_slots(for_date: date):
    if for_date.weekday() not in OPEN_WEEKDAYS:
        return {"date": str(for_date), "open": False, "slots": []}

    db = SessionLocal()
    counts = slot_counts_for_date(db, for_date)
    db.close()

    slots = [
        {
            "time": t,
            "remaining": max(0, SLOT_CAPACITY - counts[t]),
            "full": counts[t] >= SLOT_CAPACITY,
            "past": is_slot_in_past(for_date, t),
        }
        for t in SLOT_TIMES
    ]
    return {"date": str(for_date), "open": True, "slots": slots}


@app.post("/inquiries")
async def create_inquiry(
    name: str = Form(...),
    phone: str = Form(...),
    appointment_date: date = Form(...),
    appointment_time: str = Form(...),
    appointment_type: str = Form("physical"),
    payment_screenshot: UploadFile = File(None),
):
    if appointment_date.weekday() not in OPEN_WEEKDAYS:
        raise HTTPException(status_code=400, detail="Clinic is closed on weekends. Please pick a weekday.")
    if appointment_time not in SLOT_TIMES:
        raise HTTPException(status_code=400, detail="Invalid time slot.")
    if is_slot_in_past(appointment_date, appointment_time):
        raise HTTPException(status_code=400, detail="This date and time has already passed. Please choose a present or future time.")
    if appointment_type not in ("physical", "online"):
        raise HTTPException(status_code=400, detail="Invalid appointment type.")
    if appointment_type == "online" and payment_screenshot is None:
        raise HTTPException(status_code=400, detail="Please upload your payment screenshot for an online appointment.")

    db = SessionLocal()
    existing = db.query(Inquiry).filter(
        Inquiry.phone == phone,
        Inquiry.appointment_date == appointment_date,
    ).first()
    if existing:
        db.close()
        raise HTTPException(
            status_code=409,
            detail="This phone number already has a booking on this date. Please contact us on WhatsApp if you need to change it."
        )

    counts = slot_counts_for_date(db, appointment_date)
    if counts[appointment_time] >= SLOT_CAPACITY:
        db.close()
        raise HTTPException(status_code=409, detail="This date and time slot is already booked. Please choose another time.")

    screenshot_filename = None
    if appointment_type == "online" and payment_screenshot is not None:
        ext = Path(payment_screenshot.filename).suffix or ".jpg"
        screenshot_filename = f"{uuid.uuid4().hex}{ext}"
        dest_path = Path(SCREENSHOT_DIR) / screenshot_filename
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(payment_screenshot.file, f)

    new_inquiry = Inquiry(
        name=name,
        phone=phone,
        appointment_date=appointment_date,
        appointment_time=appointment_time,
        appointment_type=appointment_type,
        payment_screenshot_path=screenshot_filename,
        status="Pending Payment Verification" if appointment_type == "online" else "New",
    )
    db.add(new_inquiry)
    db.commit()
    db.refresh(new_inquiry)
    db.close()

    if appointment_type == "online":
        send_payment_notification(name, phone, appointment_date, appointment_time)

    return {"status": "saved", "id": new_inquiry.id}


@app.get("/inquiries", dependencies=[Depends(verify_admin)])
def list_inquiries():
    db = SessionLocal()
    results = db.query(Inquiry).all()
    db.close()
    return results


@app.get("/payment-screenshot/{inquiry_id}", dependencies=[Depends(verify_admin)])
def get_payment_screenshot(inquiry_id: int):
    db = SessionLocal()
    inquiry = db.query(Inquiry).filter(Inquiry.id == inquiry_id).first()
    db.close()
    if not inquiry or not inquiry.payment_screenshot_path:
        raise HTTPException(status_code=404, detail="No screenshot found.")
    file_path = Path(SCREENSHOT_DIR) / inquiry.payment_screenshot_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Screenshot file missing.")
    return FileResponse(file_path)


class StatusUpdateIn:
    pass


from pydantic import BaseModel


class StatusUpdate(BaseModel):
    status: str = None
    appointment_date: date = None
    appointment_time: str = None


@app.patch("/inquiries/{inquiry_id}", dependencies=[Depends(verify_admin)])
def update_inquiry(inquiry_id: int, update: StatusUpdate):
    db = SessionLocal()
    inquiry = db.query(Inquiry).filter(Inquiry.id == inquiry_id).first()
    if not inquiry:
        db.close()
        raise HTTPException(status_code=404, detail="Not found")

    if update.status is not None:
        inquiry.status = update.status

    if update.appointment_date is not None and update.appointment_time is not None:
        counts = slot_counts_for_date(db, update.appointment_date)
        already_in_target = (inquiry.appointment_date == update.appointment_date
                              and inquiry.appointment_time == update.appointment_time)
        if not already_in_target and counts[update.appointment_time] >= SLOT_CAPACITY:
            db.close()
            raise HTTPException(status_code=409, detail="Target slot is full.")
        inquiry.appointment_date = update.appointment_date
        inquiry.appointment_time = update.appointment_time

    db.commit()
    db.close()
    return {"status": "updated"}