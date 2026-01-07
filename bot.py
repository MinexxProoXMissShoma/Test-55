import os
import json
import random
import threading
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "6"))
BD_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))

# =========================================================
# THREAD SAFE
# =========================================================
lock = threading.RLock()

# =========================================================
# GLOBAL STATE
# =========================================================
data = {}
admin_state = None

live_job = None

draw_job = None
draw_finalize_job = None

reset_job = None
reset_finalize_job = None


# =========================================================
# DEFAULT DATA
# =========================================================
def fresh_default_data():
    return {
        # giveaway status
        "active": False,
        "closed": False,

        # giveaway info
        "title": "",
        "prize": "",
        "winner_count": 1,
        "duration_seconds": 0,
        "rules": "",

        # timers
        "start_time_utc": None,

        # channel posts (message ids)
        "live_message_id": None,
        "closed_message_id": None,
        "draw_message_id": None,
        "winners_message_id": None,

        # participants uid(str)-> {username:"@x", name:""}
        "participants": {},

        # 1st winner
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # winners map uid->{"username":"@x"}
        "winners": {},
        "pending_winners_text": "",
        "claim_deadline_utc": None,  # unix timestamp

        # verify
        "verify_targets": [],  # [{"ref": "-100..." or "@xxx", "display":"..."}]

        # permanent block uid -> {"username": "@x" or ""}
        "permanent_block": {},

        # old winner protection
        # "skip": no list required, no block
        # "block": block those in old_winners list
        "old_winner_mode": "skip",
        "old_winners": {},  # uid -> {"username": "@x" or ""}

        # backrules system
        "backrules_on": False,
        "backrules_ban": {},  # uid -> {"username": "@x", "banned_at": "...", "missing_ref": "..."}

        # autowinnerpost
        "auto_winner_on": False,

        # winner history list
        # each: {"uid": "...", "username": "@x", "title": "...", "prize": "...", "type": "...", "time_bd": "..."}
        "winner_history": [],

        # logs list
        # each: {"time_bd":"", "type":"", "uid":"", "username":"", "details":""}
        "logs": [],
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
            json.dump(data, f, indent=4, ensure_ascii=False)


data = load_data()


# =========================================================
# TIME / LOG HELPERS
# =========================================================
def now_bd() -> datetime:
    return datetime.now(tz=BD_TZ)


def now_utc_ts() -> float:
    return datetime.utcnow().timestamp()


def fmt_bd(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y %H:%M:%S")


def add_log(event_type: str, uid: str = "", username: str = "", details: str = ""):
    with lock:
        logs = data.get("logs", [])
        logs.append({
            "time_bd": fmt_bd(now_bd()),
            "type": event_type,
            "uid": str(uid) if uid else "",
            "username": username or "",
            "details": details or "",
        })
        # keep last 5000 logs
        if len(logs) > 5000:
            logs = logs[-5000:]
        data["logs"] = logs
        save_data()


# =========================================================
# BASIC HELPERS
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
    return len(data.get("participants", {}) or {})


def format_hms(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_progress(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    blocks = 10
    filled = int(round(blocks * percent / 100.0))
    empty = blocks - filled
    return "▰" * filled + "▱" * empty


def parse_duration(text: str) -> int:
    """
    Accept:
    30 Second
    30 sec
    30 Minute
    11 Hour
    3600
    """
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


# =========================================================
# MARKUPS
# =========================================================
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


def reset_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel"),
        ]]
    )


def autowinner_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Auto Post ON", callback_data="autowin_on"),
            InlineKeyboardButton("Auto Post OFF", callback_data="autowin_off"),
        ]]
    )


def backrules_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("ON", callback_data="backrules_on"),
            InlineKeyboardButton("OFF", callback_data="backrules_off"),
        ],
         [
            InlineKeyboardButton("UNBAN", callback_data="backrules_unban"),
            InlineKeyboardButton("BANLIST", callback_data="backrules_banlist"),
         ]]
    )


def unban_choose_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Unban Permanent Block", callback_data="unban_permanent"),
            InlineKeyboardButton("Unban Old Winner Block", callback_data="unban_oldwinner"),
        ]]
    )


def removeban_choose_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Reset Permanent Ban List", callback_data="reset_permanent_ban"),
            InlineKeyboardButton("Reset Old Winner Ban List", callback_data="reset_oldwinner_ban"),
        ]]
    )


def removeban_confirm_markup(kind: str):
    if kind == "permanent":
        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Confirm Reset Permanent", callback_data="confirm_reset_permanent"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
            ]]
        )
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm Reset Old Winner", callback_data="confirm_reset_oldwinner"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
        ]]
    )


# =========================================================
# VERIFY SYSTEM
# =========================================================
def normalize_verify_ref(text: str) -> str:
    """
    Accept:
    -1001234567890
    @ChannelName
    https://t.me/ChannelName
    t.me/ChannelName
    """
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


def verify_user_join(bot, user_id: int):
    """
    Return (ok, missing_ref)
    """
    targets = data.get("verify_targets", []) or []
    if not targets:
        return True, ""

    for t in targets:
        ref = (t or {}).get("ref", "")
        if not ref:
            return False, "UNKNOWN"
        try:
            member = bot.get_chat_member(chat_id=ref, user_id=user_id)
            status = getattr(member, "status", None)
            if status not in ("member", "administrator", "creator"):
                return False, ref
        except Exception:
            return False, ref

    return True, ""


