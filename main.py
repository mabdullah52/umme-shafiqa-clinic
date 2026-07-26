import os
import secrets
import shutil
import smtplib
import string
import random
import uuid
import requests
from datetime import datetime, date
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, status, Form, File, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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

# Phase 3 — AI FAQ Assistant (Grok API, xAI)
GROK_API_KEY = os.environ.get("GROK_API_KEY", "")
GROK_API_URL = "https://api.x.ai/v1/chat/completions"
GROK_MODEL = "grok-4.3"

FAQ_SYSTEM_PROMPT = """You are the FAQ assistant for Umme Shafiqa Clinic, a home-based gynaecology clinic in Lahore, Pakistan, run by Dr. Rashida Latif.

CLINIC FACTS (this is the ONLY information you may state as fact):
- Doctor: Dr. Rashida Latif, gynaecologist. She has practiced in Lahore for many years — previously at Lady Wallington Hospital, then Mian Munshi Hospital, and is currently also affiliated with Jinnah Hospital, alongside running this clinic.
- Address: House #113, Neelum Block, Allama Iqbal Town, Lahore.
- Timings: Monday to Friday, 7:30 PM to 10:00 PM. Closed on weekends.
- Services offered: gynaecological consultation, transvaginal ultrasound (TVS), infertility and female health care, and pregnancy care (consultation and ultrasound during pregnancy).
- Gynae operations (e.g. general gynaecological surgery, C-section, and similar procedures) can also be arranged, but these are NOT booked through the website. Fees for operations vary case by case — direct patients to message the clinic on WhatsApp or arrange an appointment with the doctor directly to discuss this.
- Fees: physical visit (consultation + ultrasound) is Rs. 2500–3000. TVS (transvaginal ultrasound) specifically is Rs. 3500. Online consultation is Rs. 1500.
- Online consultations require payment (via JazzCash/EasyPaisa or Meezan Bank) and a payment screenshot upload; the appointment is confirmed only after staff verifies the payment.
- Vaccinations are NOT offered at this clinic.
- Booking is done through the clinic website's appointment form (physical or online consultation), or by contacting the clinic on WhatsApp.

STRICT RULES — follow these without exception:
1. ONLY answer questions about the clinic (hours, services, fees, location, booking process, the doctor's background). Do not answer general knowledge, unrelated, or off-topic questions — politely redirect to clinic topics instead.
2. NEVER diagnose any medical condition, symptom, or complaint a patient describes.
3. NEVER prescribe or recommend any medicine, dosage, or treatment.
4. NEVER interpret lab reports, ultrasound results, or any medical documents or images.
5. If a question falls outside clinic information, or requires medical judgment, or you are not certain the answer is covered by the facts above, respond exactly with: "Please consult the doctor."
6. Keep answers short, warm, and factual. Never invent details not listed above.
"""


def call_grok_faq(user_message: str, conversation_history: list):
    if not GROK_API_KEY:
        return "Sorry, the assistant isn't set up yet. Please contact us on WhatsApp for now."

    messages = [{"role": "system", "content": FAQ_SYSTEM_PROMPT}]
    for turn in conversation_history[-8:]:  # keep a short rolling window, not unlimited history
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": user_message})

    try:
        resp = requests.post(
            GROK_API_URL,
            headers={"Authorization": f"Bearer {GROK_API_KEY}", "Content-Type": "application/json"},
            json={"model": GROK_MODEL, "messages": messages, "temperature": 0.3, "max_tokens": 300},
            timeout=20,
        )
        print(f"GROK API STATUS: {resp.status_code}")
        print(f"GROK API RESPONSE BODY: {resp.text[:2000]}")
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"GROK API ERROR: {repr(e)}")
        return "Sorry, I'm having trouble answering right now. Please contact us on WhatsApp."


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
    confirmation_code = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="New")


class ContactMessage(Base):
    __tablename__ = "contact_messages"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    contact_info = Column(String)  # phone or email, whatever they gave
    message = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    read = Column(String, default="Unread")  # "Unread" or "Read"


Base.metadata.create_all(engine)

with engine.connect() as conn:
    conn.execute(text("ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS appointment_date DATE"))
    conn.execute(text("ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS appointment_time VARCHAR"))
    conn.execute(text("ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS appointment_type VARCHAR DEFAULT 'physical'"))
    conn.execute(text("ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS payment_screenshot_path VARCHAR"))
    conn.execute(text("ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS confirmation_code VARCHAR"))
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
    """Combined count across BOTH physical and online bookings for a date — shared capacity pool.
    Cancelled bookings don't count against capacity, so cancelling frees the slot."""
    rows = db.query(Inquiry).filter(
        Inquiry.appointment_date == target_date,
        Inquiry.status != "Cancelled",
    ).all()
    counts = {t: 0 for t in SLOT_TIMES}
    for row in rows:
        if row.appointment_time in counts:
            counts[row.appointment_time] += 1
    return counts


