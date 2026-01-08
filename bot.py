# bot.py
# PowerPoint Break Giveaway Bot (PTB v13.x, sync / non-async)
# FULL A→Z Working Version (Fast Popups + 5s Live Post + 2min Draw Progress + Auto Winner Post + Backrules + Manager TXT + Winner History)

import os
import json
import random
import threading
from datetime import datetime, timedelta

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ParseMode,
)
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

# Bangladesh timezone offset (+6)
BD_OFFSET_HOURS = 6

# =========================================================
# THREAD SAFE STORAGE
# =========================================================
lock = threading.RLock()

# =========================================================
# GLOBAL STATE
# =========================================================
data = {}
admin_state = None

# jobs
countdown_job = None
draw_job = None
draw_finalize_job = None
reset_job = None
reset_finalize_job = None

# =========================================================
# DATA / STORAGE
# =========================================================
def fresh_default_data():
    return {
        # giveaway core
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
        "winners_message_id": None,

        # participants
        "participants": {},  # uid(str) -> {"username": "@x" or "", "name": ""}

        # verify targets (max 10)
        "verify_targets": [],  # [{"ref": "-100..." or "@xxx", "display": "..."}]

        # protection lists
        "permanent_block": {},  # uid -> {"username": "@x" or ""}

        # old winner protection mode for current giveaway
        # "block" => blocks using old_winners list
        # "skip"  => no list needed and does NOT block
        "old_winner_mode": "skip",
        "old_winners": {},  # only used if mode="block"

        # manual old-winner block list (works always, even if mode=skip)
        "manual_oldwinner_block": {},  # uid -> {"username": "@x" or ""}

        # first join winner
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # winners current giveaway
        "winners": {},  # uid -> {"username": "@x"}
        "pending_winners_text": "",

        # claim expiry (24h) for current giveaway winners post
        "claim_expires_at": None,  # utc timestamp

        # winner history (all-time)
        "winner_history": [],  # list of dict entries

        # auto winner post mode
        "auto_winner_post": False,  # if True: after close -> channel draw progress -> auto winners post

        # backrules system
        "backrules_mode": "off",  # "on" or "off"
        "backrules_ban": {},  # uid -> {"username": "@x", "banned_at": ts}

        # draw running flag (prevents stuck / multiple)
        "draw_running": False,

        # manager logs (for /manager export)
        "logs": [],  # list of dict: {"ts": utc_ts, "type": "...", "uid": "...", "username": "...", "extra": {...}}
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
    return d


def save_data():
    with lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


data = load_data()

# =========================================================
# HELPERS
# =========================================================
def bd_now_str(utc_ts: float = None) -> str:
    if utc_ts is None:
        utc_ts = datetime.utcnow().timestamp()
    dt = datetime.utcfromtimestamp(utc_ts) + timedelta(hours=BD_OFFSET_HOURS)
    return dt.strftime("%d/%m/%Y %H:%M:%S")


def log_event(ev_type: str, uid: str = "", username: str = "", extra: dict = None):
    with lock:
        data.setdefault("logs", [])
        data["logs"].append({
            "ts": datetime.utcnow().timestamp(),
            "type": ev_type,
            "uid": str(uid) if uid else "",
            "username": username or "",
            "extra": extra or {},
        })
        # keep logs capped
        if len(data["logs"]) > 5000:
            data["logs"] = data["logs"][-5000:]
        save_data()


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
    return len(data.get("participants", {}) or {})


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
            "• Must join our official channel\n"
            "• Only real accounts are allowed\n"
            "• Multiple entries are not permitted\n"
            "• Stay in the channel until results are announced\n"
            "• Admin decision will be final"
        )
    lines = [l.strip() for l in rules.splitlines() if l.strip()]
    return "\n".join("• " + l for l in lines)


def join_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data="join_giveaway")]]
    )


def claim_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data="claim_prize")]]
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


def autowinner_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Auto Post ON", callback_data="autopost_on"),
            InlineKeyboardButton("Auto Post OFF", callback_data="autopost_off"),
        ]]
    )


def backrules_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("ON", callback_data="backrules_on"),
                InlineKeyboardButton("OFF", callback_data="backrules_off"),
            ],
            [
                InlineKeyboardButton("UNBAN", callback_data="backrules_unban"),
                InlineKeyboardButton("BANLIST", callback_data="backrules_banlist"),
            ],
        ]
    )


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


# =========================================================
# POPUP TEXTS (fast + clean)
# =========================================================
def popup_verify_required() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n"
        "To join this giveaway, you must join the required channels/groups first ✅\n"
        "👇 After joining all of them, click JOIN GIVEAWAY again."
    )


def popup_access_locked() -> str:
    # <= 200 characters, admin copy by tap on @username (Telegram limitation: popup can't have copy button)
    return (
        "🚫 ACCESS LOCKED\n\n"
        "You left the required channels/groups.\n"
        "Entry to this giveaway is restricted.\n\n"
        f"Contact Admin:\n{ADMIN_CONTACT}"
    )


def popup_old_winner_blocked() -> str:
    return (
        "🚫You have already won a previous giveaway.\n"
        "To keep the giveaway fair for everyone,\n"
        "repeat winners are restricted from participating.\n"
        "🙏Please wait for the next Giveaway"
    )


def popup_first_winner(username: str, uid: str) -> str:
    return (
        "✨CONGRATULATIONS🌟\n"
        "You joined the giveaway FIRST and secured the🥇1st Winner spot!\n"
        f"👤{username}|🆔{uid}\n"
        "📸Screenshot & post in the group to confirm."
    )


def popup_already_joined() -> str:
    return (
        "🚫 ENTRY Unsuccessful\n"
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


def popup_claim_winner(username: str, uid: str) -> str:
    return (
        "🌟Congratulations✨\n"
        "You’ve won this giveaway.\n"
        f"👤 {username} | 🆔 {uid}\n"
        "📩 Please contact admin to claim your prize:\n"
        f"👉 {ADMIN_CONTACT}"
    )


def popup_claim_expired() -> str:
    return (
        "⌛ PRIZE EXPIRED\n"
        "Your claim time (24 hours) has ended.\n"
        "This prize can’t be claimed now."
    )


def popup_claim_not_winner() -> str:
    return (
        "❌ NOT A WINNER\n\n"
        "Sorry! Your User ID is not in the winners list.\n"
        "Please wait for the next giveaway 🤍"
    )


# =========================================================
# TEXT BUILDERS
# =========================================================
def build_preview_text() -> str:
    remaining = data.get("duration_seconds", 0)
    progress = build_progress(0)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 GIVEAWAY PREVIEW (ADMIN ONLY)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ {data.get('title','')} ⚡\n\n"
        "🎁 Prize:\n"
        f"{data.get('prize','')}\n\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n"
        "👥 Total Participants: 0\n"
        "🎯 Winner Type: Random\n\n"
        f"⏰ Time Remaining: {format_hms(remaining)}\n"
        f"📊 Progress: {progress}\n\n"
        "📜 Rules:\n"
        f"{format_rules()}\n\n"
        f"📢 Hosted By: {HOST_NAME}\n"
        f"🔗 Official Channel: {CHANNEL_USERNAME}\n\n"
        "👇 Please tap the button below to join the giveaway"
    )