# =========================================================
# POPUPS (ONLY SPACING, NO EXTRA EMOJI/WORDS)
# =========================================================
def popup_verify_required() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n"
        "To join this giveaway, you must join the required channels/groups first ✅\n"
        "👇 After joining all of them, click JOIN GIVEAWAY again."
    )


def popup_old_winner_blocked() -> str:
    return (
        "🚫You have already won a previous giveaway.\n"
        "To keep the giveaway fair for everyone,\n"
        "repeat winners are restricted from participating.\n"
        "🙏Please wait for the next Giveaway"
    )


def popup_permanent_blocked() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n"
        "You are permanently blocked from joining giveaways.\n"
        f"If you believe this is a mistake, contact admin: {ADMIN_CONTACT}"
    )


def popup_backrules_locked() -> str:
    # <=200 chars, admin username easy to copy
    return (
        "🚫 ACCESS LOCKED\n"
        "You left the required channels/groups.\n"
        "Entry is restricted.\n"
        f"Contact Admin: {ADMIN_CONTACT}"
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


def popup_claim_winner(username: str, uid: str) -> str:
    # short winner popup
    return (
        "🌟Congratulations✨\n"
        "You’ve won this giveaway.\n"
        f"👤 {username} | 🆔 {uid}\n"
        "📩   please  Contract admin Claim your prize now:\n"
        f"👉 {ADMIN_CONTACT}"
    )


def popup_claim_expired() -> str:
    return (
        "⛔ PRIZE EXPIRED\n"
        "Your 24-hour claim time is over.\n"
        "This prize can’t be claimed now."
    )


def popup_claim_not_winner() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "❌ NOT A WINNER\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Sorry! Your User ID is not in the winners list.\n"
        "Please wait for the next giveaway 🤍"
    )


# =========================================================
# TEXT BUILDERS
# =========================================================
def build_start_user_text() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ {HOST_NAME} Giveaway System ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Please join our official channel and wait for the giveaway post.\n\n"
        "🔗 Official Channel:\n"
        f"{CHANNEL_LINK}"
    )


def build_preview_text() -> str:
    remaining = int(data.get("duration_seconds", 0) or 0)
    progress = build_progress(0)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 GIVEAWAY PREVIEW (ADMIN ONLY)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ {data.get('title','')} ⚡\n\n"
        "🎁 Prize:\n"
        f"{data.get('prize','')}\n\n"
        f"🏆 Total Winners: {int(data.get('winner_count', 1) or 1)}\n"
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
    duration = int(data.get("duration_seconds", 1) or 1)
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
        f"🏆 Total Winners: {int(data.get('winner_count', 1) or 1)}\n"
        f"👥 Total Participants: {participants_count()}\n"
        "🎯 Winner Type: Random\n\n"
        f"⏰ Time Remaining: {format_hms(int(remaining))}\n"
        f"📊 Progress: {progress}\n\n"
        "📜 Rules:\n"
        f"{format_rules()}\n\n"
        f"📢 Hosted By: {HOST_NAME}\n"
        f"🔗 Official Channel: {CHANNEL_USERNAME}\n\n"
        "👇 Please tap the button below to join the giveaway"
    )


def build_closed_post_text() -> str:
    # your final style: only 2 lines top/bottom border
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY OFFICIALLY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {int(data.get('winner_count', 1) or 1)}\n\n"
        "🎯 Winner selection is currently in progress.\n"
        "Please wait for the official announcement.\n\n"
        "🙏 Thank you to everyone who participated.\n"
        f"— {HOST_NAME} ⚡"
    )


def build_winners_text(first_uid: str, first_user: str, other_winners: list) -> str:
    # other_winners = [(uid, uname), ...]
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
    for uid, uname in other_winners:
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
    lines.append("👇 Click the button below to claim your prize:")

    return "\n".join(lines)


# =========================================================
# DRAW PROGRESS (2 MIN, 1s animation, NO countdown number)
# =========================================================
DRAW_DURATION = 120
DRAW_INTERVAL = 1  # 1s updates (fast)
DOTS = [".", "..", "...", "....", ".....", "......", "......."]
SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def stop_draw():
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


def build_draw_progress_text(percent: int, dots: str, spin: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔎 Selecting winners... {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        f"{spin} Winner selection is in progress{dots}\n\n"
        "✅ This draw is 100% fair & random.\n"
        "🔐 User ID based selection only.\n\n"
        f"Please wait{dots}"
    )


def start_draw_progress(context: CallbackContext, chat_id: int, pin_in_channel: bool = False):
    """
    Starts draw progress in given chat_id.
    If pin_in_channel=True => pin the progress message (best for channel).
    """
    global draw_job, draw_finalize_job
    stop_draw()

    msg = context.bot.send_message(chat_id=chat_id, text=build_draw_progress_text(0, ".", SPINNER[0]))
    if pin_in_channel:
        try:
            context.bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id, disable_notification=True)
        except Exception:
            pass

    with lock:
        data["draw_message_id"] = msg.message_id
        save_data()

    ctx = {
        "chat_id": chat_id,
        "msg_id": msg.message_id,
        "start_ts": now_utc_ts(),
        "tick": 0,
        "pin": pin_in_channel,
    }

    def tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] = jd.get("tick", 0) + 1

        elapsed = max(0, now_utc_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(DRAW_DURATION)) * 100)))
        dots = DOTS[(jd["tick"] - 1) % len(DOTS)]
        spin = SPINNER[(jd["tick"] - 1) % len(SPINNER)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["chat_id"],
                message_id=jd["msg_id"],
                text=build_draw_progress_text(percent, dots, spin),
            )
        except Exception:
            # if flood/warn, skip this tick
            pass

    draw_job = context.job_queue.run_repeating(
        tick,
        interval=DRAW_INTERVAL,
        first=0,
        context=ctx,
        name="draw_progress_job",
    )

    draw_finalize_job = context.job_queue.run_once(
        draw_finalize,
        when=DRAW_DURATION,
        context=ctx,
        name="draw_finalize_job",
    )


