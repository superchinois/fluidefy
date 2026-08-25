import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text
from ..database import Base

class StagingOrder(Base):
    __tablename__ = "staging_orders"

    id = Column(Integer, primary_key=True, index=True)
    file_name = Column(String(255), nullable=False)
    date_of_arrival = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    file_hash = Column(String(64), unique=True, index=True, nullable=False)
    file_base64 = Column(Text, nullable=False)
    processing_status = Column(String(50), default="pending", nullable=False) # pending, integrated, failed
    error_log = Column(Text, nullable=True)