def build_live_text(remaining: int) -> str:
    duration = data.get("duration_seconds", 1) or 1
    elapsed = duration - remaining
    elapsed = max(0, min(duration, elapsed))
    percent = int(round((elapsed / float(duration)) * 100))
    progress = build_progress(percent)

    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ {data.get('title','')} ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 Prize:\n"
        f"{data.get('prize','')}\n\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n"
        f"👥 Total Participants: {participants_count()}\n"
        "🎯 Winner Type: Random\n\n"
        f"⏰ Time Remaining: {format_hms(remaining)}\n"
        f"📊 Progress: {progress}\n\n"
        "📜 Rules:\n"
        f"{format_rules()}\n\n"
        f"📢 Hosted By: {HOST_NAME}\n"
        f"🔗 Official Channel: {CHANNEL_USERNAME}\n\n"
        "👇 Please tap the button below to join the giveaway"
    )


def build_closed_post_text() -> str:
    # no live dots here (as you requested when auto on/off)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY OFFICIALLY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        "🏆 Winner selection is in progress.\n"
        "Please wait for the official announcement.\n\n"
        "🙏 Thank you to everyone who participated.\n"
        f"— {HOST_NAME} ⚡"
    )


def build_winners_post_text(first_uid: str, first_user: str, random_winners: list) -> str:
    lines = []
    lines.append("🏆 GIVEAWAY WINNERS ANNOUNCEMENT 🏆")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"⚡ {data.get('title','')} ⚡")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("╔══════════════════════╗")
    lines.append("║ 🥇 1ST WINNER • FIRST JOIN 🥇 ║")
    lines.append("╠══════════════════════╣")
    if first_user:
        lines.append(f"║ 👤 {first_user} | 🆔 {first_uid} ║")
    else:
        lines.append(f"║ 👤 User ID: {first_uid} ║")
    lines.append("╚══════════════════════╝")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("👑 OTHER WINNERS (SELECTED RANDOMLY):")

    i = 1
    for uid, uname in random_winners:
        if uname:
            lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}")
        else:
            lines.append(f"{i}️⃣ 👤 User ID: {uid}")
        i += 1

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🎉 Congratulations to all the winners!")
    lines.append("✅ This giveaway was completed using a")
    lines.append("100% fair & transparent random system.")
    lines.append("")
    lines.append(f"📢 Hosted By: {HOST_NAME}")
    lines.append("👇 Click the button below to claim your prize")
    lines.append("")
    lines.append("⏳ Rule: Claim within 24 hours. After 24 hours, claim expires.")
    return "\n".join(lines)


# =========================================================
# JOB: LIVE COUNTDOWN (channel post) - every 5 seconds
# =========================================================
def start_live_countdown(job_queue):
    global countdown_job
    stop_live_countdown()
    countdown_job = job_queue.run_repeating(live_tick, interval=5, first=0, name="live_countdown")


def stop_live_countdown():
    global countdown_job
    if countdown_job is not None:
        try:
            countdown_job.schedule_removal()
        except Exception:
            pass
    countdown_job = None


def live_tick(context: CallbackContext):
    global data
    with lock:
        if not data.get("active"):
            stop_live_countdown()
            return

        start_time = data.get("start_time")
        if start_time is None:
            data["start_time"] = datetime.utcnow().timestamp()
            save_data()
            start_time = data["start_time"]

        start = datetime.utcfromtimestamp(start_time)
        now = datetime.utcnow()
        duration = data.get("duration_seconds", 1) or 1
        elapsed = int((now - start).total_seconds())
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

            # post closed message and save id
            try:
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text())
                data["closed_message_id"] = m.message_id
                save_data()
            except Exception:
                pass

            log_event("giveaway_closed_auto", extra={"title": data.get("title", ""), "participants": participants_count()})

            # notify admin
            try:
                context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "⏰ Giveaway Closed Automatically!\n\n"
                        f"Giveaway: {data.get('title','')}\n"
                        f"Total Participants: {participants_count()}\n\n"
                        "Now use /draw to select winners."
                    ),
                )
            except Exception:
                pass

            stop_live_countdown()

            # AUTO WINNER POST mode
            if data.get("auto_winner_post", False):
                try:
                    start_draw_progress_channel(context)
                except Exception:
                    pass
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
# DRAW SYSTEM (2 minutes) with progress bar + % + fast spinner (NO countdown numbers)
# =========================================================
DRAW_DURATION_SECONDS = 120
DRAW_UPDATE_INTERVAL = 1  # user wanted 1s

SPINNER = ["🔄", "🔃"]
DOTS7 = ["." , "..", "...", "....", ".....", "......", "......."]

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