def finalize_winners_selection():
    """
    Returns (pending_text, winners_map, first_uid, first_uname)
    """
    participants = data.get("participants", {}) or {}
    if not participants:
        return "", {}, "", ""

    winner_count = int(data.get("winner_count", 1) or 1)
    winner_count = max(1, winner_count)

    # ensure first winner exists
    first_uid = data.get("first_winner_id")
    if not first_uid:
        first_uid = next(iter(participants.keys()))
        info = participants.get(first_uid, {}) or {}
        data["first_winner_id"] = first_uid
        data["first_winner_username"] = info.get("username", "")
        data["first_winner_name"] = info.get("name", "")

    first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

    pool = [uid for uid in participants.keys() if uid != first_uid]
    need = max(0, winner_count - 1)
    if need > len(pool):
        need = len(pool)

    selected = random.sample(pool, need) if need > 0 else []

    winners_map = {}
    winners_map[str(first_uid)] = {"username": first_uname}

    other_list = []
    for uid in selected:
        info = participants.get(uid, {}) or {}
        winners_map[str(uid)] = {"username": info.get("username", "")}
        other_list.append((str(uid), info.get("username", "")))

    pending_text = build_winners_text(str(first_uid), first_uname, other_list)
    return pending_text, winners_map, str(first_uid), first_uname


def add_winner_history(winners_map: dict, first_uid: str):
    """
    Save winners to history automatically when posted (approve/auto).
    """
    title = data.get("title", "")
    prize = data.get("prize", "")
    ts_bd = fmt_bd(now_bd())

    hist = data.get("winner_history", []) or []
    for uid, info in winners_map.items():
        uname = (info or {}).get("username", "") or ""
        wtype = "🥇 1st Winner (First Join)" if str(uid) == str(first_uid) else "👑 Random Winner"
        hist.append({
            "uid": str(uid),
            "username": uname,
            "title": title,
            "prize": prize,
            "type": wtype,
            "time_bd": ts_bd,
        })

    # keep last 2000 winners
    if len(hist) > 2000:
        hist = hist[-2000:]

    data["winner_history"] = hist


def draw_finalize(context: CallbackContext):
    global data
    stop_draw()

    jd = context.job.context
    chat_id = jd["chat_id"]
    msg_id = jd["msg_id"]

    with lock:
        pending_text, winners_map, first_uid, first_uname = finalize_winners_selection()
        if not pending_text:
            try:
                context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="No participants to draw winners from.")
            except Exception:
                pass
            return

        data["winners"] = winners_map
        data["pending_winners_text"] = pending_text

        # claim expires in 24h
        data["claim_deadline_utc"] = now_utc_ts() + (24 * 3600)

        save_data()

    # If draw was started in channel by autowinner => directly post winners in channel
    if jd.get("pin") is True and chat_id == CHANNEL_ID and data.get("auto_winner_on"):
        # delete CLOSED post first
        closed_mid = data.get("closed_message_id")
        if closed_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception:
                pass

        # delete draw progress message (unpin also)
        try:
            try:
                context.bot.unpin_chat_message(chat_id=CHANNEL_ID, message_id=msg_id)
            except Exception:
                pass
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=msg_id)
        except Exception:
            pass

        # post winners to channel
        try:
            m = context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=pending_text,
                reply_markup=claim_button_markup(),
            )
            with lock:
                data["winners_message_id"] = m.message_id
                data["draw_message_id"] = None
                data["closed_message_id"] = None

                add_winner_history(winners_map, first_uid)
                save_data()

            # notify admin
            try:
                context.bot.send_message(chat_id=ADMIN_ID, text="✅ Auto Winner Post completed successfully in channel.")
            except Exception:
                pass

        except Exception as e:
            try:
                context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Auto Winner Post failed: {e}")
            except Exception:
                pass
        return

    # Normal mode => admin preview with Approve/Reject
    try:
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=pending_text,
            reply_markup=winners_approve_markup(),
        )
    except Exception:
        context.bot.send_message(chat_id=chat_id, text=pending_text, reply_markup=winners_approve_markup())


# =========================================================
# LIVE POST UPDATER (Every 5 seconds)
# =========================================================
def stop_live():
    global live_job
    if live_job is not None:
        try:
            live_job.schedule_removal()
        except Exception:
            pass
    live_job = None


def start_live(job_queue):
    global live_job
    stop_live()
    live_job = job_queue.run_repeating(live_tick, interval=5, first=0)


