import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
from datetime import datetime, date, timedelta, timezone
from collections import Counter

from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.tl.types import Chat, Channel
from openai import OpenAI
import schedule

from models import init_db, get_session, Group, Member, Message, Report, MemberStat, RunLog, GroupMember
from sender import collect_and_send_todays_data

load_dotenv()

db_engine = init_db()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
REPORT_CHAT = os.environ.get("TELEGRAM_REPORT_CHAT")  # where to send reports (optional)
REPORT_TIME = os.environ.get("REPORT_TIME", "09:00")  # daily report time (HH:MM)

openai_client = OpenAI(
    api_key=os.environ.get("PROXY_API_KEY"),
    base_url=os.environ.get("OPENAI_BASE_URL", "https://lively-breeze-0247.rimefara22.workers.dev/v1"),
)


def get_all_groups(client):
    """Return all groups and supergroups the user is a member of."""
    groups = []
    for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, Chat):
            groups.append(dialog)
        elif isinstance(entity, Channel) and entity.megagroup:
            groups.append(dialog)
    return groups


def fetch_messages(client, group):
    """Fetch non-empty text messages sent today (since midnight local time)."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    messages = []
    for msg in client.iter_messages(group.id, limit=1000):
        if msg.date < today_start:
            break
        if msg.text and msg.text.strip():
            messages.append(msg)
    return messages


def compute_member_stats(messages):
    """Return top-10 most active senders as (sender_id, name, count) tuples."""
    counts = Counter()
    id_to_name = {}
    for msg in messages:
        sender = msg.sender
        if sender and msg.sender_id:
            name = (
                getattr(sender, "username", None)
                or getattr(sender, "first_name", None)
                or "Unknown"
            )
            id_to_name[msg.sender_id] = name
            counts[msg.sender_id] += 1
    return [(sid, id_to_name[sid], count) for sid, count in counts.most_common(10)]


def analyze_with_ai(messages, group_name):
    """Run sentiment analysis and topic extraction via OpenAI."""
    sample_lines = []
    for msg in messages[:150]:  # cap to avoid token limits
        sender_name = "User"
        if msg.sender:
            sender_name = (
                getattr(msg.sender, "first_name", None)
                or getattr(msg.sender, "username", None)
                or "User"
            )
        sample_lines.append(f"[{sender_name}]: {msg.text}")

    sample = "\n".join(sample_lines)

    prompt = f"""You are analyzing Telegram group chat messages from "{group_name}" from today.

Messages:
{sample}

Provide a concise analysis with exactly these 3 sections:

1. **Sentiment**: Overall tone (Positive / Neutral / Negative) with a 1-2 sentence explanation.

2. **Top Topics**: List the 5 main subjects being discussed (one per line).

