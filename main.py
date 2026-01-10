# bot.py (PTB v13 / non-async / python-telegram-bot v13)
# =========================================================
# FINAL FIXED A TO Z (Your requested behaviors)
#
# ✅ Giveaway post style (short borders, no breaking)
# ✅ Verify targets (multiple)
# ✅ Permanent block works
# ✅ Old winner block works
# ✅ First join champion popup (same always)
# ✅ Join again popup
# ✅ Giveaway close behavior:
#    - AutoDraw OFF: CLOSED post has live spinner + progress bar (edit every 5s)
#    - AutoDraw ON : CLOSED post is SIMPLE (no live)
# ✅ AutoDraw system:
#    - /Autodraw -> ON/OFF
#    - ON: on giveaway end -> pinned Auto Selection post runs 5 minutes
#          - progress + spinner + time updates every 1 second (smooth, no lag)
#          - shows 3 entries at once:
#            line1 changes every 5s, line2 every 6s, line3 every 7s
#          - colorful icons rotate (premium look)
#          - after 100%: deletes CLOSED + pinned selection post automatically
#          - posts Winners automatically with Claim button
#    - OFF: admin /draw -> progress in admin -> preview -> Approve posts to channel
# ✅ Manual draw fixed: after 100% you always get winner preview with Approve/Reject
# ✅ Claim system:
#    - Within 24h:
#       Winner -> claim popup
#       Delivered winner -> "Prize already delivered" popup
#       Non-winner -> "You are not a winner" popup
#    - After 24h:
#       Winner -> "Prize expired" popup
#       Non-winner -> "Giveaway completed" popup
# ✅ Multiple winners posts supported:
#    - Claim button points to correct giveaway ID (each post separate)
# ✅ Giveaway ID format:
#    - P788-P686-B6548 (random unique)
# ✅ /prizeDelivered:
#    - Step1: send giveaway ID
#    - Step2: send list @user | id OR id
#    - Bot marks delivered ✅ on winners post + updates delivery count
#
# =========================================================

import os
import json
import random
import threading
import secrets
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)

# =========================================================
# LOAD ENV
# =========================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

HOST_NAME = os.getenv("HOST_NAME", "POWER POINT BREAK")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@PowerPointBreak")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/PowerPointBreak")

ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@MinexxProo")
DATA_FILE = os.getenv("DATA_FILE", "giveaway_data.json")

# =========================================================
# THREAD SAFE STORAGE
# =========================================================
lock = threading.RLock()

# =========================================================
# GLOBAL STATE
# =========================================================
data = {}
admin_state = None

countdown_job = None
closed_anim_job = None

draw_job = None
draw_finalize_job = None

auto_draw_job = None
auto_draw_finalize_job = None

# =========================================================
# CONSTANTS
# =========================================================
DOTS = [".", "..", "...", "....", ".....", "......", "......."]
SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
SHOW_COLORS = ["🟣", "🟠", "🟢", "🔵", "🟡", "🔴", "⚪", "⚫"]

DRAW_DURATION_SECONDS = 40
DRAW_UPDATE_INTERVAL = 0.7

AUTO_DRAW_DURATION_SECONDS = 5 * 60
AUTO_TICK_INTERVAL = 1.0
CLOSED_TICK_INTERVAL = 5.0

# =========================================================
# DATA / STORAGE
# =========================================================
def fresh_default_data():
    return {
        "active": False,
        "closed": False,

        "title": "",
        "prize": "",
        "winner_count": 0,
        "duration_seconds": 0,
        "rules": "",

        "start_time": None,

        "live_message_id": None,
        "closed_message_id": None,

        "participants": {},  # uid(str) -> {"username": "@x" or "", "name": ""}

        "verify_targets": [],  # [{"ref": "-100..." or "@xxx", "display": "..."}]

        "permanent_block": {},

        "old_winner_mode": "skip",  # "block" or "skip"
        "old_winners": {},

        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # current giveaway winners (for manual approve preview)
        "winners": {},
        "pending_winners_text": "",
        "pending_winners_gid": "",

        # claim window per current winners post (manual flow)
        "claim_start_ts": None,
        "claim_expires_ts": None,

        # autodraw
        "auto_draw": False,
        "autodraw_message_id": None,

        # history for multiple posts
        "history": {},  # gid -> snapshot
    }


def load_data():
    base = fresh_default_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}

    for k, v in base.items():
        d.setdefault(k, v)

    # normalize types
    if not isinstance(d.get("participants"), dict):
        d["participants"] = {}
    if not isinstance(d.get("verify_targets"), list):
        d["verify_targets"] = []
    if not isinstance(d.get("permanent_block"), dict):
        d["permanent_block"] = {}
    if not isinstance(d.get("old_winners"), dict):
        d["old_winners"] = {}
    if not isinstance(d.get("history"), dict):
        d["history"] = {}

    return d


def save_data():
    with lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)


data = load_data()

# =========================================================
# HELPERS
# =========================================================
def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == ADMIN_ID)


def user_tag(username: str) -> str:
    if not username:
        return ""
    u = username.strip()
    if not u:
        return ""
    return u if u.startswith("@") else "@" + u


def participants_count() -> int:
    return len(data.get("participants", {}))


def now_ts() -> float:
    return datetime.utcnow().timestamp()


