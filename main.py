import os
import secrets
from datetime import datetime, date, timedelta

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Date, text
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///clinic.db")
engine = create_engine(DATABASE_URL)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)

# Clinic booking rules
SLOT_TIMES = ["19:30", "20:00", "20:30", "21:00", "21:30"]  # 7:30 PM to 9:30 PM start times, 30-min each, last ends 10:00 PM
SLOT_CAPACITY = 3  # patients allowed per slot, waiting-room style
OPEN_WEEKDAYS = {0, 1, 2, 3, 4}  # Monday=0 ... Friday=4 (Mon-Fri only)


class Inquiry(Base):
    __tablename__ = "inquiries"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    phone = Column(String)
    preferred_time = Column(String)  # legacy free-text field, kept for old rows, no longer written to
    appointment_date = Column(Date)
    appointment_time = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="New")


Base.metadata.create_all(engine)

# Add the new columns to an already-existing table (safe to run every startup)
with engine.connect() as conn:
    conn.execute(text("ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS appointment_date DATE"))
    conn.execute(text("ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS appointment_time VARCHAR"))
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


class InquiryIn(BaseModel):
    name: str
    phone: str
    appointment_date: date
    appointment_time: str


class StatusUpdate(BaseModel):
    status: str = None
    appointment_date: date = None
    appointment_time: str = None


def slot_counts_for_date(db, target_date: date):
    """Returns {time: booked_count} for every slot on a given date."""
    rows = db.query(Inquiry).filter(Inquiry.appointment_date == target_date).all()
    counts = {t: 0 for t in SLOT_TIMES}
    for row in rows:
        if row.appointment_time in counts:
            counts[row.appointment_time] += 1
    return counts


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
        }
        for t in SLOT_TIMES
    ]
    return {"date": str(for_date), "open": True, "slots": slots}


@app.post("/inquiries")
def create_inquiry(inquiry: InquiryIn):
    if inquiry.appointment_date.weekday() not in OPEN_WEEKDAYS:
        raise HTTPException(status_code=400, detail="Clinic is closed on weekends. Please pick a weekday.")
    if inquiry.appointment_time not in SLOT_TIMES:
        raise HTTPException(status_code=400, detail="Invalid time slot.")

    db = SessionLocal()
    counts = slot_counts_for_date(db, inquiry.appointment_date)
    if counts[inquiry.appointment_time] >= SLOT_CAPACITY:
        db.close()
        raise HTTPException(status_code=409, detail="This slot is full. Please choose another time.")

    new_inquiry = Inquiry(
        name=inquiry.name,
        phone=inquiry.phone,
        appointment_date=inquiry.appointment_date,
        appointment_time=inquiry.appointment_time,
    )
    db.add(new_inquiry)
    db.commit()
    db.refresh(new_inquiry)
    db.close()
    return {"status": "saved", "id": new_inquiry.id}


@app.get("/inquiries", dependencies=[Depends(verify_admin)])
def list_inquiries():
    db = SessionLocal()
    results = db.query(Inquiry).all()
    db.close()
    return results


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
        # allow moving into the same slot the inquiry is already in
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