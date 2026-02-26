"""
SQLAlchemy ORM models for persisting chat analyzer data.

Tables
------
groups       — Telegram groups/channels being monitored
members      — Telegram users seen in any group
messages     — Individual text messages fetched each run
reports      — Daily AI-generated reports (one per group per day)
member_stats — Per-report message counts for each member
"""

from datetime import datetime
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    DateTime,
    Date,
    ForeignKey,
)
from sqlalchemy.orm import DeclarativeBase, relationship, Session

DATABASE_URL = "sqlite:///chat_analyzer.db"


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------

class Group(Base):
    """A Telegram group or supergroup being monitored."""

    __tablename__ = "groups"

    id = Column(BigInteger, primary_key=True)   # Telegram chat ID
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    messages = relationship("Message", back_populates="group", cascade="all, delete-orphan")
    reports = relationship("Report", back_populates="group", cascade="all, delete-orphan")
    group_members = relationship("GroupMember", back_populates="group", cascade="all, delete-orphan")
    members = relationship("Member", secondary="group_members", back_populates="groups", viewonly=True)

    def __repr__(self):
        return f"<Group id={self.id} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Member
# ---------------------------------------------------------------------------

class Member(Base):
    """A Telegram user seen in at least one monitored group."""

    __tablename__ = "members"

    id = Column(BigInteger, primary_key=True)   # Telegram user ID
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    display_name = Column(String(255), nullable=False)  # resolved name used in reports
    first_seen_at = Column(DateTime, default=datetime.utcnow)

    messages = relationship("Message", back_populates="sender")
    stats = relationship("MemberStat", back_populates="member")
    group_memberships = relationship("GroupMember", back_populates="member", cascade="all, delete-orphan")
    groups = relationship("Group", secondary="group_members", back_populates="members", viewonly=True)

    def __repr__(self):
        return f"<Member id={self.id} display_name={self.display_name!r}>"


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------

class Message(Base):
    """A single text message fetched from a group."""

    __tablename__ = "messages"

    # Telegram message IDs are scoped per chat, so the true key is (id, group_id)
    id = Column(BigInteger, primary_key=True)
    group_id = Column(BigInteger, ForeignKey("groups.id"), primary_key=True)
    sender_id = Column(BigInteger, ForeignKey("members.id"), nullable=True)
    text = Column(Text, nullable=False)
    sent_at = Column(DateTime, nullable=False)           # original Telegram timestamp (UTC)
    fetched_at = Column(DateTime, default=datetime.utcnow)

    group = relationship("Group", back_populates="messages")
    sender = relationship("Member", back_populates="messages")

    def __repr__(self):
        preview = self.text[:40].replace("\n", " ")
        return f"<Message id={self.id} sent_at={self.sent_at} text={preview!r}>"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class Report(Base):
    """Daily analysis report generated for a group."""

    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(BigInteger, ForeignKey("groups.id"), nullable=False)
    report_date = Column(Date, nullable=False)           # the calendar day this report covers
    total_messages = Column(Integer, nullable=False, default=0)
    active_members = Column(Integer, nullable=False, default=0)
    ai_analysis = Column(Text, nullable=True)            # raw AI response text
    report_text = Column(Text, nullable=True)            # full formatted report string
    messages_analyzed = Column(Integer, nullable=True)   # how many msgs the AI actually saw (capped at 150)
    created_at = Column(DateTime, default=datetime.utcnow)

    group = relationship("Group", back_populates="reports")
    member_stats = relationship("MemberStat", back_populates="report", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Report id={self.id} group_id={self.group_id} date={self.report_date}>"


# ---------------------------------------------------------------------------
# GroupMember  (group ↔ member join table — direct membership)
# ---------------------------------------------------------------------------

class GroupMember(Base):
    """Tracks membership between a Group and a Member.

    One row per (group, member) pair. Updated on every run to keep
    last_seen_at and total_messages current.
    """

    __tablename__ = "group_members"

    group_id       = Column(BigInteger, ForeignKey("groups.id"),  primary_key=True)
    member_id      = Column(BigInteger, ForeignKey("members.id"), primary_key=True)
    joined_at      = Column(DateTime, default=datetime.utcnow)    # first time member seen in group
    last_seen_at   = Column(DateTime, default=datetime.utcnow)    # most recent activity
    total_messages = Column(Integer, default=0)                   # lifetime message count in group

    group  = relationship("Group",  back_populates="group_members")
    member = relationship("Member", back_populates="group_memberships")

    def __repr__(self):
        return f"<GroupMember group={self.group_id} member={self.member_id} msgs={self.total_messages}>"


# ---------------------------------------------------------------------------
# MemberStat  (report ↔ member join table with message count)
# ---------------------------------------------------------------------------

class MemberStat(Base):
    """Per-member message count for a single daily report."""

    __tablename__ = "member_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_id = Column(Integer, ForeignKey("reports.id"), nullable=False)
    member_id = Column(BigInteger, ForeignKey("members.id"), nullable=True)
    display_name = Column(String(255), nullable=False)  # snapshot of name at report time
    message_count = Column(Integer, nullable=False, default=0)
    rank = Column(Integer, nullable=False, default=0)   # 1 = most active

    report = relationship("Report", back_populates="member_stats")
    member = relationship("Member", back_populates="stats")

    def __repr__(self):
        return f"<MemberStat report={self.report_id} name={self.display_name!r} count={self.message_count}>"


# ---------------------------------------------------------------------------
# RunLog
# ---------------------------------------------------------------------------

class RunLog(Base):
    """One row per scheduled run — records outcome and any error."""

    __tablename__ = "run_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False)          # 'success' or 'error'
    groups_processed = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)

    def __repr__(self):
        return f"<RunLog id={self.id} status={self.status} started={self.started_at}>"


# ---------------------------------------------------------------------------
# Engine / session helpers
# ---------------------------------------------------------------------------

def get_engine(url: str = DATABASE_URL):
    return create_engine(url, echo=False)


def init_db(url: str = DATABASE_URL):
    """Create all tables (idempotent — safe to call on every startup)."""
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    return engine


def get_session(engine) -> Session:
    return Session(engine)