def format_hms(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_progress(percent: float) -> str:
    percent = max(0, min(100, percent))
    blocks = 10
    filled = int(round(blocks * percent / 100.0))
    empty = blocks - filled
    return "▰" * filled + "▱" * empty


def parse_duration(text: str) -> int:
    t = (text or "").strip().lower()
    parts = t.split()
    if len(parts) == 1 and parts[0].isdigit():
        return int(parts[0])

    if not parts or not parts[0].isdigit():
        return 0

    num = int(parts[0])
    unit = "".join(parts[1:])

    if unit.startswith("sec"):
        return num
    if unit.startswith("min"):
        return num * 60
    if unit.startswith("hour") or unit.startswith("hr"):
        return num * 3600
    return num


def format_rules() -> str:
    rules = (data.get("rules") or "").strip()
    if not rules:
        return (
            "• Must join the official channel\n"
            "• One entry per user only\n"
            "• Stay active until result announcement\n"
            "• Admin decision is final & binding"
        )
    lines = [l.strip() for l in rules.splitlines() if l.strip()]
    return "\n".join("• " + l for l in lines)


def normalize_verify_ref(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    if s.startswith("-") and s[1:].isdigit():
        return s
    if s.startswith("@"):
        return s
    raw = s.replace(" ", "")
    if "t.me/" in raw:
        slug = raw.split("t.me/", 1)[1]
        slug = slug.split("?", 1)[0]
        slug = slug.split("/", 1)[0]
        if slug and not slug.startswith("+"):
            return user_tag(slug)
    return ""


def verify_user_join(bot, user_id: int) -> bool:
    targets = data.get("verify_targets", []) or []
    if not targets:
        return True

    for t in targets:
        ref = (t or {}).get("ref", "")
        if not ref:
            return False
        try:
            member = bot.get_chat_member(chat_id=ref, user_id=user_id)
            status = getattr(member, "status", None)
            if status not in ("member", "administrator", "creator"):
                return False
        except Exception:
            return False
    return True


def parse_user_lines(text: str):
    out = []
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for line in lines:
        if "|" in line:
            left, right = line.split("|", 1)
            uname = user_tag(left.strip().lstrip("@"))
            uid = right.strip().replace(" ", "")
            if uid.isdigit():
                out.append((uid, uname))
        else:
            uid = line.strip().replace(" ", "")
            if uid.isdigit():
                out.append((uid, ""))
    return out


def make_gid() -> str:
    with lock:
        hist = data.get("history", {}) or {}
        while True:
            part1 = secrets.randbelow(900) + 100
            part2 = secrets.randbelow(900) + 100
            part3 = secrets.randbelow(9000) + 1000
            gid = f"P{part1}-P{part2}-B{part3}"
            if gid not in hist:
                return gid


def format_entry(uid: str, uname: str) -> str:
    if uname:
        return f"{uname}  |  🆔 {uid}"
    return f"User  |  🆔 {uid}"


# =========================================================
# MARKUPS
# =========================================================
def join_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data="join_giveaway")]]
    )


def claim_button_markup(gid: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data=f"claim_prize|{gid}")]]
    )


def winners_approve_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Post", callback_data="winners_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="winners_reject"),
        ]]
    )


def preview_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✔️ Approve & Post", callback_data="preview_approve"),
                InlineKeyboardButton("❌ Reject Giveaway", callback_data="preview_reject"),
            ],
            [InlineKeyboardButton("✏️ Edit Again", callback_data="preview_edit")],
        ]
    )


def verify_add_more_done_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("➕ Add Another Link", callback_data="verify_add_more"),
            InlineKeyboardButton("✅ Done", callback_data="verify_add_done"),
        ]]
    )


def end_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm End", callback_data="end_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="end_cancel"),
        ]]
    )


def autodraw_toggle_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Auto Draw ON", callback_data="autodraw_on"),
            InlineKeyboardButton("❌ Auto Draw OFF", callback_data="autodraw_off"),
        ]]
    )


# =========================================================
# POPUPS
# =========================================================
def popup_verify_required() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n"
        "To join this giveaway, you must join the required channels/groups first ✅\n"
        "👇 After joining all of them, click JOIN GIVEAWAY again."
    )


def popup_old_winner_blocked() -> str:
    return (
        "🚫 YOU ARE BLOCKED\n"
        "You have already won a previous giveaway.\n"
        "Repeat winners are restricted to keep it fair.\n"
        "🙏 Please wait for the next giveaway."
    )


def popup_first_winner(username: str, uid: str) -> str:
    return (
        "🥇 FIRST JOIN CHAMPION 🌟\n"
        "Congratulations! You joined\n"
        "the giveaway FIRST and secured\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n"
        "📸 Please take a screenshot\n"
        "and post it in the group\n"
        "to confirm 👈"
    )


def popup_already_joined() -> str:
    return (
        "🚫 ENTRY UNSUCCESSFUL\n"
        "You’ve already joined\n"
        "this giveaway 🎁\n\n"
        "Multiple entries aren’t allowed.\n"
        "Please wait for the final result ⏳"
    )


def popup_join_success(username: str, uid: str) -> str:
    return (
        "🌹 CONGRATULATIONS!\n"
        "You’ve successfully joined\n"
        "the giveaway ✅\n\n"
        "Your details:\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        f"— {HOST_NAME}"
    )


def popup_permanent_blocked() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n"
        "You are permanently blocked from joining giveaways.\n"
        f"If you believe this is a mistake, contact admin: {ADMIN_CONTACT}"
    )


def popup_claim_winner(username: str, uid: str, title: str, prize: str, admin_contact: str) -> str:
    return (
        "🌟Congratulations✨\n"
        "You’ve won this giveaway.\n"
        f"🎯 Giveaway: {title}\n"
        f"🎁 Prize: {prize}\n"
        f"👤 {username} | 🆔 {uid}\n\n"
        "📩 Please contact admin to claim:\n"
        f"👉 {admin_contact}"
    )


def popup_claim_not_winner() -> str:
    return (
        "❌ YOU ARE NOT A WINNER\n\n"
        "Sorry! Your User ID is not\n"
        "in the winners list.\n\n"
        "Please wait for the next\n"
        "giveaway ❤️‍🩹"
    )


def popup_prize_expired() -> str:
    return (
        "⏳ PRIZE EXPIRED\n"
        "Your 24-hour claim time has ended.\n"
        "This prize is no longer available."
    )


def popup_giveaway_completed() -> str:
    return (
        "✅ GIVEAWAY COMPLETED\n\n"
        "This giveaway has been completed.\n"
        f"If you have any issues, please contact admin 👉 {ADMIN_CONTACT}"
    )


def popup_prize_already_delivered(uname: str, uid: str, admin_contact: str) -> str:
    return (
        "📦 PRIZE ALREADY DELIVERED\n"
        "Your prize has already been\n"
        "successfully delivered ✅\n"
        f"👤 {uname}\n"
        f"🆔 {uid}\n"
        "If you face any issue,\n"
        f"contact admin 👉 {admin_contact}"
    )


# =========================================================
# GIVEAWAY TEXT BUILDERS
# =========================================================
def build_preview_text() -> str:
    remaining = data.get("duration_seconds", 0)
    progress = build_progress(0)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 GIVEAWAY PREVIEW (ADMIN)\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ {data.get('title','')} ⚡\n\n"
        "🎁 Prize:\n"
        f"{data.get('prize','')}\n\n"
        "👥 Total Participants: 0\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        "🎯 Winner Selection\n"
        "• 100% Random & Fair\n"
        "• Auto System\n\n"
        f"⏱️ Time Remaining: {format_hms(remaining)}\n"
        "📊 Live Progress\n"
        f"{progress}\n\n"
        "📜 Official Rules\n"
        f"{format_rules()}\n\n"
        f"📢 Hosted by: {HOST_NAME}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Tap below to join the giveaway 👇"
    )