def live_tick(context: CallbackContext):
    global data
    with lock:
        if not data.get("active"):
            stop_live()
            return

        start_ts = data.get("start_time_utc")
        if start_ts is None:
            data["start_time_utc"] = now_utc_ts()
            save_data()
            start_ts = data["start_time_utc"]

        duration = int(data.get("duration_seconds", 1) or 1)
        elapsed = int(now_utc_ts() - float(start_ts))
        remaining = duration - elapsed

        live_mid = data.get("live_message_id")

        if remaining <= 0:
            # close giveaway
            data["active"] = False
            data["closed"] = True
            save_data()

            # delete live post
            if live_mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
                except Exception:
                    pass

            # post closed message
            try:
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text())
                data["closed_message_id"] = m.message_id
                save_data()
            except Exception:
                pass

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

            add_log("GIVEAWAY_CLOSED_AUTO", details=f"title={data.get('title','')} participants={participants_count()}")

            stop_live()

            # AUTO WINNER POST
            if data.get("auto_winner_on"):
                # Start draw progress pinned in channel (no dots in closed post)
                try:
                    start_draw_progress(context, CHANNEL_ID, pin_in_channel=True)
                except Exception:
                    pass

            return

        if not live_mid:
            return

        # update live message
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
# RESET PROGRESS (40s)
# =========================================================
RESET_DURATION = 40
RESET_INTERVAL = 1

def stop_reset():
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


def build_reset_text(percent: int, dots: str, spin: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ FULL RESET\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔎 Resetting... {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        f"{spin} Please wait{dots}"
    )


def do_full_reset_all(context: CallbackContext, admin_chat_id: int, admin_msg_id: int):
    global data

    # stop jobs
    stop_live()
    stop_draw()
    stop_reset()

    with lock:
        # delete channel messages if any
        for k in ["live_message_id", "closed_message_id", "draw_message_id", "winners_message_id"]:
            mid = data.get(k)
            if mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
                except Exception:
                    pass

        data = fresh_default_data()
        save_data()

    try:
        context.bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            text=(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ RESET COMPLETED SUCCESSFULLY!\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Bot is now brand new.\n"
                "All saved data has been removed.\n\n"
                "Start again with:\n"
                "/newgiveaway"
            ),
        )
    except Exception:
        pass


def start_reset_progress(context: CallbackContext, admin_chat_id: int):
    global reset_job, reset_finalize_job
    stop_reset()

    msg = context.bot.send_message(chat_id=admin_chat_id, text=build_reset_text(0, ".", SPINNER[0]))
    ctx = {"chat_id": admin_chat_id, "msg_id": msg.message_id, "start_ts": now_utc_ts(), "tick": 0}

    def tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] = jd.get("tick", 0) + 1

        elapsed = max(0, now_utc_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(RESET_DURATION)) * 100)))
        dots = DOTS[(jd["tick"] - 1) % len(DOTS)]
        spin = SPINNER[(jd["tick"] - 1) % len(SPINNER)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["chat_id"],
                message_id=jd["msg_id"],
                text=build_reset_text(percent, dots, spin),
            )
        except Exception:
            pass

    reset_job = context.job_queue.run_repeating(tick, interval=RESET_INTERVAL, first=0, context=ctx)
    reset_finalize_job = context.job_queue.run_once(
        lambda ctx2: do_full_reset_all(ctx2, ctx["chat_id"], ctx["msg_id"]),
        when=RESET_DURATION,
        context=ctx,
    )