def build_draw_progress_text(percent: int, spin: str, dots: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔎 Selecting winners... {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        f"{spin} Winner selection is in progress...\n\n"
        "✅ This draw is 100% fair & random.\n"
        "🔐 User ID based selection only.\n\n"
        f"Please wait{dots}"
    )


def pick_winners_and_build_text() -> str:
    """Pick winners and return winners post text. Also updates data[winners], pending_winners_text, claim_expires_at, winner_history."""
    global data
    participants = data.get("participants", {}) or {}
    if not participants:
        return ""

    winner_count = int(data.get("winner_count", 1)) or 1
    winner_count = max(1, winner_count)

    # ensure first winner exists
    first_uid = data.get("first_winner_id")
    if not first_uid:
        first_uid = next(iter(participants.keys()))
        info = participants.get(first_uid, {}) or {}
        data["first_winner_id"] = first_uid
        data["first_winner_username"] = info.get("username", "")
        data["first_winner_name"] = info.get("name", "")

    first_uid = str(first_uid)
    first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

    pool = [uid for uid in participants.keys() if uid != first_uid]
    remaining_needed = max(0, winner_count - 1)
    if remaining_needed > len(pool):
        remaining_needed = len(pool)
    selected = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

    winners_map = {}
    winners_map[first_uid] = {"username": first_uname}

    random_list = []
    for uid in selected:
        info = participants.get(uid, {}) or {}
        winners_map[str(uid)] = {"username": info.get("username", "")}
        random_list.append((str(uid), info.get("username", "")))

    data["winners"] = winners_map
    post_text = build_winners_post_text(first_uid, first_uname, random_list)
    data["pending_winners_text"] = post_text

    # claim expiry 24h
    data["claim_expires_at"] = (datetime.utcnow() + timedelta(hours=24)).timestamp()

    # winner history auto-save
    win_time = datetime.utcnow().timestamp()
    title = data.get("title", "")
    prize = data.get("prize", "")
    hist = data.get("winner_history", []) or []

    # First winner history
    hist.append({
        "ts": win_time,
        "title": title,
        "prize": prize,
        "uid": first_uid,
        "username": first_uname,
        "type": "FIRST_JOIN",
    })

    # Random winners history
    for uid, uname in random_list:
        hist.append({
            "ts": win_time,
            "title": title,
            "prize": prize,
            "uid": uid,
            "username": uname,
            "type": "RANDOM",
        })

    data["winner_history"] = hist
    log_event("winners_selected", extra={"title": title, "winners": len(winners_map)})

    return post_text


def start_draw_progress_admin(context: CallbackContext, admin_chat_id: int):
    """Manual /draw -> show progress in admin chat -> final admin preview with approve/reject."""
    global data, draw_job, draw_finalize_job

    with lock:
        if data.get("draw_running"):
            context.bot.send_message(chat_id=admin_chat_id, text="⚠️ Draw is already running.")
            return
        data["draw_running"] = True
        save_data()

    stop_draw_jobs()

    msg = context.bot.send_message(chat_id=admin_chat_id, text=build_draw_progress_text(0, SPINNER[0], DOTS7[0]))
    ctx = {"chat_id": admin_chat_id, "msg_id": msg.message_id, "start_ts": datetime.utcnow().timestamp(), "tick": 0, "mode": "admin"}

    def tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] += 1
        elapsed = max(0, int(datetime.utcnow().timestamp() - jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(DRAW_DURATION_SECONDS)) * 100)))
        spin = SPINNER[jd["tick"] % len(SPINNER)]
        dots = DOTS7[jd["tick"] % len(DOTS7)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["chat_id"],
                message_id=jd["msg_id"],
                text=build_draw_progress_text(percent, spin, dots),
            )
        except Exception:
            pass

    draw_job = context.job_queue.run_repeating(tick, interval=DRAW_UPDATE_INTERVAL, first=0, context=ctx, name="draw_admin_progress")
    draw_finalize_job = context.job_queue.run_once(draw_finalize_admin, when=DRAW_DURATION_SECONDS, context=ctx, name="draw_admin_finalize")


def draw_finalize_admin(context: CallbackContext):
    global data
    stop_draw_jobs()

    jd = context.job.context
    chat_id = jd["chat_id"]
    msg_id = jd["msg_id"]

    with lock:
        try:
            post_text = pick_winners_and_build_text()
            save_data()
        except Exception:
            post_text = ""

        data["draw_running"] = False
        save_data()

    if not post_text:
        try:
            context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="No participants to draw winners from.")
        except Exception:
            pass
        return

    # admin preview with approve/reject
    try:
        context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=post_text, reply_markup=winners_approve_markup())
    except Exception:
        context.bot.send_message(chat_id=chat_id, text=post_text, reply_markup=winners_approve_markup())


def start_draw_progress_channel(context: CallbackContext):
    """Auto winner post after close -> pinned progress in channel -> auto winners post -> progress removed."""
    global data, draw_job, draw_finalize_job

    with lock:
        if data.get("draw_running"):
            return
        data["draw_running"] = True
        save_data()

    stop_draw_jobs()

    # channel progress message
    msg = context.bot.send_message(chat_id=CHANNEL_ID, text=build_draw_progress_text(0, SPINNER[0], DOTS7[0]))
    progress_mid = msg.message_id

    # pin it
    try:
        context.bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=progress_mid, disable_notification=True)
    except Exception:
        pass

    ctx = {"chat_id": CHANNEL_ID, "msg_id": progress_mid, "start_ts": datetime.utcnow().timestamp(), "tick": 0, "mode": "channel"}

    def tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] += 1
        elapsed = max(0, int(datetime.utcnow().timestamp() - jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(DRAW_DURATION_SECONDS)) * 100)))
        spin = SPINNER[jd["tick"] % len(SPINNER)]
        dots = DOTS7[jd["tick"] % len(DOTS7)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["chat_id"],
                message_id=jd["msg_id"],
                text=build_draw_progress_text(percent, spin, dots),
            )
        except Exception:
            pass

    draw_job = context.job_queue.run_repeating(tick, interval=DRAW_UPDATE_INTERVAL, first=0, context=ctx, name="draw_channel_progress")
    draw_finalize_job = context.job_queue.run_once(draw_finalize_channel, when=DRAW_DURATION_SECONDS, context=ctx, name="draw_channel_finalize")


def draw_finalize_channel(context: CallbackContext):
    global data
    stop_draw_jobs()

    jd = context.job.context
    progress_mid = jd["msg_id"]

    with lock:
        try:
            post_text = pick_winners_and_build_text()
            save_data()
        except Exception:
            post_text = ""

        data["draw_running"] = False
        save_data()

    # unpin + delete progress message
    try:
        context.bot.unpin_chat_message(chat_id=CHANNEL_ID, message_id=progress_mid)
    except Exception:
        pass
    try:
        context.bot.delete_message(chat_id=CHANNEL_ID, message_id=progress_mid)
    except Exception:
        pass

    if not post_text:
        return

    # remove CLOSED post when posting winners
    closed_mid = data.get("closed_message_id")
    if closed_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
        except Exception:
            pass
        with lock:
            data["closed_message_id"] = None
            save_data()

    # post winners directly (no approve needed in auto mode)
    try:
        m = context.bot.send_message(chat_id=CHANNEL_ID, text=post_text, reply_markup=claim_button_markup())
        with lock:
            data["winners_message_id"] = m.message_id
            save_data()
    except Exception:
        pass


# =========================================================
# RESET (40s progress bar + % , no countdown numbers)
# =========================================================
RESET_SECONDS = 40

def stop_reset_jobs():
    global reset_job, reset_finalize_job
    if reset_job is not None:
        try:
            reset_job.schedule_removal()
        except Exception:
            pass
    reset_job = None
    if reset_finalize_job is not None:
        try:
            reset_finalize_job.schedule_removal()
        except Exception:
            pass
    reset_finalize_job = None