def build_live_text(remaining: int) -> str:
    duration = data.get("duration_seconds", 1) or 1
    elapsed = duration - remaining
    elapsed = max(0, min(duration, elapsed))
    percent = int(round((elapsed / float(duration)) * 100))
    progress = build_progress(percent)

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ {HOST_NAME} GIVEAWAY ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{data.get('prize','')}\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        "🎯 Winner Selection\n"
        "• 100% Random & Fair\n"
        "• Auto System\n\n"
        f"⏱️ Time Remaining: {format_hms(remaining)}\n"
        "📊 Live Progress\n"
        f"{progress}\n\n"
        "📜 Official Rules\n"
        f"{format_rules()}\n\n"
        f"📢 Hosted by: {HOST_NAME}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Tap below to join the giveaway 👇"
    )


def build_closed_post_text(spin: str, percent: int) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY OFFICIALLY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        f"{spin} Winner selection is currently in progress\n"
        f"📊 Progress: {bar}  {percent}%\n\n"
        "Please wait for the official announcement.\n\n"
        f"🙏 Thank you to everyone who participated.\n"
        f"— {HOST_NAME} ⚡"
    )


def build_closed_simple_text() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY OFFICIALLY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        "Winner selection will be announced shortly.\n\n"
        f"— {HOST_NAME} ⚡"
    )


def build_draw_progress_text(percent: int, spin: str, dots: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{spin} Selecting winners: {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        "🔐 100% fair & random system\n"
        "🔒 User ID based selection only\n\n"
        f"Please wait{dots}"
    )


def build_autodraw_text(percent: int, remaining: int, spin: str,
                       line1: str, c1: str,
                       line2: str, c2: str,
                       line3: str, c3: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 AUTO RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{spin} Selecting winners: {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        f"🕒 Time Remaining: {format_hms(remaining)}\n"
        "🔐 100% Random • Fair • Auto System\n\n"
        "👥 Live Entries Showcase\n"
        f"{c1} ➤ Now showing: {line1}\n"
        f"{c2} ➤ Now showing: {line2}\n"
        f"{c3} ➤ Now showing: {line3}\n\n"
        "Please wait ......."
    )


def build_winners_post_text(gid: str, title: str, prize: str,
                           first_uid: str, first_user: str,
                           others: list, delivered: dict) -> str:
    delivered = delivered or {}
    total = 1 + len(others)
    delivered_count = sum(1 for k in delivered if delivered.get(k))

    lines = []
    lines.append("🏆 GIVEAWAY WINNER ANNOUNCEMENT 🏆")
    lines.append("")
    lines.append(f"{HOST_NAME}")
    lines.append("")
    lines.append(f"🆔 Giveaway ID: {gid}")
    lines.append("")
    lines.append(f"🎁 PRIZE: {prize}")
    lines.append(f"📦 Prize Delivery: {delivered_count}/{total}")
    lines.append("")
    lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
    flag = "✅ Delivered" if delivered.get(first_uid) else ""
    if first_user:
        if flag:
            lines.append(f"👑 {first_user} | 🆔 {first_uid} | {flag}")
        else:
            lines.append(f"👑 {first_user}")
            lines.append(f"🆔 {first_uid}")
    else:
        if flag:
            lines.append(f"👑 User | 🆔 {first_uid} | {flag}")
        else:
            lines.append("👑 User")
            lines.append(f"🆔 {first_uid}")
    lines.append("")
    lines.append("👑 OTHER WINNERS")
    i = 1
    for uid, uname in others:
        flag = "✅ Delivered" if delivered.get(uid) else ""
        if uname:
            if flag:
                lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid} | {flag}")
            else:
                lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}")
        else:
            if flag:
                lines.append(f"{i}️⃣ 👤 User | 🆔 {uid} | {flag}")
            else:
                lines.append(f"{i}️⃣ 👤 User | 🆔 {uid}")
        i += 1
    lines.append("")
    lines.append("👇 Click the button below to claim your prize")
    lines.append("")
    lines.append("⏳ Rule: Claim within 24 hours — after that, prize expires.")
    return "\n".join(lines)


# =========================================================
# STOP JOBS
# =========================================================
def stop_live_countdown():
    global countdown_job
    if countdown_job is not None:
        try:
            countdown_job.schedule_removal()
        except Exception:
            pass
    countdown_job = None


def stop_closed_anim():
    global closed_anim_job
    if closed_anim_job is not None:
        try:
            closed_anim_job.schedule_removal()
        except Exception:
            pass
    closed_anim_job = None


def stop_draw_jobs():
    global draw_job, draw_finalize_job
    if draw_job is not None:
        try:
            draw_job.schedule_removal()
        except Exception:
            pass
    draw_job = None
    if draw_finalize_job is not None:
        try:
            draw_finalize_job.schedule_removal()
        except Exception:
            pass
    draw_finalize_job = None


def stop_auto_draw_jobs():
    global auto_draw_job, auto_draw_finalize_job
    if auto_draw_job is not None:
        try:
            auto_draw_job.schedule_removal()
        except Exception:
            pass
    auto_draw_job = None

    if auto_draw_finalize_job is not None:
        try:
            auto_draw_finalize_job.schedule_removal()
        except Exception:
            pass
    auto_draw_finalize_job = None


# =========================================================
# LIVE COUNTDOWN
# =========================================================
def start_live_countdown(job_queue):
    global countdown_job
    stop_live_countdown()
    countdown_job = job_queue.run_repeating(live_tick, interval=5, first=0, name="live_countdown")


def live_tick(context: CallbackContext):
    with lock:
        if not data.get("active"):
            stop_live_countdown()
            return

        start_time = data.get("start_time")
        if start_time is None:
            data["start_time"] = now_ts()
            save_data()
            start_time = data["start_time"]

        start = datetime.utcfromtimestamp(start_time)
        duration = data.get("duration_seconds", 1) or 1
        elapsed = int((datetime.utcnow() - start).total_seconds())
        remaining = duration - elapsed

        live_mid = data.get("live_message_id")

        if remaining <= 0:
            # close giveaway
            data["active"] = False
            data["closed"] = True
            save_data()

            # delete live message
            if live_mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
                except Exception:
                    pass

            # post closed message
            try:
                if data.get("auto_draw"):
                    m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_simple_text())
                    data["closed_message_id"] = m.message_id
                    save_data()

                    # start autodraw pinned selection
                    start_autodraw_channel_progress(context.job_queue, context.bot)
                else:
                    m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text(SPIN[0], 0))
                    data["closed_message_id"] = m.message_id
                    save_data()
                    start_closed_anim(context.job_queue)
            except Exception:
                pass

            # notify admin
            try:
                context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "⏰ Giveaway Closed!\n\n"
                        f"Giveaway: {data.get('title','')}\n"
                        f"Total Participants: {participants_count()}\n\n"
                        "AutoDraw ON → Auto winners post\n"
                        "AutoDraw OFF → use /draw"
                    ),
                )
            except Exception:
                pass

            stop_live_countdown()
            return

        if not live_mid:
            return

        try:
            context.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=live_mid,
                text=build_live_text(remaining),
                reply_markup=join_button_markup(),
            )
        except Exception:
            pass


