from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
from fastapi.middleware.cors import CORSMiddleware

engine = create_engine("sqlite:///clinic.db")
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)

class Inquiry(Base):
    __tablename__ = "inquiries"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    phone = Column(String)
    preferred_time = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="New")

Base.metadata.create_all(engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class InquiryIn(BaseModel):
    name: str
    phone: str
    preferred_time: str = ""

class StatusUpdate(BaseModel):
    status: str

@app.post("/inquiries")
def create_inquiry(inquiry: InquiryIn):
    db = SessionLocal()
    new_inquiry = Inquiry(name=inquiry.name, phone=inquiry.phone, preferred_time=inquiry.preferred_time)
    db.add(new_inquiry)
    db.commit()
    db.refresh(new_inquiry)
    db.close()
    return {"status": "saved", "id": new_inquiry.id}

@app.get("/inquiries")
def list_inquiries():
    db = SessionLocal()
    results = db.query(Inquiry).all()
    db.close()
    return results

@app.patch("/inquiries/{inquiry_id}")
def update_status(inquiry_id: int, update: StatusUpdate):
    db = SessionLocal()
    inquiry = db.query(Inquiry).filter(Inquiry.id == inquiry_id).first()
    if inquiry:
        inquiry.status = update.status
        db.commit()
    db.close()
    return {"status": "updated"}