def build_reset_text(percent: int, spin: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ RESET IN PROGRESS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{spin} Resetting... {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        "Please wait......."
    )


def full_reset_everything(context: CallbackContext):
    global data
    stop_live_countdown()
    stop_draw_jobs()
    stop_reset_jobs()

    with lock:
        # delete channel messages if exist
        for mid_key in ["live_message_id", "closed_message_id", "winners_message_id"]:
            mid = data.get(mid_key)
            if mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
                except Exception:
                    pass

        data = fresh_default_data()
        save_data()

    log_event("full_reset_done")


# =========================================================
# ADMIN LIST PARSER
# =========================================================
def parse_user_lines(text: str):
    """
    Accept (recommended):
      @name | 123456789
    Also accept:
      123456789
    Bot always works by user_id only; username is just display.
    """
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


# =========================================================
# COMMANDS
# =========================================================
def cmd_start(update: Update, context: CallbackContext):
    u = update.effective_user
    if u:
        log_event("start", uid=str(u.id), username=user_tag(u.username or ""))

    if u and u.id == ADMIN_ID:
        update.message.reply_text(
            "🛡️👑 WELCOME BACK, ADMIN 👑🛡️\n\n"
            "⚙️ System Status: ONLINE ✅\n"
            "🚀 Giveaway Engine: READY\n"
            "🔐 Security Level: MAXIMUM\n\n"
            "You have full control over:\n"
            "🎁 Giveaway Creation\n"
            "👥 Participant Management\n"
            "🏆 Winner Selection\n"
            "🔒 Block & Verify System\n\n"
            "🧭 Open the Admin Control Panel:\n"
            "/panel\n\n"
            f"⚡ POWERED BY: {HOST_NAME}"
        )
    else:
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ {HOST_NAME} Giveaway System ⚡\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please join our official channel and wait for the giveaway post.\n\n"
            "🔗 Official Channel:\n"
            f"{CHANNEL_LINK}"
        )


def cmd_panel(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "🛠 ADMIN CONTROL PANEL – POWER POINT BREAK\n\n"
        "📌 GIVEAWAY\n"
        "/newgiveaway\n"
        "/participants\n"
        "/draw\n"
        "/endgiveaway\n"
        "/winnerlist\n"
        "/completePrize\n\n"
        "⚡ AUTO\n"
        "/autowinnerpost\n\n"
        "🔒 BLOCK SYSTEM\n"
        "/blockpermanent\n"
        "/blockoldwinner\n"
        "/unban\n"
        "/removeban\n"
        "/blocklist\n\n"
        "✅ VERIFY SYSTEM\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "🧩 BACKRULES\n"
        "/backrules\n\n"
        "📄 MANAGER\n"
        "/manager 07/01/2026\n\n"
        "♻️ RESET\n"
        "/reset"
    )


def cmd_autowinnerpost(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text("Choose Auto Winner Post mode:", reply_markup=autowinner_markup())


def cmd_backrules(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text("Backrules Control Panel:", reply_markup=backrules_markup())


def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ ADD VERIFY (CHAT ID / @USERNAME)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send Chat ID (recommended) OR @username:\n\n"
        "Examples:\n"
        "-1001234567890\n"
        "@PowerPointBreak\n\n"
        "Max verify targets: 10"
    )


def cmd_removeverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    targets = data.get("verify_targets", []) or []
    if not targets:
        update.message.reply_text("No verify targets are set.")
        return

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "🗑 REMOVE VERIFY TARGET",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "Current Verify Targets:",
        "",
    ]
    for i, t in enumerate(targets, start=1):
        lines.append(f"{i}) {t.get('display','')}")
    lines += [
        "",
        "Send a number to remove that target.",
        "11) Remove ALL verify targets",
    ]
    admin_state = "remove_verify_pick"
    update.message.reply_text("\n".join(lines))


def cmd_newgiveaway(update: Update, context: CallbackContext):
    global admin_state, data
    if not is_admin(update):
        return

    with lock:
        # keep permanent + verify + backrules + manual oldwinner list + history/logs
        keep_perma = data.get("permanent_block", {})
        keep_verify = data.get("verify_targets", [])
        keep_manual_old = data.get("manual_oldwinner_block", {})
        keep_backrules = data.get("backrules_mode", "off")
        keep_backban = data.get("backrules_ban", {})
        keep_history = data.get("winner_history", [])
        keep_logs = data.get("logs", [])
        keep_autopost = data.get("auto_winner_post", False)

        data = fresh_default_data()
        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
        data["manual_oldwinner_block"] = keep_manual_old
        data["backrules_mode"] = keep_backrules
        data["backrules_ban"] = keep_backban
        data["winner_history"] = keep_history
        data["logs"] = keep_logs
        data["auto_winner_post"] = keep_autopost
        save_data()

    admin_state = "title"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 NEW GIVEAWAY SETUP STARTED\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "STEP 1️⃣ — GIVEAWAY TITLE\n\n"
        "Send Giveaway Title:"
    )


def cmd_participants(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    parts = data.get("participants", {}) or {}
    if not parts:
        update.message.reply_text("👥 Participants List is empty.")
        return

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "👥 PARTICIPANTS LIST (ADMIN VIEW)",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Total Participants: {len(parts)}",
        "",
    ]
    i = 1
    for uid, info in parts.items():
        uname = (info or {}).get("username", "")
        if uname:
            lines.append(f"{i}. {uname} | User ID: {uid}")
        else:
            lines.append(f"{i}. User ID: {uid}")
        lines.append("")
        i += 1

    update.message.reply_text("\n".join(lines))


def cmd_endgiveaway(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if not data.get("active"):
        update.message.reply_text("No active giveaway is running right now.")
        return

    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ END GIVEAWAY CONFIRMATION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Are you sure you want to end this giveaway now?\n\n"
        "✅ Confirm End → Giveaway will close\n"
        "❌ Cancel → Giveaway will continue",
        reply_markup=end_confirm_markup()
    )


def cmd_draw(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if not data.get("closed"):
        update.message.reply_text("Giveaway is not closed yet or no giveaway running.")
        return
    if not (data.get("participants", {}) or {}):
        update.message.reply_text("No participants to draw winners from.")
        return
    if data.get("auto_winner_post", False):
        update.message.reply_text("Auto Winner Post is ON. Winners will be selected automatically after close.")
        return

    start_draw_progress_admin(context, update.effective_chat.id)


def cmd_winnerlist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    hist = data.get("winner_history", []) or []
    if not hist:
        update.message.reply_text("No winner history yet.")
        return

    # newest first
    hist_sorted = sorted(hist, key=lambda x: x.get("ts", 0), reverse=True)

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆 WINNER HISTORY LIST (ALL WINNERS)")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    idx = 1
    for h in hist_sorted[:300]:  # cap output
        ts = h.get("ts", 0)
        dt = bd_now_str(ts)
        uname = h.get("username", "")
        uid = h.get("uid", "")
        wtype = h.get("type", "")
        title = h.get("title", "")
        prize = h.get("prize", "")

        lines.append(f"{idx}️⃣ 👤 {uname or 'User'} | 🆔 {uid}")
        lines.append(f"📅 Win Time (BD): {dt}")
        lines.append(f"🏅 Win Type: {wtype}")
        lines.append("")
        lines.append(f"🎁 Prize:\n{prize}")
        lines.append("")
        lines.append(f"⚡ Giveaway:\n{title}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")
        idx += 1

    update.message.reply_text("\n".join(lines))


def cmd_complete_prize(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    # Admin preview -> approve/reject (simple)
    winners = data.get("winners", {}) or {}
    if not winners:
        update.message.reply_text("No winners found for current giveaway.")
        return

    # Build compact post (no extra big text)
    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("✅ PRIZE DELIVERY UPDATE")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("All prizes have been delivered successfully ✅")
    lines.append("")
    lines.append(f"🏆 Giveaway: {data.get('title','')}")
    lines.append("🎁 Prize:")
    lines.append(f"{data.get('prize','')}")
    lines.append("")
    lines.append("👑 Winners:")
    for uid, info in winners.items():
        uname = (info or {}).get("username", "")
        if uname:
            lines.append(f"👤 {uname} | 🆔 {uid}")
        else:
            lines.append(f"👤 User ID: {uid}")
    lines.append("")
    lines.append(f"— {HOST_NAME} ⚡")

    text = "\n".join(lines)

    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Post", callback_data="complete_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="complete_reject"),
        ]]
    )
    context.user_data["complete_text"] = text
    update.message.reply_text(text, reply_markup=kb)