# =========================================================
# CLOSED ANIM (AutoDraw OFF only)
# =========================================================
def start_closed_anim(job_queue):
    global closed_anim_job
    stop_closed_anim()
    closed_anim_job = job_queue.run_repeating(
        closed_anim_tick,
        interval=CLOSED_TICK_INTERVAL,
        first=0,
        context={"tick": 0},
        name="closed_anim",
    )


def closed_anim_tick(context: CallbackContext):
    if data.get("auto_draw"):
        stop_closed_anim()
        return

    if data.get("pending_winners_gid") or data.get("history"):
        # keep running until winners posted; no special stop here
        pass

    mid = data.get("closed_message_id")
    if not mid:
        stop_closed_anim()
        return

    ctx = context.job.context or {}
    tick = int(ctx.get("tick", 0)) + 1
    ctx["tick"] = tick
    context.job.context = ctx

    spin = SPIN[(tick - 1) % len(SPIN)]
    percent = int((tick * 5) % 101)

    try:
        context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=mid,
            text=build_closed_post_text(spin, percent),
        )
    except Exception:
        pass


# =========================================================
# MANUAL DRAW (Admin progress -> Preview with Approve/Reject)
# =========================================================
def start_draw_progress(context: CallbackContext, admin_chat_id: int):
    global draw_job, draw_finalize_job
    stop_draw_jobs()

    msg = context.bot.send_message(
        chat_id=admin_chat_id,
        text=build_draw_progress_text(0, SPIN[0], "."),
    )

    ctx = {"admin_chat_id": admin_chat_id, "admin_msg_id": msg.message_id, "start_ts": now_ts(), "tick": 0}

    def tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] = int(jd.get("tick", 0)) + 1

        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(DRAW_DURATION_SECONDS)) * 100)))

        spin = SPIN[(jd["tick"] - 1) % len(SPIN)]
        dots = DOTS[(jd["tick"] - 1) % len(DOTS)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["admin_chat_id"],
                message_id=jd["admin_msg_id"],
                text=build_draw_progress_text(percent, spin, dots),
            )
        except Exception:
            pass

    draw_job = context.job_queue.run_repeating(tick, interval=DRAW_UPDATE_INTERVAL, first=0, context=ctx)
    draw_finalize_job = context.job_queue.run_once(draw_finalize, when=DRAW_DURATION_SECONDS, context=ctx)


def select_winners_core():
    participants = data.get("participants", {}) or {}
    if not participants:
        return None

    winner_count = int(data.get("winner_count", 1)) or 1
    winner_count = max(1, winner_count)

    first_uid = data.get("first_winner_id")
    if not first_uid:
        first_uid = next(iter(participants.keys()))
        info = participants.get(first_uid, {}) or {}
        data["first_winner_id"] = first_uid
        data["first_winner_username"] = info.get("username", "")
        data["first_winner_name"] = info.get("name", "")

    first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

    pool = [uid for uid in participants.keys() if uid != first_uid]
    needed = max(0, winner_count - 1)
    needed = min(needed, len(pool))

    selected = random.sample(pool, needed) if needed > 0 else []

    winners_map = {first_uid: {"username": first_uname}}
    others = []
    for uid in selected:
        info = participants.get(uid, {}) or {}
        winners_map[uid] = {"username": info.get("username", "")}
        others.append((uid, info.get("username", "")))

    return first_uid, first_uname, winners_map, others


def draw_finalize(context: CallbackContext):
    stop_draw_jobs()
    jd = context.job.context
    admin_chat_id = jd["admin_chat_id"]
    admin_msg_id = jd["admin_msg_id"]

    with lock:
        sel = select_winners_core()
        if not sel:
            try:
                context.bot.edit_message_text(chat_id=admin_chat_id, message_id=admin_msg_id, text="No participants.")
            except Exception:
                pass
            return

        first_uid, first_uname, winners_map, others = sel
        gid = make_gid()
        delivered = {}

        text = build_winners_post_text(
            gid=gid,
            title=data.get("title", ""),
            prize=data.get("prize", ""),
            first_uid=first_uid,
            first_user=first_uname,
            others=others,
            delivered=delivered,
        )

        data["winners"] = winners_map
        data["pending_winners_text"] = text
        data["pending_winners_gid"] = gid
        save_data()

    # admin preview (approve/reject)
    try:
        context.bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            text=text,
            reply_markup=winners_approve_markup(),
        )
    except Exception:
        context.bot.send_message(chat_id=admin_chat_id, text=text, reply_markup=winners_approve_markup())


# =========================================================
# AUTO DRAW (Pinned selection post 5 min)
# =========================================================
def start_autodraw_channel_progress(job_queue, bot):
    global auto_draw_job, auto_draw_finalize_job
    stop_auto_draw_jobs()

    with lock:
        parts = list((data.get("participants", {}) or {}).items())

    queue = [(uid, (info or {}).get("username", "")) for uid, info in parts]
    if not queue:
        queue = [("0", "@username")]

    state = {
        "start_ts": now_ts(),
        "tick": 0,
        "ptr": 0,
        "line1": queue[0],
        "line2": queue[0],
        "line3": queue[0],
        "c1": random.choice(SHOW_COLORS),
        "c2": random.choice(SHOW_COLORS),
        "c3": random.choice(SHOW_COLORS),
        "t1": now_ts(),
        "t2": now_ts(),
        "t3": now_ts(),
    }

    def next_entry():
        idx = state["ptr"] % len(queue)
        state["ptr"] += 1
        return queue[idx]

    state["line1"] = next_entry()
    state["line2"] = next_entry()
    state["line3"] = next_entry()

    m = bot.send_message(
        chat_id=CHANNEL_ID,
        text=build_autodraw_text(
            0,
            AUTO_DRAW_DURATION_SECONDS,
            SPIN[0],
            format_entry(state["line1"][0], state["line1"][1]),
            state["c1"],
            format_entry(state["line2"][0], state["line2"][1]),
            state["c2"],
            format_entry(state["line3"][0], state["line3"][1]),
            state["c3"],
        ),
    )

    try:
        bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=m.message_id, disable_notification=True)
    except Exception:
        pass

    with lock:
        data["autodraw_message_id"] = m.message_id
        save_data()

    ctx = {"mid": m.message_id}

    def tick(job_ctx: CallbackContext):
        state["tick"] += 1
        elapsed = int(now_ts() - state["start_ts"])
        remaining = max(0, AUTO_DRAW_DURATION_SECONDS - elapsed)
        percent = int(round(min(100, (elapsed / float(AUTO_DRAW_DURATION_SECONDS)) * 100)))
        spin = SPIN[(state["tick"] - 1) % len(SPIN)]

        # line change schedule
        if now_ts() - state["t1"] >= 5:
            state["line1"] = next_entry()
            state["c1"] = random.choice(SHOW_COLORS)
            state["t1"] = now_ts()
        if now_ts() - state["t2"] >= 6:
            state["line2"] = next_entry()
            state["c2"] = random.choice(SHOW_COLORS)
            state["t2"] = now_ts()
        if now_ts() - state["t3"] >= 7:
            state["line3"] = next_entry()
            state["c3"] = random.choice(SHOW_COLORS)
            state["t3"] = now_ts()

        text = build_autodraw_text(
            percent,
            remaining,
            spin,
            format_entry(state["line1"][0], state["line1"][1]),
            state["c1"],
            format_entry(state["line2"][0], state["line2"][1]),
            state["c2"],
            format_entry(state["line3"][0], state["line3"][1]),
            state["c3"],
        )

        try:
            job_ctx.bot.edit_message_text(chat_id=CHANNEL_ID, message_id=ctx["mid"], text=text)
        except Exception:
            pass

    auto_draw_job = job_queue.run_repeating(tick, interval=AUTO_TICK_INTERVAL, first=0, context=ctx)
    auto_draw_finalize_job = job_queue.run_once(autodraw_finalize, when=AUTO_DRAW_DURATION_SECONDS, context=ctx)