3. **Key Insights**: 2-3 notable patterns, highlights, or takeaways from the conversation.
"""

    response = openai_client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=800,
    )
    return response.choices[0].message.content


def format_report(group_name, messages, member_stats, ai_analysis):
    date_str = datetime.now().strftime("%Y-%m-%d")
    unique_senders = len({m.sender_id for m in messages if m.sender_id})

    lines = [
        f"📊 Daily Report — {group_name}",
        f"📅 {date_str}",
        "",
        "📈 Activity Summary",
        f"  • Total messages: {len(messages)}",
        f"  • Active members: {unique_senders}",
        "",
        "👥 Most Active Members",
    ]

    for rank, (sender_id, name, count) in enumerate(member_stats, 1):
        lines.append(f"  {rank}. {name} — {count} messages")

    lines += ["", "🤖 AI Analysis", "", ai_analysis]
    return "\n".join(lines)


def save_to_db(dialog, messages, member_stats, ai_analysis, report_text):
    """Persist group, members, messages, report and member stats to the DB."""
    with get_session(db_engine) as session:
        # -- Group (upsert) --------------------------------------------------
        group = session.get(Group, dialog.id)
        if group is None:
            group = Group(id=dialog.id, name=dialog.name)
            session.add(group)
        else:
            group.name = dialog.name

        # -- Members + Messages (upsert by Telegram ID) ----------------------
        updated_members = set()  # track who we've already refreshed this run
        for msg in messages:
            if msg.sender and msg.sender_id:
                sender = msg.sender
                display_name = (
                    getattr(sender, "username", None)
                    or getattr(sender, "first_name", None)
                    or "Unknown"
                )
                member = session.get(Member, msg.sender_id)
                if member is None:
                    member = Member(
                        id=msg.sender_id,
                        username=getattr(sender, "username", None),
                        first_name=getattr(sender, "first_name", None),
                        display_name=display_name,
                    )
                    session.add(member)
                    updated_members.add(msg.sender_id)
                elif msg.sender_id not in updated_members:
                    # Refresh name fields in case the user changed them
                    member.username = getattr(sender, "username", None)
                    member.first_name = getattr(sender, "first_name", None)
                    member.display_name = display_name
                    updated_members.add(msg.sender_id)

            if session.get(Message, (msg.id, dialog.id)) is None:
                session.add(Message(
                    id=msg.id,
                    group_id=dialog.id,
                    sender_id=msg.sender_id,
                    text=msg.text,
                    sent_at=msg.date.replace(tzinfo=None),  # store as naive UTC
                ))

        session.flush()

        # -- GroupMember (upsert — tracks lifetime membership per group) ------
        # Count how many messages each sender contributed this run
        run_counts: dict[int, int] = {}
        for msg in messages:
            if msg.sender_id:
                run_counts[msg.sender_id] = run_counts.get(msg.sender_id, 0) + 1

        now = datetime.utcnow()
        for sender_id, msg_count in run_counts.items():
            gm = session.get(GroupMember, (dialog.id, sender_id))
            if gm is None:
                gm = GroupMember(
                    group_id=dialog.id,
                    member_id=sender_id,
                    joined_at=now,
                    last_seen_at=now,
                    total_messages=msg_count,
                )
                session.add(gm)
            else:
                gm.last_seen_at = now
                gm.total_messages = (gm.total_messages or 0) + msg_count

        session.flush()

        today = date.today()
        report = (
            session.query(Report)
            .filter_by(group_id=dialog.id, report_date=today)
            .first()
        )
        if report is None:
            report = Report(group_id=dialog.id, report_date=today)
            session.add(report)

        report.total_messages = len(messages)
        report.active_members = len({m.sender_id for m in messages if m.sender_id})
        report.messages_analyzed = min(150, len(messages))
        report.ai_analysis = ai_analysis
        report.report_text = report_text

        session.flush()

        # Delete stale stats so a re-run of the same day overwrites cleanly
        for old in list(report.member_stats):
            session.delete(old)
        session.flush()

        # -- MemberStats -----------------------------------------------------
        for rank, (sender_id, name, count) in enumerate(member_stats, 1):
            session.add(MemberStat(
                report_id=report.id,
                member_id=sender_id,
                display_name=name,
                message_count=count,
                rank=rank,
            ))

        session.commit()
        print(f"    Saved to DB — report id={report.id}, {len(messages)} messages.")


def _save_run_log(started_at, status, groups_processed, error_message=None):
    with get_session(db_engine) as session:
        session.add(RunLog(
            started_at=started_at,
            finished_at=datetime.utcnow(),
            status=status,
            groups_processed=groups_processed,
            error_message=error_message,
        ))
        session.commit()


def run_daily_report():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] Starting daily analysis for all groups...")

    started_at = datetime.utcnow()
    groups_processed = 0

    try:
        with TelegramClient("session", API_ID, API_HASH) as client:
            groups = get_all_groups(client)
            print(f"Found {len(groups)} group(s).\n")

            for dialog in groups:
                group_name = dialog.name
                print(f"  Analyzing: {group_name}")

                messages = fetch_messages(client, dialog)

                if not messages:
                    print(f"    No messages today, skipping.\n")
                    continue

                print(f"    Fetched {len(messages)} messages. Running AI analysis...")

                member_stats = compute_member_stats(messages)
                ai_analysis = analyze_with_ai(messages, group_name)
                report = format_report(group_name, messages, member_stats, ai_analysis)

                print("\n" + report + "\n")

                save_to_db(dialog, messages, member_stats, ai_analysis, report)
                groups_processed += 1

                if REPORT_CHAT:
                    client.send_message(REPORT_CHAT, report)
                    print(f"    Report sent to '{REPORT_CHAT}'.\n")

        _save_run_log(started_at, "success", groups_processed)

        # -- Send collected data to the webhook server ----------------------
        if groups_processed > 0:
            print("\nSending today's data to webhook server...")
            sent = collect_and_send_todays_data()
            if sent:
                print("  Data sent successfully.")
            else:
                print("  Warning: data was not sent (check logs).")

    except Exception as e:
        print(f"  [ERROR] {e}")
        _save_run_log(started_at, "error", groups_processed, str(e))
        raise


if __name__ == "__main__":
    # Run immediately on startup
    run_daily_report()

    # Then schedule a daily run
    schedule.every().day.at(REPORT_TIME).do(run_daily_report)
    print(f"\nScheduler active — next report at {REPORT_TIME} daily. Press Ctrl+C to stop.\n")

    while True:
        schedule.run_pending()
        time.sleep(30)