def cmd_blockpermanent(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "perma_block_list"
    update.message.reply_text(
        "Send PERMANENT block list (multi-line):\n\n"
        "Format (recommended):\n"
        "@username | user_id\n\n"
        "ID only also works.\n\n"
        "Example:\n"
        "@minexxproo | 7297292\n"
        "7297292"
    )


def cmd_blockoldwinner(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "manual_oldwinner_block_list"
    update.message.reply_text(
        "Send OLD WINNER block list (multi-line):\n\n"
        "Format (recommended):\n"
        "@username | user_id\n\n"
        "ID only also works.\n\n"
        "Example:\n"
        "@minexxproo | 8392828\n"
        "@user2 | 833828292\n"
        "839393"
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
    if not is_admin(update):
        return
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
    oldw_mode_list = data.get("old_winners", {}) or {}
    manual_old = data.get("manual_oldwinner_block", {}) or {}

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📌 BAN LISTS")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"OLD WINNER MODE (CURRENT GIVEAWAY): {data.get('old_winner_mode','skip').upper()}")
    lines.append("")

    lines.append("⛔ OLD WINNER BLOCK LIST (MODE=BLOCK)")
    lines.append(f"Total: {len(oldw_mode_list)}")
    if oldw_mode_list:
        i = 1
        for uid, info in oldw_mode_list.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {(uname or 'User')} | User ID: {uid}")
            i += 1
    else:
        lines.append("No users in this list.")
    lines.append("")

    lines.append("⛔ OLD WINNER BLOCK LIST (MANUAL /blockoldwinner)")
    lines.append(f"Total: {len(manual_old)}")
    if manual_old:
        i = 1
        for uid, info in manual_old.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {(uname or 'User')} | User ID: {uid}")
            i += 1
    else:
        lines.append("No users in this list.")
    lines.append("")

    lines.append("🔒 PERMANENT BLOCK LIST")
    lines.append(f"Total: {len(perma)}")
    if perma:
        i = 1
        for uid, info in perma.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {(uname or 'User')} | User ID: {uid}")
            i += 1
    else:
        lines.append("No users in this list.")

    update.message.reply_text("\n".join(lines))


def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ Reject", callback_data="reset_cancel"),
        ]]
    )
    update.message.reply_text("Reset will remove EVERYTHING.\nProceed?", reply_markup=kb)