def autodraw_finalize(context: CallbackContext):
    stop_auto_draw_jobs()

    with lock:
        sel = select_winners_core()
        if not sel:
            return

        first_uid, first_uname, winners_map, others = sel
        gid = make_gid()
        delivered = {}

        text = build_winners_post_text(
            gid=gid,
            title=data.get("title", ""),
            prize=data.get("prize", ""),
            first_uid=first_uid,
            first_user=first_uname,
            others=others,
            delivered=delivered,
        )

        # build history snapshot
        hist = data.get("history", {}) or {}
        snap = {
            "gid": gid,
            "title": data.get("title", ""),
            "prize": data.get("prize", ""),
            "winners": winners_map,
            "delivered": delivered,
            "created_ts": now_ts(),
            "claim_expires_ts": now_ts() + 24 * 3600,
            "admin_contact": ADMIN_CONTACT,
            "winners_message_id": None,
        }
        hist[gid] = snap
        data["history"] = hist
        save_data()

        closed_mid = data.get("closed_message_id")
        auto_mid = data.get("autodraw_message_id")

    # delete CLOSED + pinned selection
    if closed_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
        except Exception:
            pass
    if auto_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=auto_mid)
        except Exception:
            pass

    with lock:
        data["closed_message_id"] = None
        data["autodraw_message_id"] = None
        save_data()

    # post winners automatically
    try:
        m = context.bot.send_message(chat_id=CHANNEL_ID, text=text, reply_markup=claim_button_markup(gid))
        with lock:
            data["history"][gid]["winners_message_id"] = m.message_id
            save_data()
    except Exception:
        pass


# =========================================================
# COMMANDS
# =========================================================
def cmd_start(update: Update, context: CallbackContext):
    u = update.effective_user
    if u and u.id == ADMIN_ID:
        update.message.reply_text("Admin Panel: /panel")
    else:
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ {HOST_NAME} Giveaway System ⚡\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please join the official channel:\n"
            f"{CHANNEL_LINK}"
        )


def cmd_panel(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    status = "ON ✅" if data.get("auto_draw") else "OFF ❌"
    update.message.reply_text(
        "🛠 ADMIN CONTROL PANEL\n\n"
        "📌 GIVEAWAY\n"
        "/newgiveaway\n"
        "/participants\n"
        "/endgiveaway\n"
        "/draw\n\n"
        "⚙️ AUTO DRAW\n"
        f"/Autodraw   (Current: {status})\n\n"
        "📦 PRIZE DELIVERY\n"
        "/prizeDelivered\n\n"
        "🔒 BLOCK SYSTEM\n"
        "/blockpermanent\n"
        "/unban\n"
        "/blocklist\n"
        "/removeban\n\n"
        "✅ VERIFY SYSTEM\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "♻️ RESET\n"
        "/reset"
    )


def cmd_autodraw(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    status = "ON ✅" if data.get("auto_draw") else "OFF ❌"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 AUTO DRAW SETTINGS\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Current Status: {status}\n\n"
        "Choose option:",
        reply_markup=autodraw_toggle_markup()
    )


def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ ADD VERIFY TARGET\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send Chat ID (recommended) OR @username:\n\n"
        "-1001234567890\n"
        "@PowerPointBreak"
    )


def cmd_removeverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    targets = data.get("verify_targets", []) or []
    if not targets:
        update.message.reply_text("No verify targets are set.")
        return

    lines = ["━━━━━━━━━━━━━━━━━━━━", "🗑 REMOVE VERIFY TARGET", "━━━━━━━━━━━━━━━━━━━━", "", "Current:", ""]
    for i, t in enumerate(targets, start=1):
        lines.append(f"{i}) {t.get('display','')}")
    lines += ["", "Send a number to remove.", "11) Remove ALL"]
    admin_state = "remove_verify_pick"
    update.message.reply_text("\n".join(lines))


def cmd_newgiveaway(update: Update, context: CallbackContext):
    global admin_state, data
    if not is_admin(update):
        return

    stop_live_countdown()
    stop_draw_jobs()
    stop_closed_anim()
    stop_auto_draw_jobs()

    with lock:
        keep_perma = dict(data.get("permanent_block", {}) or {})
        keep_verify = list(data.get("verify_targets", []) or [])
        keep_auto = bool(data.get("auto_draw", False))
        keep_history = dict(data.get("history", {}) or {})

        data.clear()
        data.update(fresh_default_data())

        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
        data["auto_draw"] = keep_auto
        data["history"] = keep_history
        save_data()

    admin_state = "title"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 NEW GIVEAWAY SETUP\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "STEP 1 — Send Giveaway Title:"
    )