# =========================================================
# ADMIN LIST PARSER
# =========================================================
def parse_user_lines(text: str):
    """
    Accept:
    123456789
    @name | 123456789
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
        add_log("START", str(u.id), user_tag(u.username or ""), "user pressed /start")

    if u and u.id == ADMIN_ID:
        update.message.reply_text(
            "🛡️👑 WELCOME BACK, ADMIN 👑🛡️\n\n"
            "⚙️ System Status: ONLINE ✅\n"
            "🚀 Giveaway Engine: READY\n"
            "🔐 Security Level: MAXIMUM\n\n"
            "🧭 Open the Admin Control Panel:\n"
            "/panel\n\n"
            f"⚡ POWERED BY: {HOST_NAME}"
        )
    else:
        update.message.reply_text(build_start_user_text())


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
        "/winnerlist\n\n"
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
        "📄 REPORT\n"
        "/manager DD/MM/YYYY\n\n"
        "♻️ RESET\n"
        "/reset"
    )


def cmd_autowinnerpost(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "Choose Auto Winner Post mode:",
        reply_markup=autowinner_markup()
    )


def cmd_backrules(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "BACKRULES CONTROL:",
        reply_markup=backrules_markup()
    )


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
        "After adding, users must join ALL verify targets to join giveaway."
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
    global admin_state
    if not is_admin(update):
        return

    with lock:
        # keep verify & blocks & systems (you can still change later)
        keep_verify = data.get("verify_targets", [])
        keep_perma = data.get("permanent_block", {})
        keep_old_winners = data.get("old_winners", {})
        keep_old_mode = data.get("old_winner_mode", "skip")
        keep_backrules = data.get("backrules_on", False)
        keep_backrules_ban = data.get("backrules_ban", {})
        keep_auto = data.get("auto_winner_on", False)
        keep_history = data.get("winner_history", [])
        keep_logs = data.get("logs", [])

        data.clear()
        data.update(fresh_default_data())
        data["verify_targets"] = keep_verify
        data["permanent_block"] = keep_perma
        data["old_winners"] = keep_old_winners
        data["old_winner_mode"] = keep_old_mode
        data["backrules_on"] = keep_backrules
        data["backrules_ban"] = keep_backrules_ban
        data["auto_winner_on"] = keep_auto
        data["winner_history"] = keep_history
        data["logs"] = keep_logs
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
    if not data.get("participants", {}):
        update.message.reply_text("No participants to draw winners from.")
        return

    # Start draw progress in ADMIN chat (manual mode)
    start_draw_progress(context, update.effective_chat.id, pin_in_channel=False)


def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ FULL RESET CONFIRMATION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ This will remove EVERYTHING.\n\n"
        "Confirm reset?",
        reply_markup=reset_confirm_markup()
    )


def cmd_winnerlist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    hist = data.get("winner_history", []) or []
    if not hist:
        update.message.reply_text("No winner history found yet.")
        return

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆 WINNER HISTORY LIST")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    # show last 50 only
    last = hist[-50:]
    for i, w in enumerate(last, start=1):
        lines.append(f"{i}) {w.get('username','') or 'User'} | {w.get('uid','')}")
        lines.append(f"   Win Type: {w.get('type','')}")
        lines.append(f"   Time (BD): {w.get('time_bd','')}")
        lines.append(f"   Giveaway: {w.get('title','')}")
        lines.append("")

    update.message.reply_text("\n".join(lines))


def cmd_manager(update: Update, context: CallbackContext):
    """
    /manager DD/MM/YYYY  -> export that date logs as TXT
    """
    if not is_admin(update):
        return

    args = context.args or []
    if not args:
        update.message.reply_text("Use: /manager DD/MM/YYYY")
        return

    date_str = args[0].strip()
    try:
        dd, mm, yy = date_str.split("/")
        target = f"{int(dd):02d}/{int(mm):02d}/{int(yy):04d}"
    except Exception:
        update.message.reply_text("Use format: DD/MM/YYYY")
        return

    logs = data.get("logs", []) or []
    filtered = []
    for item in logs:
        t = (item.get("time_bd", "") or "")
        if t.startswith(target):
            filtered.append(item)

    # build txt
    out_lines = []
    out_lines.append(f"REPORT DATE: {target} (BD Time)")
    out_lines.append("-" * 40)
    if not filtered:
        out_lines.append("No logs found for this date.")
    else:
        for it in filtered:
            out_lines.append(f"[{it.get('time_bd','')}] {it.get('type','')}")
            if it.get("username") or it.get("uid"):
                out_lines.append(f"User: {it.get('username','')} | {it.get('uid','')}")
            if it.get("details"):
                out_lines.append(f"Details: {it.get('details')}")
            out_lines.append("")

    fname = f"manager_{target.replace('/','-')}.txt"
    path = os.path.join(os.path.dirname(__file__), fname) if "__file__" in globals() else fname
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines))

        with open(path, "rb") as f:
            update.message.reply_document(document=f, filename=fname, caption="Manager report (TXT)")
    except Exception as e:
        update.message.reply_text(f"Failed to generate report: {e}")


def cmd_blockpermanent(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "perma_block_list"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 PERMANENT BLOCK\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send list (one per line):\n"
        "User ID only OR username + id\n\n"
        "Examples:\n"
        "7297292\n"
        "@MinexxProo | 7297292"
    )


def cmd_blockoldwinner(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "oldwinner_block_add"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⛔ OLD WINNER BLOCK LIST ADD\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send list (one per line):\n"
        "@username | user_id OR user_id\n\n"
        "Example:\n"
        "@minexxproo | 8392828\n"
        "556677"
    )


def cmd_unban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "unban_choose"
    update.message.reply_text("Choose Unban Type:", reply_markup=unban_choose_markup())


def cmd_removeban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "removeban_choose"
    update.message.reply_text("Choose which ban list to reset:", reply_markup=removeban_choose_markup())


def cmd_blocklist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    perma = data.get("permanent_block", {}) or {}
    oldw = data.get("old_winners", {}) or {}

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📌 BAN LISTS")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"OLD WINNER MODE: {data.get('old_winner_mode','skip').upper()}")
    lines.append("")

    lines.append("⛔ OLD WINNER BLOCK LIST")
    lines.append(f"Total: {len(oldw)}")
    if oldw:
        i = 1
        for uid, info in oldw.items():
            uname = (info or {}).get("username", "")
            if uname:
                lines.append(f"{i}) {uname} | User ID: {uid}")
            else:
                lines.append(f"{i}) User ID: {uid}")
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
            if uname:
                lines.append(f"{i}) {uname} | User ID: {uid}")
            else:
                lines.append(f"{i}) User ID: {uid}")
            i += 1
    else:
        lines.append("No permanently blocked users.")

    update.message.reply_text("\n".join(lines))


# =========================================================
# ADMIN TEXT FLOW
# =========================================================
def admin_text_handler(update: Update, context: CallbackContext):
    global admin_state
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
            update.message.reply_text("Invalid input.\nSend Chat ID like -100123... or @username.")
            return

        with lock:
            targets = data.get("verify_targets", []) or []
            if len(targets) >= 100:
                update.message.reply_text("Max verify targets reached (100). Remove some first.")
                return
            targets.append({"ref": ref, "display": ref})
            data["verify_targets"] = targets
            save_data()

        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET ADDED SUCCESSFULLY!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Added: {ref}\n"
            f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}\n\n"
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
                update.message.reply_text("✅ All verify targets removed successfully!")
                return

            if n < 1 or n > len(targets):
                update.message.reply_text("Invalid number. Try again.")
                return

            removed = targets.pop(n - 1)
            data["verify_targets"] = targets
            save_data()

        admin_state = None
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET REMOVED\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Removed: {removed.get('display','')}\n"
            f"Remaining: {len(data.get('verify_targets', []) or [])}"
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

        # OLD WINNER MODE
        admin_state = "old_winner_mode"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 OLD WINNER PROTECTION MODE\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "1️⃣ BLOCK OLD WINNERS\n"
            "• Old winners cannot join this giveaway\n\n"
            "2️⃣ SKIP OLD WINNERS\n"
            "• Everyone can join\n"
            "• No old winner list needed\n\n"
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

        # block mode needs list
        with lock:
            data["old_winner_mode"] = "block"
            data["old_winners"] = {}
            save_data()
        admin_state = "old_winner_block_list"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⛔ OLD WINNER BLOCK LIST SETUP\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Send old winners list (one per line):\n\n"
            "Format:\n"
            "@username | user_id\n\n"
            "Example:\n"
            "@minexxproo | 728272\n"
            "556677"
        )
        return

    if admin_state == "old_winner_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
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
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
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

    # OLD WINNER BLOCK ADD (command)
    if admin_state == "oldwinner_block_add":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            ow = data.get("old_winners", {}) or {}
            before = len(ow)
            for uid, uname in entries:
                ow[str(uid)] = {"username": uname}
            data["old_winners"] = ow
            data["old_winner_mode"] = "block"  # ensure it actually blocks
            save_data()
        admin_state = None
        update.message.reply_text(
            "✅ Old winner block list updated!\n"
            f"New Added: {len(data['old_winners']) - before}\n"
            f"Total Old Winner Blocked: {len(data['old_winners'])}\n"
            "Mode set to: BLOCK"
        )
        return

    # UNBAN INPUT
    if admin_state == "unban_permanent_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @name | id)")
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
            update.message.reply_text("Send User ID (or @name | id)")
            return
        uid, _ = entries[0]
        with lock:
            ow = data.get("old_winners", {}) or {}
            if str(uid) in ow:
                del ow[str(uid)]
                data["old_winners"] = ow
                save_data()
                update.message.reply_text("✅ Unbanned from Old Winner Block successfully!")
            else:
                update.message.reply_text("This user id is not in Old Winner Block list.")
        admin_state = None
        return

    # BACKRULES UNBAN INPUT
    if admin_state == "backrules_unban_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @name | id)")
            return
        uid, _ = entries[0]
        with lock:
            bl = data.get("backrules_ban", {}) or {}
            if str(uid) in bl:
                del bl[str(uid)]
                data["backrules_ban"] = bl
                save_data()
                update.message.reply_text("✅ Access unlocked successfully.")
            else:
                update.message.reply_text("This user id is not in Backrules Banlist.")
        admin_state = None
        return


# =========================================================
# CALLBACK HANDLER
# =========================================================
def block_any_button_if_needed(query, uid: str, context: CallbackContext) -> bool:
    """
    If user is in permanent block or old winner block or backrules ban => show popup and return True (blocked).
    Applies to ANY button.
    """
    # permanent
    if uid in (data.get("permanent_block", {}) or {}):
        try:
            query.answer(popup_permanent_blocked(), show_alert=True)
        except Exception:
            pass
        return True

    # old winner block only if mode=block AND list not empty
    if data.get("old_winner_mode") == "block":
        ow = data.get("old_winners", {}) or {}
        if ow and (uid in ow):
            try:
                query.answer(popup_old_winner_blocked(), show_alert=True)
            except Exception:
                pass
            return True

    # backrules ban
    brb = data.get("backrules_ban", {}) or {}
    if brb and (uid in brb):
        try:
            query.answer(popup_backrules_locked(), show_alert=True)
        except Exception:
            pass
        return True

    return False


def cb_handler(update: Update, context: CallbackContext):
    global admin_state
    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)

    # block ANY button if needed
    if qd not in ("preview_approve", "preview_reject", "preview_edit", "winners_approve", "winners_reject",
                  "verify_add_more", "verify_add_done", "end_confirm", "end_cancel",
                  "reset_confirm", "reset_cancel",
                  "autowin_on", "autowin_off",
                  "backrules_on", "backrules_off", "backrules_unban", "backrules_banlist",
                  "unban_permanent", "unban_oldwinner",
                  "reset_permanent_ban", "reset_oldwinner_ban", "confirm_reset_permanent", "confirm_reset_oldwinner",
                  "cancel_reset_ban"):
        if block_any_button_if_needed(query, uid, context):
            return

    # =======================
    # Auto Winner Post toggle
    # =======================
    if qd in ("autowin_on", "autowin_off"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return

        with lock:
            data["auto_winner_on"] = (qd == "autowin_on")
            save_data()

        try:
            query.answer()
        except Exception:
            pass
        try:
            query.edit_message_text(
                f"✅ Auto Winner Post is now {'ON' if data['auto_winner_on'] else 'OFF'}"
            )
        except Exception:
            pass
        return

    # =======================
    # Backrules control
    # =======================
    if qd in ("backrules_on", "backrules_off"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        with lock:
            data["backrules_on"] = (qd == "backrules_on")
            save_data()
        try:
            query.answer()
        except Exception:
            pass
        try:
            query.edit_message_text(f"✅ Backrules is now {'ON' if data['backrules_on'] else 'OFF'}")
        except Exception:
            pass
        return

    if qd == "backrules_unban":
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
        admin_state = "backrules_unban_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to UNBAN from Backrules banlist:")
        except Exception:
            pass
        return

    if qd == "backrules_banlist":
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

        bl = data.get("backrules_ban", {}) or {}
        lines = []
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("🚫 BACKRULES BANLIST")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"Total: {len(bl)}")
        lines.append("")
        if not bl:
            lines.append("No banned users.")
        else:
            i = 1
            for k, info in bl.items():
                uname = (info or {}).get("username", "")
                lines.append(f"{i}) {uname or 'User'} | User ID: {k}")
                i += 1
        try:
            query.edit_message_text("\n".join(lines))
        except Exception:
            pass
        return

    # =======================
    # Verify add buttons
    # =======================
    if qd == "verify_add_more":
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
        try:
            query.answer()
        except Exception:
            pass
        admin_state = None
        try:
            query.edit_message_text(
                "✅ Verify setup completed successfully!\n\n"
                f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}"
            )
        except Exception:
            pass
        return

    # =======================
    # Preview actions
    # =======================
    if qd.startswith("preview_"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return

        if qd == "preview_approve":
            try:
                query.answer()
            except Exception:
                pass

            try:
                duration = int(data.get("duration_seconds", 0) or 1)
                m = context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=build_live_text(duration),
                    reply_markup=join_button_markup(),
                )

                with lock:
                    data["live_message_id"] = m.message_id
                    data["active"] = True
                    data["closed"] = False
                    data["start_time_utc"] = now_utc_ts()

                    # clear giveaway runtime state
                    data["closed_message_id"] = None
                    data["draw_message_id"] = None
                    data["winners_message_id"] = None
                    data["participants"] = {}
                    data["winners"] = {}
                    data["pending_winners_text"] = ""
                    data["claim_deadline_utc"] = None
                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""

                    save_data()

                start_live(context.job_queue)
                query.edit_message_text("✅ Giveaway approved and posted to channel!")
                add_log("GIVEAWAY_POSTED", details=f"title={data.get('title','')}")

            except Exception as e:
                try:
                    query.edit_message_text(f"Failed to post in channel. Make sure bot is admin.\nError: {e}")
                except Exception:
                    pass
            return

        if qd == "preview_reject":
            try:
                query.answer()
            except Exception:
                pass
            try:
                query.edit_message_text("❌ Giveaway rejected and cleared.")
            except Exception:
                pass
            return

        if qd == "preview_edit":
            try:
                query.answer()
            except Exception:
                pass
            try:
                query.edit_message_text("✏️ Edit Mode\n\nStart again with /newgiveaway")
            except Exception:
                pass
            return

    # =======================
    # End giveaway confirm/cancel
    # =======================
    if qd == "end_confirm":
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

        stop_live()

        try:
            query.edit_message_text("✅ Giveaway Closed Successfully! Now use /draw")
        except Exception:
            pass

        add_log("GIVEAWAY_CLOSED_MANUAL", details=f"title={data.get('title','')}")
        return

    if qd == "end_cancel":
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
        try:
            query.edit_message_text("❌ Cancelled. Giveaway is still running.")
        except Exception:
            pass
        return

    # =======================
    # Reset confirm/cancel
    # =======================
    if qd == "reset_confirm":
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

        # start 40s progress reset in admin chat
        try:
            start_reset_progress(context, query.message.chat_id)
        except Exception:
            pass

        try:
            query.edit_message_text("✅ Confirmed. Reset process started...")
        except Exception:
            pass
        return

    if qd == "reset_cancel":
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
        try:
            query.edit_message_text("❌ Reset cancelled.")
        except Exception:
            pass
        return

    # =======================
    # Unban choose
    # =======================
    if qd == "unban_permanent":
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
        admin_state = "unban_permanent_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Permanent Block:")
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
        try:
            query.answer()
        except Exception:
            pass
        admin_state = "unban_oldwinner_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Old Winner Block:")
        except Exception:
            pass
        return

    # =======================
    # Removeban choose & confirm
    # =======================
    if qd in ("reset_permanent_ban", "reset_oldwinner_ban"):
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

        if qd == "reset_permanent_ban":
            try:
                query.edit_message_text("Confirm reset Permanent Ban List?", reply_markup=removeban_confirm_markup("permanent"))
            except Exception:
                pass
            return

        if qd == "reset_oldwinner_ban":
            try:
                query.edit_message_text("Confirm reset Old Winner Ban List?", reply_markup=removeban_confirm_markup("oldwinner"))
            except Exception:
                pass
            return

    if qd == "cancel_reset_ban":
        try:
            query.answer()
        except Exception:
            pass
        admin_state = None
        try:
            query.edit_message_text("Cancelled.")
        except Exception:
            pass
        return

    if qd == "confirm_reset_permanent":
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
            data["permanent_block"] = {}
            save_data()
        try:
            query.edit_message_text("✅ Permanent Ban List has been reset.")
        except Exception:
            pass
        return

    if qd == "confirm_reset_oldwinner":
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
            data["old_winners"] = {}
            save_data()
        try:
            query.edit_message_text("✅ Old Winner Ban List has been reset.")
        except Exception:
            pass
        return

    # =======================
    # JOIN GIVEAWAY
    # =======================
    if qd == "join_giveaway":
        # permanent/old/backrules already blocked above for any button

        if not data.get("active"):
            try:
                query.answer("This giveaway is not active right now.", show_alert=True)
            except Exception:
                pass
            return

        # verify
        ok, missing_ref = verify_user_join(context.bot, int(uid))
        if not ok:
            # backrules ON => lock user permanently until admin unban
            if data.get("backrules_on"):
                tg_user = query.from_user
                uname = user_tag(tg_user.username or "")
                with lock:
                    bl = data.get("backrules_ban", {}) or {}
                    if uid not in bl:
                        bl[uid] = {"username": uname, "banned_at": fmt_bd(now_bd()), "missing_ref": missing_ref}
                        data["backrules_ban"] = bl
                        save_data()
                    add_log("BACKRULES_LOCK", uid, uname, f"missing={missing_ref}")

                try:
                    query.answer(popup_backrules_locked(), show_alert=True)
                except Exception:
                    pass
                return

            # normal verify popup
            add_log("VERIFY_FAIL", uid, user_tag(query.from_user.username or ""), f"missing={missing_ref}")
            try:
                query.answer(popup_verify_required(), show_alert=True)
            except Exception:
                pass
            return

        # if backrules ban exists even after verify success => still blocked (your rule)
        if (data.get("backrules_ban", {}) or {}).get(uid):
            try:
                query.answer(popup_backrules_locked(), show_alert=True)
            except Exception:
                pass
            return

        # FIRST WINNER repeat clicks => same popup always
        first_uid = data.get("first_winner_id")
        if first_uid and uid == str(first_uid):
            uname = user_tag(query.from_user.username or "") or data.get("first_winner_username", "") or "@username"
            try:
                query.answer(popup_first_winner(uname, uid), show_alert=True)
            except Exception:
                pass
            return

        # already joined (normal users)
        if uid in (data.get("participants", {}) or {}):
            try:
                query.answer(popup_already_joined(), show_alert=True)
            except Exception:
                pass
            return

        # success join
        tg_user = query.from_user
        uname = user_tag(tg_user.username or "")
        full_name = (tg_user.full_name or "").strip()

        with lock:
            # first join => 1st winner
            if not data.get("first_winner_id"):
                data["first_winner_id"] = uid
                data["first_winner_username"] = uname
                data["first_winner_name"] = full_name

            data["participants"][uid] = {"username": uname, "name": full_name}
            save_data()

        add_log("JOIN", uid, uname, f"title={data.get('title','')}")

        # instant update live post (participants)
        try:
            live_mid = data.get("live_message_id")
            start_ts = data.get("start_time_utc")
            if live_mid and start_ts:
                duration = int(data.get("duration_seconds", 1) or 1)
                elapsed = int(now_utc_ts() - float(start_ts))
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

        # popup: first winner / normal join
        if data.get("first_winner_id") == uid:
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

    # =======================
    # WINNERS APPROVE/REJECT
    # =======================
    if qd == "winners_approve":
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

        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=text, reply_markup=claim_button_markup())
            with lock:
                data["winners_message_id"] = m.message_id
                data["closed_message_id"] = None

                # auto winner history
                winners_map = data.get("winners", {}) or {}
                first_uid = data.get("first_winner_id") or ""
                add_winner_history(winners_map, str(first_uid))

                save_data()

            add_log("WINNERS_POSTED_MANUAL", details=f"winners={len(data.get('winners', {}) or {})}")

            try:
                query.edit_message_text("✅ Approved! Winners list posted to channel (with Claim button).")
            except Exception:
                pass

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
        try:
            query.answer()
        except Exception:
            pass
        with lock:
            data["pending_winners_text"] = ""
            data["winners"] = {}
            data["claim_deadline_utc"] = None
            save_data()
        try:
            query.edit_message_text("❌ Rejected! Winners list will NOT be posted.")
        except Exception:
            pass
        return

    # =======================
    # CLAIM PRIZE
    # =======================
    if qd == "claim_prize":
        # permanent/old/backrules block should show same popup (already handled earlier)
        winners = data.get("winners", {}) or {}
        if uid in winners:
            # check expiry
            deadline = data.get("claim_deadline_utc")
            if deadline and now_utc_ts() > float(deadline):
                try:
                    query.answer(popup_claim_expired(), show_alert=True)
                except Exception:
                    pass
                return

            uname = winners.get(uid, {}).get("username", "") or user_tag(query.from_user.username or "") or "@username"
            try:
                query.answer(popup_claim_winner(uname, uid), show_alert=True)
            except Exception:
                pass
        else:
            try:
                query.answer(popup_claim_not_winner(), show_alert=True)
            except Exception:
                pass
        return

    # default
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

    # giveaway
    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))
    dp.add_handler(CommandHandler("winnerlist", cmd_winnerlist))

    # auto/backrules
    dp.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))
    dp.add_handler(CommandHandler("backrules", cmd_backrules))

    # verify
    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    # blocks
    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("blockoldwinner", cmd_blockoldwinner))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))

    # report
    dp.add_handler(CommandHandler("manager", cmd_manager))

    # reset
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # text + callbacks
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # resume live if active
    if data.get("active"):
        start_live(updater.job_queue)

    print("Bot is running (PTB 13, non-async, polling) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