def cmd_manager(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    # Optional date filter: /manager 07/01/2026
    arg = " ".join(context.args).strip() if context.args else ""
    target_date = None
    if arg:
        try:
            target_date = datetime.strptime(arg, "%d/%m/%Y").date()
        except Exception:
            update.message.reply_text("Invalid date format.\nUse: /manager 07/01/2026")
            return

    logs = data.get("logs", []) or []
    if not logs:
        update.message.reply_text("No logs yet.")
        return

    lines = []
    lines.append("POWER POINT BREAK - MANAGER REPORT")
    lines.append(f"Generated (BD): {bd_now_str()}")
    if target_date:
        lines.append(f"Filter Date: {target_date.strftime('%d/%m/%Y')}")
    lines.append("")

    for lg in logs:
        ts = lg.get("ts", 0)
        dt_bd = datetime.utcfromtimestamp(ts) + timedelta(hours=BD_OFFSET_HOURS)
        if target_date and dt_bd.date() != target_date:
            continue
        t = lg.get("type", "")
        uid = lg.get("uid", "")
        uname = lg.get("username", "")
        extra = lg.get("extra", {})
        lines.append(f"[{dt_bd.strftime('%d/%m/%Y %H:%M:%S')}] {t} | {uname} | {uid} | {extra}")

    content = "\n".join(lines) if lines else "No data for this date."

    # write txt file
    fname = f"manager_{arg.replace('/','-') if arg else 'all'}.txt"
    path = os.path.join(os.path.dirname(__file__), fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    try:
        update.message.reply_document(document=open(path, "rb"), filename=fname)
    except Exception:
        update.message.reply_text("Failed to send txt file (hosting permission issue).")


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

    # ADD VERIFY (max 10)
    if admin_state == "add_verify":
        ref = normalize_verify_ref(msg)
        if not ref:
            update.message.reply_text("Invalid input.\nSend Chat ID like -100123... or @username.")
            return

        with lock:
            targets = data.get("verify_targets", []) or []
            if len(targets) >= 10:
                update.message.reply_text("❌ Max verify targets reached (10). Remove one first using /removeverifylink.")
                return
            if any((t or {}).get("ref") == ref for t in targets):
                update.message.reply_text("⚠️ This verify target already exists.")
                return

            targets.append({"ref": ref, "display": ref})
            data["verify_targets"] = targets
            save_data()

        log_event("verify_target_added", extra={"target": ref})

        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET ADDED SUCCESSFULLY!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Added: {ref}\n"
            f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}/10\n\n"
            "Add another or Done?",
            reply_markup=verify_add_more_done_markup()
        )
        return

    # REMOVE VERIFY PICK
    if admin_state == "remove_verify_pick":
        if not msg.isdigit():
            update.message.reply_text("Send a valid number (1,2,3... or 11).")
            return
        n = int(msg)
        with lock:
            targets = data.get("verify_targets", []) or []
            if not targets:
                admin_state = None
                update.message.reply_text("No verify targets remain.")
                return

            if n == 11:
                data["verify_targets"] = []
                save_data()
                admin_state = None
                log_event("verify_targets_removed_all")
                update.message.reply_text("✅ All verify targets removed successfully!")
                return

            if n < 1 or n > len(targets):
                update.message.reply_text("Invalid number. Try again.")
                return

            removed = targets.pop(n - 1)
            data["verify_targets"] = targets
            save_data()

        admin_state = None
        log_event("verify_target_removed", extra={"removed": removed.get("display", "")})
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET REMOVED\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Removed: {removed.get('display','')}\n"
            f"Remaining: {len(data.get('verify_targets', []) or [])}/10"
        )
        return

    # GIVEAWAY SETUP FLOW
    if admin_state == "title":
        with lock:
            data["title"] = msg
            save_data()
        admin_state = "prize"
        update.message.reply_text("✅ Title saved!\n\nNow send Giveaway Prize (multi-line allowed):")
        return

    if admin_state == "prize":
        with lock:
            data["prize"] = msg
            save_data()
        admin_state = "winners"
        update.message.reply_text("✅ Prize saved!\n\nNow send Total Winner Count (1 - 1000000):")
        return

    if admin_state == "winners":
        if not msg.isdigit():
            update.message.reply_text("Please send a valid number for winner count.")
            return
        count = max(1, min(1000000, int(msg)))
        with lock:
            data["winner_count"] = count
            save_data()
        admin_state = "duration"
        update.message.reply_text(
            "✅ Winner count saved!\n\n"
            f"🏆 Total Winners: {count}\n\n"
            "⏱ Send Giveaway Duration\n"
            "Example:\n"
            "30 Second\n"
            "30 Minute\n"
            "11 Hour"
        )
        return

    if admin_state == "duration":
        seconds = parse_duration(msg)
        if seconds <= 0:
            update.message.reply_text("Invalid duration. Example: 30 Second / 30 Minute / 11 Hour")
            return

        with lock:
            data["duration_seconds"] = seconds
            save_data()

        admin_state = "old_winner_mode"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 OLD WINNER PROTECTION MODE\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "1️⃣ BLOCK OLD WINNERS\n"
            "• Old winners cannot join this giveaway\n\n"
            "2️⃣ SKIP OLD WINNERS\n"
            "• Old winners can join\n"
            "• Old winners will NOT be blocked\n\n"
            "Reply with:\n"
            "1 → BLOCK\n"
            "2 → SKIP"
        )
        return

    if admin_state == "old_winner_mode":
        if msg not in ("1", "2"):
            update.message.reply_text("Reply with 1 or 2 only.")
            return

        if msg == "2":
            with lock:
                data["old_winner_mode"] = "skip"
                data["old_winners"] = {}
                save_data()

            admin_state = "rules"
            update.message.reply_text("✅ Old Winner Mode set to: SKIP\n\nNow send Giveaway Rules (multi-line):")
            return

        with lock:
            data["old_winner_mode"] = "block"
            data["old_winners"] = {}
            save_data()

        admin_state = "old_winner_block_list"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⛔ OLD WINNER BLOCK LIST SETUP\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Send old winners list (multi-line):\n\n"
            "Format (recommended):\n"
            "@username | user_id\n\n"
            "ID only also works.\n\n"
            "Example:\n"
            "@minexxproo | 728272\n"
            "@user2 | 889900\n"
            "556677"
        )
        return

    if admin_state == "old_winner_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: @username | user_id (recommended) OR user_id")
            return
        with lock:
            ow = data.get("old_winners", {}) or {}
            before = len(ow)
            for uid, uname in entries:
                ow[str(uid)] = {"username": uname}
            data["old_winners"] = ow
            save_data()

        admin_state = "rules"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ OLD WINNER BLOCK LIST SAVED!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📌 Total Added: {len(data['old_winners']) - before}\n"
            "Now send Giveaway Rules (multi-line):"
        )
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
            update.message.reply_text("Send valid list: @username | user_id (recommended) OR user_id")
            return
        with lock:
            perma = data.get("permanent_block", {}) or {}
            before = len(perma)
            for uid, uname in entries:
                perma[str(uid)] = {"username": uname}
            data["permanent_block"] = perma
            save_data()

        admin_state = None
        update.message.reply_text(
            "✅ Permanent block saved!\n"
            f"New Added: {len(data['permanent_block']) - before}\n"
            f"Total Blocked: {len(data['permanent_block'])}"
        )
        return

    # MANUAL OLD WINNER BLOCK
    if admin_state == "manual_oldwinner_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: @username | user_id (recommended) OR user_id")
            return
        with lock:
            mo = data.get("manual_oldwinner_block", {}) or {}
            before = len(mo)
            for uid, uname in entries:
                mo[str(uid)] = {"username": uname}
            data["manual_oldwinner_block"] = mo
            save_data()

        admin_state = None
        update.message.reply_text(
            "✅ Manual Old Winner block saved!\n"
            f"New Added: {len(data['manual_oldwinner_block']) - before}\n"
            f"Total: {len(data['manual_oldwinner_block'])}"
        )
        return

    # BACKRULES UNBAN INPUT
    if admin_state == "backrules_unban_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @username | user_id)")
            return
        uid, _ = entries[0]
        with lock:
            b = data.get("backrules_ban", {}) or {}
            if str(uid) in b:
                del b[str(uid)]
                data["backrules_ban"] = b
                save_data()
                update.message.reply_text("✅ Unbanned successfully (Backrules).")
                log_event("backrules_unban", uid=str(uid))
            else:
                update.message.reply_text("This user is not in Backrules ban list.")
        admin_state = None
        return

    # UNBAN INPUT HANDLERS
    if admin_state == "unban_permanent_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @username | user_id)")
            return
        uid, _ = entries[0]
        with lock:
            perma = data.get("permanent_block", {}) or {}
            if str(uid) in perma:
                del perma[str(uid)]
                data["permanent_block"] = perma
                save_data()
                update.message.reply_text("✅ Unbanned from Permanent Block successfully!")
            else:
                update.message.reply_text("This user id is not in Permanent Block list.")
        admin_state = None
        return

    if admin_state == "unban_oldwinner_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @username | user_id)")
            return
        uid, _ = entries[0]
        with lock:
            ow = data.get("old_winners", {}) or {}
            mo = data.get("manual_oldwinner_block", {}) or {}
            removed = False
            if str(uid) in ow:
                del ow[str(uid)]
                removed = True
            if str(uid) in mo:
                del mo[str(uid)]
                removed = True
            data["old_winners"] = ow
            data["manual_oldwinner_block"] = mo
            save_data()

            update.message.reply_text("✅ Unbanned from Old Winner Block successfully!" if removed else "This user id is not in Old Winner block lists.")
        admin_state = None
        return


# =========================================================
# CALLBACK HANDLER
# =========================================================
def answer_ok(q):
    try:
        q.answer()
    except Exception:
        pass