def cmd_participants(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    parts = data.get("participants", {})
    if not parts:
        update.message.reply_text("👥 Participants List is empty.")
        return
    lines = ["━━━━━━━━━━━━━━━━━━━━", "👥 PARTICIPANTS LIST", "━━━━━━━━━━━━━━━━━━━━", f"Total: {len(parts)}", ""]
    i = 1
    for uid, info in parts.items():
        uname = (info or {}).get("username", "")
        if uname:
            lines.append(f"{i}. {uname} | User ID: {uid}")
        else:
            lines.append(f"{i}. User ID: {uid}")
        i += 1
    update.message.reply_text("\n".join(lines))


def cmd_endgiveaway(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if not data.get("active"):
        update.message.reply_text("No active giveaway is running right now.")
        return
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ END GIVEAWAY\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Are you sure you want to end now?",
        reply_markup=end_confirm_markup()
    )


def cmd_draw(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if data.get("auto_draw"):
        update.message.reply_text("Auto Draw is ON. Winners will be posted automatically after giveaway ends.")
        return
    if not data.get("closed"):
        update.message.reply_text("Giveaway is not closed yet or no giveaway running.")
        return
    if not data.get("participants", {}):
        update.message.reply_text("No participants to draw winners from.")
        return
    start_draw_progress(context, update.effective_chat.id)


def cmd_blockpermanent(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "perma_block_list"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 PERMANENT BLOCK\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send list (one per line):\n"
        "User ID OR @username | user_id"
    )


def cmd_unban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "unban_choose"
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Unban Permanent Block", callback_data="unban_permanent"),
            InlineKeyboardButton("Unban Old Winner Block", callback_data="unban_oldwinner"),
        ]]
    )
    update.message.reply_text("Choose Unban Type:", reply_markup=kb)


def cmd_removeban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "removeban_choose"
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Reset Permanent Ban List", callback_data="reset_permanent_ban"),
            InlineKeyboardButton("Reset Old Winner Ban List", callback_data="reset_oldwinner_ban"),
        ]]
    )
    update.message.reply_text("Choose which ban list to reset:", reply_markup=kb)


def cmd_blocklist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    perma = data.get("permanent_block", {}) or {}
    oldw = data.get("old_winners", {}) or {}

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📌 BAN LISTS")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"OLD WINNER MODE: {data.get('old_winner_mode','skip').upper()}")
    lines.append("")

    lines.append("⛔ OLD WINNER BLOCK LIST")
    lines.append(f"Total: {len(oldw)}")
    if oldw:
        i = 1
        for uid, info in oldw.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {uname or 'User'} | User ID: {uid}")
            i += 1
    else:
        lines.append("No old winner blocked users.")
    lines.append("")

    lines.append("🔒 PERMANENT BLOCK LIST")
    lines.append(f"Total: {len(perma)}")
    if perma:
        i = 1
        for uid, info in perma.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {uname or 'User'} | User ID: {uid}")
            i += 1
    else:
        lines.append("No permanently blocked users.")

    update.message.reply_text("\n".join(lines))


def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "Confirm reset?",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel"),
            ]]
        )
    )


def cmd_prize_delivered(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "prize_delivered_gid"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📦 PRIZE DELIVERY\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send Giveaway ID first.\n"
        "Example:\n"
        "P788-P686-B6548"
    )


# =========================================================
# ADMIN TEXT FLOW
# =========================================================
def admin_text_handler(update: Update, context: CallbackContext):
    global admin_state, data
    if not is_admin(update):
        return
    if admin_state is None:
        return

    msg = (update.message.text or "").strip()
    if not msg:
        return

    # ADD VERIFY
    if admin_state == "add_verify":
        ref = normalize_verify_ref(msg)
        if not ref:
            update.message.reply_text("Invalid input. Send Chat ID like -100... or @username.")
            return
        with lock:
            targets = data.get("verify_targets", []) or []
            targets.append({"ref": ref, "display": ref})
            data["verify_targets"] = targets
            save_data()
        update.message.reply_text(
            "✅ Verify target added!",
            reply_markup=verify_add_more_done_markup()
        )
        return

    # REMOVE VERIFY PICK
    if admin_state == "remove_verify_pick":
        if not msg.isdigit():
            update.message.reply_text("Send a valid number.")
            return
        n = int(msg)
        with lock:
            targets = data.get("verify_targets", []) or []
            if n == 11:
                data["verify_targets"] = []
                save_data()
                admin_state = None
                update.message.reply_text("✅ All verify targets removed!")
                return
            if n < 1 or n > len(targets):
                update.message.reply_text("Invalid number.")
                return
            targets.pop(n - 1)
            data["verify_targets"] = targets
            save_data()
        admin_state = None
        update.message.reply_text("✅ Removed!")
        return

    # GIVEAWAY SETUP FLOW
    if admin_state == "title":
        with lock:
            data["title"] = msg
            save_data()
        admin_state = "prize"
        update.message.reply_text("✅ Title saved!\n\nNow send Giveaway Prize:")
        return

    if admin_state == "prize":
        with lock:
            data["prize"] = msg
            save_data()
        admin_state = "winners"
        update.message.reply_text("✅ Prize saved!\n\nNow send Total Winner Count:")
        return

    if admin_state == "winners":
        if not msg.isdigit():
            update.message.reply_text("Send a valid number.")
            return
        with lock:
            data["winner_count"] = max(1, min(1000000, int(msg)))
            save_data()
        admin_state = "duration"
        update.message.reply_text("✅ Saved!\n\nSend Duration (e.g. 30 Second / 5 Minute / 1 Hour):")
        return

    if admin_state == "duration":
        seconds = parse_duration(msg)
        if seconds <= 0:
            update.message.reply_text("Invalid duration.")
            return
        with lock:
            data["duration_seconds"] = seconds
            save_data()

        admin_state = "old_winner_mode"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 OLD WINNER MODE\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "1 → BLOCK OLD WINNERS\n"
            "2 → SKIP OLD WINNERS\n\n"
            "Reply 1 or 2:"
        )
        return

    if admin_state == "old_winner_mode":
        if msg not in ("1", "2"):
            update.message.reply_text("Reply with 1 or 2.")
            return
        if msg == "2":
            with lock:
                data["old_winner_mode"] = "skip"
                data["old_winners"] = {}
                save_data()
            admin_state = "rules"
            update.message.reply_text("Send Giveaway Rules (multi-line):")
            return

        with lock:
            data["old_winner_mode"] = "block"
            data["old_winners"] = {}
            save_data()
        admin_state = "old_winner_block_list"
        update.message.reply_text("Send old winners list (one per line): @user | id OR id")
        return

    if admin_state == "old_winner_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list.")
            return
        with lock:
            ow = data.get("old_winners", {}) or {}
            for uid, uname in entries:
                ow[uid] = {"username": uname}
            data["old_winners"] = ow
            save_data()
        admin_state = "rules"
        update.message.reply_text("✅ Old winner list saved!\nNow send Giveaway Rules:")
        return

    if admin_state == "rules":
        with lock:
            data["rules"] = msg
            save_data()
        admin_state = None
        update.message.reply_text("✅ Rules saved!\nShowing preview…")
        update.message.reply_text(build_preview_text(), reply_markup=preview_markup())
        return

    # PERMANENT BLOCK
    if admin_state == "perma_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list.")
            return
        with lock:
            perma = data.get("permanent_block", {}) or {}
            for uid, uname in entries:
                perma[uid] = {"username": uname}
            data["permanent_block"] = perma
            save_data()
        admin_state = None
        update.message.reply_text("✅ Permanent block saved!")
        return

    # PRIZE DELIVERY FLOW
    if admin_state == "prize_delivered_gid":
        gid = msg.strip()
        with lock:
            snap = (data.get("history", {}) or {}).get(gid)
        if not snap:
            update.message.reply_text("Giveaway ID not found.")
            return
        context.user_data["pd_gid"] = gid
        admin_state = "prize_delivered_list"
        update.message.reply_text("Send delivered list now:\n@user | id  OR  id")
        return

    if admin_state == "prize_delivered_list":
        gid = context.user_data.get("pd_gid", "")
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list.")
            return

        with lock:
            hist = data.get("history", {}) or {}
            snap = hist.get(gid)
            if not snap:
                admin_state = None
                update.message.reply_text("Giveaway not found.")
                return

            winners = snap.get("winners", {}) or {}
            delivered = snap.get("delivered", {}) or {}

            for uid, uname in entries:
                if uid in winners:
                    delivered[uid] = True
                    if uname:
                        winners[uid]["username"] = uname

            snap["delivered"] = delivered
            snap["winners"] = winners
            hist[gid] = snap
            data["history"] = hist
            save_data()

        # update winners post message text
        try:
            with lock:
                snap2 = data["history"][gid]
            wmap = snap2.get("winners", {}) or {}
            dmap = snap2.get("delivered", {}) or {}
            wmid = snap2.get("winners_message_id")
            title = snap2.get("title", "")
            prize = snap2.get("prize", "")

            keys = list(wmap.keys())
            first_uid = keys[0] if keys else ""
            first_uname = (wmap.get(first_uid, {}) or {}).get("username", "")

            others = [(u, (wmap.get(u, {}) or {}).get("username", "")) for u in keys[1:]]

            new_text = build_winners_post_text(gid, title, prize, first_uid, first_uname, others, dmap)

            if wmid:
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=wmid,
                    text=new_text,
                    reply_markup=claim_button_markup(gid),
                )
        except Exception:
            pass

        admin_state = None
        update.message.reply_text("✅ Prize delivery updated successfully!")
        return


