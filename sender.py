"""
sender.py — Collect today's data from the DB and send it via HTTP POST
using the `requests` library.

Usage (standalone):
    python sender.py

Or call programmatically:
    from sender import collect_and_send_todays_data
    collect_and_send_todays_data()

Required .env keys (add to your .env file):
    WEBHOOK_URL=https://your-server.example.com/api/daily-report
    WEBHOOK_SECRET=optional_bearer_token   # sent as Authorization: Bearer <token>
"""

import os
import json
import logging
from datetime import date, datetime

import requests
from dotenv import load_dotenv

from models import init_db, get_session, Report, MemberStat, Group, Member, GroupMember

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------------------------------------------------------------------------
# Configuration (read from environment)
# ---------------------------------------------------------------------------
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")          # required
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")    # optional Bearer token
REQUEST_TIMEOUT = int(os.environ.get("WEBHOOK_TIMEOUT", "15"))  # seconds


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def collect_todays_data(engine) -> list[dict]:
    """
    Query the SQLite DB for every report generated today and return a list
    of structured dicts — one per group.

    Each entry now includes full group info (id, name, created_at) and a
    group_members list of every distinct member who posted a message in that
    group today.

    Returns an empty list if no reports exist for today yet.
    """
    today = date.today()
    payload_rows = []

    with get_session(engine) as session:
        reports = (
            session.query(Report)
            .filter(Report.report_date == today)
            .all()
        )

        if not reports:
            logger.info("No reports found for today (%s).", today)
            return []

        for report in reports:
            group: Group = report.group

            # ----------------------------------------------------------------
            # Group members — all members ever seen in the group (via GroupMember)
            # ----------------------------------------------------------------
            gm_rows = (
                session.query(GroupMember, Member)
                .join(Member, Member.id == GroupMember.member_id)
                .filter(GroupMember.group_id == group.id)
                .order_by(GroupMember.total_messages.desc())
                .all()
            )

            group_members = [
                {
                    "id": member.id,
                    "username": member.username,
                    "first_name": member.first_name,
                    "display_name": member.display_name,
                    "first_seen_at": member.first_seen_at.isoformat() + "Z"
                    if member.first_seen_at else None,
                    "joined_group_at": gm.joined_at.isoformat() + "Z"
                    if gm.joined_at else None,
                    "last_seen_at": gm.last_seen_at.isoformat() + "Z"
                    if gm.last_seen_at else None,
                    "total_messages": gm.total_messages,
                }
                for gm, member in gm_rows
            ]

            # ----------------------------------------------------------------
            # Top members for this report (ranked by activity)
            # ----------------------------------------------------------------
            top_members = [
                {
                    "rank": stat.rank,
                    "display_name": stat.display_name,
                    "message_count": stat.message_count,
                }
                for stat in sorted(report.member_stats, key=lambda s: s.rank)
            ]

            payload_rows.append({
                "report_id": report.id,
                "report_date": str(report.report_date),
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "group": {
                    "id": group.id,
                    "name": group.name,
                    "created_at": group.created_at.isoformat() + "Z"
                    if group.created_at else None,
                    "total_members_today": len(group_members),
                },
                "group_members": group_members,
                "stats": {
                    "total_messages": report.total_messages,
                    "active_members": report.active_members,
                    "messages_analyzed": report.messages_analyzed,
                },
                "top_members": top_members,
                "ai_analysis": report.ai_analysis,
                "report_text": report.report_text,
            })

    logger.info("Collected data for %d group(s) for %s.", len(payload_rows), today)
    return payload_rows


# ---------------------------------------------------------------------------
# HTTP sender
# ---------------------------------------------------------------------------

def send_data(payload: list[dict], url: str = WEBHOOK_URL, secret: str = WEBHOOK_SECRET) -> bool:
    """
    POST today's report data as JSON to the given URL.

    Args:
        payload: List of report dicts produced by collect_todays_data().
        url:     Webhook endpoint to POST to.
        secret:  Optional Bearer token added to Authorization header.

    Returns:
        True if the server responded with 2xx, False otherwise.
    """
    if not url:
        raise ValueError(
            "WEBHOOK_URL is not configured. "
            "Set it in your .env file or pass it explicitly."
        )

    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    body = {
        "sent_at": datetime.utcnow().isoformat() + "Z",
        "reports": payload,
    }

    logger.info("Sending %d report(s) to %s …", len(payload), url)
    try:
        response = requests.post(
            url,
            data=json.dumps(body, ensure_ascii=False),
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        logger.info("Server responded %s %s.", response.status_code, response.reason)
        return True

    except requests.exceptions.HTTPError as exc:
        logger.error("HTTP error: %s — %s", exc.response.status_code, exc.response.text[:200])
    except requests.exceptions.ConnectionError as exc:
        logger.error("Connection error: %s", exc)
    except requests.exceptions.Timeout:
        logger.error("Request timed out after %ds.", REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        logger.error("Unexpected requests error: %s", exc)

    return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def collect_and_send_todays_data(
    webhook_url: str = WEBHOOK_URL,
    webhook_secret: str = WEBHOOK_SECRET,
) -> bool:
    """
    Collect today's analyzed chat data from the local SQLite DB and send it
    via an HTTP POST request using the `requests` library.

    Args:
        webhook_url:    Target endpoint URL (default: WEBHOOK_URL env var).
        webhook_secret: Optional Bearer token (default: WEBHOOK_SECRET env var).

    Returns:
        True if data was sent successfully, False on error or if no data.
    """
    engine = init_db()
    data = collect_todays_data(engine)

    if not data:
        logger.warning("Nothing to send — no reports for today.")
        return False

    return send_data(data, url=webhook_url, secret=webhook_secret)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    success = collect_and_send_todays_data()
    raise SystemExit(0 if success else 1)