def cb_handler(update: Update, context: CallbackContext):
    global admin_state, data
    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)
    uname = user_tag(query.from_user.username or "")

    # --- Global restriction popups on ANY user action (as requested) ---
    # For non-admin actions only:
    non_admin_action = not (uid == str(ADMIN_ID) and qd.startswith(("preview_", "winners_", "end_", "reset_", "verify_", "autopost_", "backrules_", "complete_")))
    if non_admin_action:
        # permanent block => popup for any button
        if uid in (data.get("permanent_block", {}) or {}):
            try:
                query.answer(popup_permanent_blocked(), show_alert=True)
            except Exception:
                pass
            return

        # old winner manual block ALWAYS blocks (even if mode skip)
        if uid in (data.get("manual_oldwinner_block", {}) or {}):
            try:
                query.answer(popup_old_winner_blocked(), show_alert=True)
            except Exception:
                pass
            return

        # old winner mode block list blocks only if mode=block and list has entries
        if data.get("old_winner_mode") == "block":
            ow = data.get("old_winners", {}) or {}
            if ow and uid in ow:
                try:
                    query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception:
                    pass
                return

    # Verify buttons
    if qd == "verify_add_more":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        answer_ok(query)
        admin_state = "add_verify"
        try:
            query.edit_message_text("Send another Chat ID or @username:")
        except Exception:
            pass
        return

    if qd == "verify_add_done":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        answer_ok(query)
        admin_state = None
        try:
            query.edit_message_text(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ VERIFY SETUP COMPLETED\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}/10"
            )
        except Exception:
            pass
        return

    # Auto winner post on/off
    if qd == "autopost_on":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        answer_ok(query)
        with lock:
            data["auto_winner_post"] = True
            save_data()
        try:
            query.edit_message_text("✅ Auto Winner Post: ON")
        except Exception:
            pass
        return

    if qd == "autopost_off":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        answer_ok(query)
        with lock:
            data["auto_winner_post"] = False
            save_data()
        try:
            query.edit_message_text("✅ Auto Winner Post: OFF")
        except Exception:
            pass
        return

    # Backrules controls
    if qd == "backrules_on":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        answer_ok(query)
        with lock:
            data["backrules_mode"] = "on"
            save_data()
        try:
            query.edit_message_text("✅ Backrules: ON")
        except Exception:
            pass
        return

    if qd == "backrules_off":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        answer_ok(query)
        with lock:
            data["backrules_mode"] = "off"
            save_data()
        try:
            query.edit_message_text("✅ Backrules: OFF")
        except Exception:
            pass
        return

    if qd == "backrules_unban":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        answer_ok(query)
        admin_state = "backrules_unban_input"
        try:
            query.edit_message_text(
                "Send UNBAN list (multi-line):\n"
                "Format (recommended): @username | user_id\n"
                "ID only also works."
            )
        except Exception:
            pass
        return

    if qd == "backrules_banlist":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        answer_ok(query)
        b = data.get("backrules_ban", {}) or {}
        if not b:
            try:
                query.edit_message_text("No Backrules banned users.")
            except Exception:
                pass
            return
        lines = []
        lines.append("BACKRULES BAN LIST")
        lines.append("")
        i = 1
        for bid, info in b.items():
            u = (info or {}).get("username", "")
            lines.append(f"{i}) {(u or 'User')} | User ID: {bid}")
            i += 1
        try:
            query.edit_message_text("\n".join(lines))
        except Exception:
            pass
        return

    # Preview actions
    if qd.startswith("preview_"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return

        if qd == "preview_approve":
            answer_ok(query)
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
                    data["start_time"] = datetime.utcnow().timestamp()
                    data["closed_message_id"] = None
                    data["winners_message_id"] = None
                    data["participants"] = {}
                    data["winners"] = {}
                    data["pending_winners_text"] = ""
                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""
                    data["claim_expires_at"] = None
                    save_data()

                log_event("giveaway_posted", extra={"title": data.get("title", "")})
                start_live_countdown(context.job_queue)

                query.edit_message_text("✅ Giveaway approved and posted to channel!")
            except Exception as e:
                try:
                    query.edit_message_text(f"Failed to post in channel. Make sure bot is admin.\nError: {e}")
                except Exception:
                    pass
            return

        if qd == "preview_reject":
            answer_ok(query)
            try:
                query.edit_message_text("❌ Giveaway rejected and cleared.")
            except Exception:
                pass
            return

        if qd == "preview_edit":
            answer_ok(query)
            try:
                query.edit_message_text("✏️ Edit Mode\n\nStart again with /newgiveaway")
            except Exception:
                pass
            return

    # End giveaway confirm/cancel
    if qd == "end_confirm":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        answer_ok(query)

        with lock:
            if not data.get("active"):
                try:
                    query.edit_message_text("No active giveaway is running right now.")
                except Exception:
                    pass
                return
            data["active"] = False
            data["closed"] = True
            save_data()

        # delete live post
        live_mid = data.get("live_message_id")
        if live_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
            except Exception:
                pass

        # post closed
        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text())
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
        except Exception:
            pass

        stop_live_countdown()
        log_event("giveaway_closed_manual", extra={"title": data.get("title", "")})

        try:
            query.edit_message_text("✅ Giveaway Closed Successfully! Now use /draw")
        except Exception:
            pass

        # if auto on, start channel draw
        if data.get("auto_winner_post", False):
            try:
                start_draw_progress_channel(context)
            except Exception:
                pass
        return

    if qd == "end_cancel":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        answer_ok(query)
        try:
            query.edit_message_text("❌ Cancelled. Giveaway is still running.")
        except Exception:
            pass
        return

    # Reset confirm/cancel
    if qd == "reset_confirm":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        answer_ok(query)

        # start reset progress 40s
        stop_reset_jobs()
        msg = query.message
        start_ts = datetime.utcnow().timestamp()
        ctx = {"chat_id": msg.chat_id, "msg_id": msg.message_id, "start_ts": start_ts, "tick": 0}

        def tick(job_ctx: CallbackContext):
            jd = job_ctx.job.context
            jd["tick"] += 1
            elapsed = max(0, int(datetime.utcnow().timestamp() - jd["start_ts"]))
            percent = int(round(min(100, (elapsed / float(RESET_SECONDS)) * 100)))
            spin = SPINNER[jd["tick"] % len(SPINNER)]
            try:
                job_ctx.bot.edit_message_text(
                    chat_id=jd["chat_id"],
                    message_id=jd["msg_id"],
                    text=build_reset_text(percent, spin),
                )
            except Exception:
                pass

        reset_job = context.job_queue.run_repeating(tick, interval=1, first=0, context=ctx, name="reset_progress")
        reset_finalize_job = context.job_queue.run_once(reset_finalize, when=RESET_SECONDS, context=ctx, name="reset_finalize")
        return

    if qd == "reset_cancel":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        answer_ok(query)
        try:
            query.edit_message_text("❌ Reset rejected.")
        except Exception:
            pass
        return

    # Unban choose
    if qd == "unban_permanent":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        answer_ok(query)
        admin_state = "unban_permanent_input"
        try:
            query.edit_message_text("Send User ID (or @username | user_id) to unban from Permanent Block:")
        except Exception:
            pass
        return

    if qd == "unban_oldwinner":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        answer_ok(query)
        admin_state = "unban_oldwinner_input"
        try:
            query.edit_message_text("Send User ID (or @username | user_id) to unban from Old Winner Block:")
        except Exception:
            pass
        return

    # removeban choose confirm
    if qd == "reset_permanent_ban":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        answer_ok(query)
        with lock:
            data["permanent_block"] = {}
            save_data()
        try:
            query.edit_message_text("✅ Permanent Ban List has been reset.")
        except Exception:
            pass
        return

    if qd == "reset_oldwinner_ban":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        answer_ok(query)
        with lock:
            data["old_winners"] = {}
            data["manual_oldwinner_block"] = {}
            save_data()
        try:
            query.edit_message_text("✅ Old Winner Ban Lists have been reset.")
        except Exception:
            pass
        return

    # Join giveaway
    if qd == "join_giveaway":
        if not data.get("active"):
            try:
                query.answer("This giveaway is not active right now.", show_alert=True)
            except Exception:
                pass
            return

        # Backrules restriction check (user locked)
        if data.get("backrules_mode") == "on":
            br = data.get("backrules_ban", {}) or {}
            if uid in br:
                try:
                    query.answer(popup_access_locked(), show_alert=True)
                except Exception:
                    pass
                return

        # verify required
        ok_verify = verify_user_join(context.bot, int(uid))
        if not ok_verify:
            # backrules ON -> if user fails verify now, mark as banned (locked) AFTER they left previously.
            # (We cannot detect "left" instantly in channels reliably; so when they fail verify at join, we lock them if mode ON.)
            if data.get("backrules_mode") == "on":
                with lock:
                    br = data.get("backrules_ban", {}) or {}
                    br[uid] = {"username": uname, "banned_at": datetime.utcnow().timestamp()}
                    data["backrules_ban"] = br
                    save_data()
                log_event("backrules_ban", uid=uid, username=uname, extra={"reason": "verify_failed_on_join"})
                try:
                    query.answer(popup_access_locked(), show_alert=True)
                except Exception:
                    pass
            else:
                try:
                    query.answer(popup_verify_required(), show_alert=True)
                except Exception:
                    pass
            return

        # FIRST WINNER repeat click -> same popup always
        first_uid = str(data.get("first_winner_id") or "")
        if first_uid and uid == first_uid:
            try:
                query.answer(popup_first_winner(uname or "@username", uid), show_alert=True)
            except Exception:
                pass
            return

        # already joined (normal user)
        if uid in (data.get("participants", {}) or {}):
            try:
                query.answer(popup_already_joined(), show_alert=True)
            except Exception:
                pass
            return

        # success join
        full_name = (query.from_user.full_name or "").strip()

        with lock:
            # If this is the FIRST participant, make them 1st winner
            if not data.get("first_winner_id"):
                data["first_winner_id"] = uid
                data["first_winner_username"] = uname
                data["first_winner_name"] = full_name

            data["participants"][uid] = {"username": uname, "name": full_name}
            save_data()

        log_event("joined", uid=uid, username=uname)

        # update live post immediately (fast participant count update)
        try:
            live_mid = data.get("live_message_id")
            start_ts = data.get("start_time")
            if live_mid and start_ts:
                start = datetime.utcfromtimestamp(start_ts)
                now = datetime.utcnow()
                duration = data.get("duration_seconds", 1) or 1
                elapsed = int((now - start).total_seconds())
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

        # popup: first winner or normal success
        if str(data.get("first_winner_id")) == uid:
            try:
                query.answer(popup_first_winner(uname or "@username", uid), show_alert=True)
            except Exception:
                pass
        else:
            try:
                query.answer(popup_join_success(uname or "@Username", uid), show_alert=True)
            except Exception:
                pass
        return

    # Winners Approve/Reject
    if qd == "winners_approve":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        answer_ok(query)

        text = (data.get("pending_winners_text") or "").strip()
        if not text:
            try:
                query.edit_message_text("No pending winners preview found.")
            except Exception:
                pass
            return

        # delete CLOSED post before posting winners
        closed_mid = data.get("closed_message_id")
        if closed_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception:
                pass
            with lock:
                data["closed_message_id"] = None
                save_data()

        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=text, reply_markup=claim_button_markup())
            with lock:
                data["winners_message_id"] = m.message_id
                save_data()
            # claim expiry already set
            query.edit_message_text("✅ Approved! Winners list posted to channel (with Claim button).")
        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post winners in channel: {e}")
            except Exception:
                pass
        return

    if qd == "winners_reject":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        answer_ok(query)
        with lock:
            data["pending_winners_text"] = ""
            save_data()
        try:
            query.edit_message_text("❌ Rejected! Winners list will NOT be posted.")
        except Exception:
            pass
        return

    # Complete prize approve/reject
    if qd == "complete_approve":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        answer_ok(query)
        text = context.user_data.get("complete_text", "")
        if not text:
            try:
                query.edit_message_text("No delivery text found.")
            except Exception:
                pass
            return
        try:
            context.bot.send_message(chat_id=CHANNEL_ID, text=text)
            query.edit_message_text("✅ Posted Prize Delivery Update to channel.")
            log_event("prize_delivery_posted", extra={"title": data.get("title", "")})
        except Exception as e:
            try:
                query.edit_message_text(f"Failed: {e}")
            except Exception:
                pass
        return

    if qd == "complete_reject":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        answer_ok(query)
        try:
            query.edit_message_text("❌ Rejected.")
        except Exception:
            pass
        return

    # Claim prize
    if qd == "claim_prize":
        winners = data.get("winners", {}) or {}
        expires = data.get("claim_expires_at")

        # if user is winner but expired -> show expired popup
        if uid in winners:
            if expires and datetime.utcnow().timestamp() > float(expires):
                try:
                    query.answer(popup_claim_expired(), show_alert=True)
                except Exception:
                    pass
                return

            win_uname = winners.get(uid, {}).get("username", "") or uname or "@username"
            try:
                query.answer(popup_claim_winner(win_uname, uid), show_alert=True)
            except Exception:
                pass
            return

        # not winner
        try:
            query.answer(popup_claim_not_winner(), show_alert=True)
        except Exception:
            pass
        return

    # default
    answer_ok(query)


def reset_finalize(context: CallbackContext):
    stop_reset_jobs()
    full_reset_everything(context)
    jd = context.job.context
    try:
        context.bot.edit_message_text(chat_id=jd["chat_id"], message_id=jd["msg_id"], text="✅ RESET COMPLETED SUCCESSFULLY!")
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

    # winners & delivery
    dp.add_handler(CommandHandler("winnerlist", cmd_winnerlist))
    dp.add_handler(CommandHandler("completePrize", cmd_complete_prize))

    # auto
    dp.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))

    # bans
    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("blockoldwinner", cmd_blockoldwinner))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))

    # backrules
    dp.add_handler(CommandHandler("backrules", cmd_backrules))

    # manager
    dp.add_handler(CommandHandler("manager", cmd_manager))

    # reset
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # handlers
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # resume live giveaway if bot restarted
    if data.get("active"):
        start_live_countdown(updater.job_queue)

    print("Bot is running (PTB 13, sync) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