# =========================================================
# CALLBACK HANDLER
# =========================================================
def cb_handler(update: Update, context: CallbackContext):
    global admin_state
    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)

    # AutoDraw ON/OFF
    if qd in ("autodraw_on", "autodraw_off"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        with lock:
            data["auto_draw"] = (qd == "autodraw_on")
            save_data()
        try:
            query.edit_message_text("✅ Auto Draw is ON." if qd == "autodraw_on" else "✅ Auto Draw is OFF.")
        except Exception:
            pass
        return

    # Verify buttons
    if qd == "verify_add_more":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass
        admin_state = "add_verify"
        try: query.edit_message_text("Send another Chat ID or @username:")
        except Exception: pass
        return

    if qd == "verify_add_done":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass
        admin_state = None
        try: query.edit_message_text("✅ Verify setup completed.")
        except Exception: pass
        return

    # Preview actions
    if qd.startswith("preview_"):
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return

        if qd == "preview_approve":
            try: query.answer()
            except Exception: pass

            try:
                duration = int(data.get("duration_seconds", 0)) or 1
                m = context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=build_live_text(duration),
                    reply_markup=join_button_markup(),
                )

                with lock:
                    data["live_message_id"] = m.message_id
                    data["active"] = True
                    data["closed"] = False
                    data["start_time"] = now_ts()
                    data["closed_message_id"] = None

                    data["participants"] = {}
                    data["winners"] = {}
                    data["pending_winners_text"] = ""
                    data["pending_winners_gid"] = ""

                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""

                    data["autodraw_message_id"] = None
                    save_data()

                stop_closed_anim()
                stop_auto_draw_jobs()
                start_live_countdown(context.job_queue)

                query.edit_message_text("✅ Giveaway approved and posted to channel!")
            except Exception as e:
                query.edit_message_text(f"Failed to post in channel.\nError: {e}")
            return

        if qd == "preview_reject":
            try: query.answer()
            except Exception: pass
            query.edit_message_text("❌ Giveaway rejected.")
            return

        if qd == "preview_edit":
            try: query.answer()
            except Exception: pass
            query.edit_message_text("✏️ Edit Mode: Start again with /newgiveaway")
            return

    # End giveaway
    if qd == "end_confirm":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass

        with lock:
            if not data.get("active"):
                try: query.edit_message_text("No active giveaway.")
                except Exception: pass
                return
            data["active"] = False
            data["closed"] = True
            save_data()

        live_mid = data.get("live_message_id")
        if live_mid:
            try: context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
            except Exception: pass

        try:
            if data.get("auto_draw"):
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_simple_text())
                with lock:
                    data["closed_message_id"] = m.message_id
                    save_data()
                start_autodraw_channel_progress(context.job_queue, context.bot)
            else:
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text(SPIN[0], 0))
                with lock:
                    data["closed_message_id"] = m.message_id
                    save_data()
                start_closed_anim(context.job_queue)
        except Exception:
            pass

        stop_live_countdown()
        try: query.edit_message_text("✅ Giveaway Closed.")
        except Exception: pass
        return

    if qd == "end_cancel":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass
        try: query.edit_message_text("❌ Cancelled.")
        except Exception: pass
        return

    # Reset
    if qd == "reset_confirm":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass

        stop_live_countdown()
        stop_draw_jobs()
        stop_closed_anim()
        stop_auto_draw_jobs()

        with lock:
            keep_perma = dict(data.get("permanent_block", {}) or {})
            keep_verify = list(data.get("verify_targets", []) or [])
            keep_auto = bool(data.get("auto_draw", False))
            keep_history = dict(data.get("history", {}) or {})

            data.clear()
            data.update(fresh_default_data())
            data["permanent_block"] = keep_perma
            data["verify_targets"] = keep_verify
            data["auto_draw"] = keep_auto
            data["history"] = keep_history
            save_data()

        try: query.edit_message_text("✅ Reset completed.")
        except Exception: pass
        return

    if qd == "reset_cancel":
        try:
            query.answer()
            query.edit_message_text("Cancelled.")
        except Exception:
            pass
        return

    # Join giveaway
    if qd == "join_giveaway":
        if not data.get("active"):
            try: query.answer("This giveaway is not active right now.", show_alert=True)
            except Exception: pass
            return

        if not verify_user_join(context.bot, int(uid)):
            try: query.answer(popup_verify_required(), show_alert=True)
            except Exception: pass
            return

        if uid in (data.get("permanent_block", {}) or {}):
            try: query.answer(popup_permanent_blocked(), show_alert=True)
            except Exception: pass
            return

        if data.get("old_winner_mode") == "block":
            if uid in (data.get("old_winners", {}) or {}):
                try: query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception: pass
                return

        with lock:
            first_uid = data.get("first_winner_id")

        if first_uid and uid == str(first_uid):
            tg_user = query.from_user
            uname = user_tag(tg_user.username or "") or data.get("first_winner_username", "") or "@username"
            try: query.answer(popup_first_winner(uname, uid), show_alert=True)
            except Exception: pass
            return

        if uid in (data.get("participants", {}) or {}):
            try: query.answer(popup_already_joined(), show_alert=True)
            except Exception: pass
            return

        tg_user = query.from_user
        uname = user_tag(tg_user.username or "")
        full_name = (tg_user.full_name or "").strip()

        with lock:
            if not data.get("first_winner_id"):
                data["first_winner_id"] = uid
                data["first_winner_username"] = uname
                data["first_winner_name"] = full_name

            data["participants"][uid] = {"username": uname, "name": full_name}
            save_data()

        # update live post
        try:
            live_mid = data.get("live_message_id")
            start_ts = data.get("start_time")
            if live_mid and start_ts:
                start = datetime.utcfromtimestamp(start_ts)
                duration = data.get("duration_seconds", 1) or 1
                elapsed = int((datetime.utcnow() - start).total_seconds())
                remaining = duration - elapsed
                if remaining < 0:
                    remaining = 0
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=live_mid,
                    text=build_live_text(remaining),
                    reply_markup=join_button_markup(),
                )
        except Exception:
            pass

        with lock:
            if data.get("first_winner_id") == uid:
                try: query.answer(popup_first_winner(uname or "@username", uid), show_alert=True)
                except Exception: pass
            else:
                try: query.answer(popup_join_success(uname or "@Username", uid), show_alert=True)
                except Exception: pass
        return

    # Winners approve/reject (Manual mode only)
    if qd == "winners_approve":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass

        with lock:
            gid = (data.get("pending_winners_gid") or "").strip()
            text = (data.get("pending_winners_text") or "").strip()
            winners_map = dict(data.get("winners", {}) or {})
            title = data.get("title", "")
            prize = data.get("prize", "")

        if not gid or not text or not winners_map:
            try: query.edit_message_text("No pending winners.")
            except Exception: pass
            return

        # delete closed post automatically when posting winners
        with lock:
            closed_mid = data.get("closed_message_id")
        if closed_mid:
            try: context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception: pass
            with lock:
                data["closed_message_id"] = None
                save_data()

        # save history snapshot
        with lock:
            hist = data.get("history", {}) or {}
            if gid not in hist:
                hist[gid] = {
                    "gid": gid,
                    "title": title,
                    "prize": prize,
                    "winners": winners_map,
                    "delivered": {},
                    "created_ts": now_ts(),
                    "claim_expires_ts": now_ts() + 24 * 3600,
                    "admin_contact": ADMIN_CONTACT,
                    "winners_message_id": None,
                }
                data["history"] = hist
                save_data()

        # post winners to channel
        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=text, reply_markup=claim_button_markup(gid))
            with lock:
                data["history"][gid]["winners_message_id"] = m.message_id
                data["pending_winners_text"] = ""
                data["pending_winners_gid"] = ""
                save_data()
            query.edit_message_text("✅ Approved! Winners posted to channel.")
        except Exception as e:
            try: query.edit_message_text(f"Failed to post winners: {e}")
            except Exception: pass
        return

    if qd == "winners_reject":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass
        with lock:
            data["pending_winners_text"] = ""
            data["pending_winners_gid"] = ""
            save_data()
        try: query.edit_message_text("❌ Rejected! Winners will NOT be posted.")
        except Exception: pass
        return

    # Claim prize (per giveaway post)
    if qd.startswith("claim_prize|"):
        gid = qd.split("|", 1)[1].strip()

        with lock:
            snap = (data.get("history", {}) or {}).get(gid)

        if not snap:
            try: query.answer(popup_giveaway_completed(), show_alert=True)
            except Exception: pass
            return

        winners = snap.get("winners", {}) or {}
        delivered = snap.get("delivered", {}) or {}
        exp_ts = snap.get("claim_expires_ts")
        now = now_ts()

        # if claim expired:
        if exp_ts and now > float(exp_ts):
            # winner -> expired popup
            if uid in winners:
                try:
                    query.answer(popup_prize_expired(), show_alert=True)
                except Exception:
                    pass
                return
            # non-winner -> completed popup
            try:
                query.answer(popup_giveaway_completed(), show_alert=True)
            except Exception:
                pass
            return

        # within 24h:
        if uid not in winners:
            try: query.answer(popup_claim_not_winner(), show_alert=True)
            except Exception: pass
            return

        # winner but delivered
        if delivered.get(uid):
            uname = winners.get(uid, {}).get("username", "") or "@username"
            try:
                query.answer(popup_prize_already_delivered(uname, uid, snap.get("admin_contact", ADMIN_CONTACT)), show_alert=True)
            except Exception:
                pass
            return

        uname = winners.get(uid, {}).get("username", "") or "@username"
        try:
            query.answer(
                popup_claim_winner(
                    username=uname,
                    uid=uid,
                    title=snap.get("title", ""),
                    prize=snap.get("prize", ""),
                    admin_contact=snap.get("admin_contact", ADMIN_CONTACT),
                ),
                show_alert=True
            )
        except Exception:
            pass
        return

    try:
        query.answer()
    except Exception:
        pass


# =========================================================
# MAIN
# =========================================================
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing in .env")

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # basic
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("panel", cmd_panel))

    # verify
    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    # giveaway
    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))

    # autodraw
    dp.add_handler(CommandHandler("Autodraw", cmd_autodraw))

    # prize delivered
    dp.add_handler(CommandHandler("prizeDelivered", cmd_prize_delivered))

    # bans
    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))

    # reset
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # text + callback
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # resume after restart
    if data.get("active"):
        start_live_countdown(updater.job_queue)

    if data.get("closed") and data.get("closed_message_id") and not data.get("auto_draw"):
        start_closed_anim(updater.job_queue)

    if data.get("auto_draw") and data.get("autodraw_message_id"):
        # best effort: do nothing (job queue won't resume old timers safely)
        pass

    print("Bot is running (PTB v13 non-async) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