def generate_confirmation_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


def send_refund_notification(name, phone, appt_date, appt_time):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and NOTIFY_EMAILS):
        return
    body = (
        f"An ONLINE (paid) appointment was just cancelled. If payment was received, a refund may be owed.\n\n"
        f"Name: {name}\nPhone: {phone}\nOriginal date: {appt_date}\nOriginal time: {appt_time}"
    )
    msg = MIMEText(body)
    msg["Subject"] = f"Refund check needed — {name} ({appt_date} {appt_time}) cancelled"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(NOTIFY_EMAILS)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, NOTIFY_EMAILS, msg.as_string())
    except Exception:
        pass


def send_patient_cancel_notification(name, phone, appt_date, appt_time, appointment_type):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and NOTIFY_EMAILS):
        return
    refund_line = "\n\nThis was a PAID online booking — refund may be owed." if appointment_type == "online" else ""
    body = (
        f"A patient cancelled their own appointment.\n\n"
        f"Name: {name}\nPhone: {phone}\nDate: {appt_date}\nTime: {appt_time}\nType: {appointment_type}"
        f"{refund_line}"
    )
    msg = MIMEText(body)
    msg["Subject"] = f"Patient cancelled — {name} ({appt_date} {appt_time})"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(NOTIFY_EMAILS)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, NOTIFY_EMAILS, msg.as_string())
    except Exception:
        pass


def send_reschedule_notification(name, phone, old_date, old_time, new_date, new_time):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and NOTIFY_EMAILS):
        return
    body = (
        f"A patient rescheduled their own appointment.\n\n"
        f"Name: {name}\nPhone: {phone}\n"
        f"Old: {old_date} {old_time}\nNew: {new_date} {new_time}"
    )
    msg = MIMEText(body)
    msg["Subject"] = f"Patient rescheduled — {name} (now {new_date} {new_time})"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(NOTIFY_EMAILS)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, NOTIFY_EMAILS, msg.as_string())
    except Exception:
        pass


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


class ContactMessageIn(BaseModel):
    name: str
    contact_info: str
    message: str


def send_contact_message_notification(name, contact_info, message):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and NOTIFY_EMAILS):
        return
    body = f"New contact message.\n\nName: {name}\nContact: {contact_info}\n\nMessage:\n{message}"
    msg = MIMEText(body)
    msg["Subject"] = f"New contact message from {name}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(NOTIFY_EMAILS)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, NOTIFY_EMAILS, msg.as_string())
    except Exception:
        pass


@app.post("/contact-messages")
def create_contact_message(msg: ContactMessageIn):
    db = SessionLocal()
    new_msg = ContactMessage(name=msg.name, contact_info=msg.contact_info, message=msg.message)
    db.add(new_msg)
    db.commit()
    db.close()
    send_contact_message_notification(msg.name, msg.contact_info, msg.message)
    return {"status": "saved"}


@app.get("/contact-messages", dependencies=[Depends(verify_admin)])
def list_contact_messages():
    db = SessionLocal()
    results = db.query(ContactMessage).order_by(ContactMessage.created_at.desc()).all()
    db.close()
    return results


class ContactMessageStatusUpdate(BaseModel):
    read: str


@app.patch("/contact-messages/{message_id}", dependencies=[Depends(verify_admin)])
def update_contact_message(message_id: int, update: ContactMessageStatusUpdate):
    db = SessionLocal()
    msg = db.query(ContactMessage).filter(ContactMessage.id == message_id).first()
    if not msg:
        db.close()
        raise HTTPException(status_code=404, detail="Not found")
    msg.read = update.read
    db.commit()
    db.close()
    return {"status": "updated"}


class FaqChatIn(BaseModel):
    message: str
    conversation: list = []


@app.post("/faq-chat")
def faq_chat(req: FaqChatIn):
    reply = call_grok_faq(req.message, req.conversation)
    return {"reply": reply}


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
        Inquiry.status != "Cancelled",
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

    code = generate_confirmation_code()
    new_inquiry = Inquiry(
        name=name,
        phone=phone,
        appointment_date=appointment_date,
        appointment_time=appointment_time,
        appointment_type=appointment_type,
        payment_screenshot_path=screenshot_filename,
        confirmation_code=code,
        status="Pending Payment Verification" if appointment_type == "online" else "New",
    )
    db.add(new_inquiry)
    db.commit()
    db.refresh(new_inquiry)
    db.close()

    if appointment_type == "online":
        send_payment_notification(name, phone, appointment_date, appointment_time)

    return {"status": "saved", "id": new_inquiry.id, "confirmation_code": code}


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


class StatusUpdate(BaseModel):
    status: str = None
    appointment_date: date = None
    appointment_time: str = None


class BookingLookup(BaseModel):
    phone: str
    confirmation_code: str


class BookingReschedule(BaseModel):
    phone: str
    confirmation_code: str
    new_date: date
    new_time: str


class BookingCancel(BaseModel):
    phone: str
    confirmation_code: str


def find_booking(db, phone: str, confirmation_code: str):
    return db.query(Inquiry).filter(
        Inquiry.phone == phone,
        Inquiry.confirmation_code == confirmation_code.strip().upper(),
        Inquiry.status != "Cancelled",
    ).first()


@app.post("/my-booking/lookup")
def lookup_booking(req: BookingLookup):
    db = SessionLocal()
    inquiry = find_booking(db, req.phone, req.confirmation_code)
    db.close()
    if not inquiry:
        raise HTTPException(status_code=404, detail="No matching booking found. Check your phone number and confirmation code.")
    return {
        "name": inquiry.name,
        "appointment_date": inquiry.appointment_date,
        "appointment_time": inquiry.appointment_time,
        "appointment_type": inquiry.appointment_type,
        "status": inquiry.status,
    }


@app.post("/my-booking/reschedule")
def reschedule_booking(req: BookingReschedule):
    if req.new_date.weekday() not in OPEN_WEEKDAYS:
        raise HTTPException(status_code=400, detail="Clinic is closed on weekends. Please pick a weekday.")
    if req.new_time not in SLOT_TIMES:
        raise HTTPException(status_code=400, detail="Invalid time slot.")
    if is_slot_in_past(req.new_date, req.new_time):
        raise HTTPException(status_code=400, detail="This date and time has already passed. Please choose a present or future time.")

    db = SessionLocal()
    inquiry = find_booking(db, req.phone, req.confirmation_code)
    if not inquiry:
        db.close()
        raise HTTPException(status_code=404, detail="No matching booking found. Check your phone number and confirmation code.")

    counts = slot_counts_for_date(db, req.new_date)
    already_in_target = (inquiry.appointment_date == req.new_date and inquiry.appointment_time == req.new_time)
    if not already_in_target and counts[req.new_time] >= SLOT_CAPACITY:
        db.close()
        raise HTTPException(status_code=409, detail="This date and time slot is already booked. Please choose another time.")

    old_date, old_time = inquiry.appointment_date, inquiry.appointment_time
    inquiry.appointment_date = req.new_date
    inquiry.appointment_time = req.new_time
    name, phone = inquiry.name, inquiry.phone
    db.commit()
    db.close()

    send_reschedule_notification(name, phone, old_date, old_time, req.new_date, req.new_time)

    return {"status": "rescheduled", "new_date": str(req.new_date), "new_time": req.new_time}


@app.post("/my-booking/cancel")
def cancel_booking_patient(req: BookingCancel):
    db = SessionLocal()
    inquiry = find_booking(db, req.phone, req.confirmation_code)
    if not inquiry:
        db.close()
        raise HTTPException(status_code=404, detail="No matching booking found. Check your phone number and confirmation code.")

    name, phone, appt_date, appt_time, appt_type = (
        inquiry.name, inquiry.phone, inquiry.appointment_date, inquiry.appointment_time, inquiry.appointment_type
    )
    inquiry.status = "Cancelled"
    db.commit()
    db.close()

    send_patient_cancel_notification(name, phone, appt_date, appt_time, appt_type)

    return {"status": "cancelled"}


@app.patch("/inquiries/{inquiry_id}", dependencies=[Depends(verify_admin)])
def update_inquiry(inquiry_id: int, update: StatusUpdate):
    db = SessionLocal()
    inquiry = db.query(Inquiry).filter(Inquiry.id == inquiry_id).first()
    if not inquiry:
        db.close()
        raise HTTPException(status_code=404, detail="Not found")

    was_cancelled_already = inquiry.status == "Cancelled"

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

    just_cancelled_paid_booking = (
        update.status == "Cancelled" and not was_cancelled_already and inquiry.appointment_type == "online"
    )
    name, phone, appt_date, appt_time = inquiry.name, inquiry.phone, inquiry.appointment_date, inquiry.appointment_time

    db.commit()
    db.close()

    if just_cancelled_paid_booking:
        send_refund_notification(name, phone, appt_date, appt_time)

    return {"status": "updated"}