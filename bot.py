# bot.py
# POWER POINT BREAK - Giveaway Bot (python-telegram-bot v13.x, Updater, non-async)
# FULL A TO Z - All systems fixed + fast popups + draw progress + reset progress + verify add buttons + prize delivery proof
#
# ✅ Live giveaway post updates every 5s (Time Remaining + Progress bar)
# ✅ Join popups (verify required / old winner block / permanent block / already joined / join success)
# ✅ First Join = 1st Winner (click again -> same 1st winner popup every time)
# ✅ Old winner mode: BLOCK (list required) / SKIP (no list; no popup; everyone can join)
# ✅ Global button guard: Permanent/OldWinner blocked users -> ANY button click shows same popup
# ✅ Giveaway close: deletes live post -> posts CLOSED post -> (optional) animates 7 dots fast
# ✅ /draw: 40s progress (no countdown number) + % + bar + fast spinner + 7 dots
# ✅ Approve/Reject winners preview; Approve posts to channel and deletes CLOSED post
# ✅ Claim button expires after 24h (winners see expired popup after expiry)
# ✅ /reset: confirm -> 40s progress -> FULL WIPE (ALL means ALL)
# ✅ /prizedeliveryprove: collect photos -> DONE -> preview -> Approve posts album+caption
# ✅ /completePrize: preview -> approve -> channel post
# ✅ /winnerlist: shows winner history (auto saved whenever winners are posted)
# ✅ /blocklist: shows old-winner list and permanent list separately
# ✅ /unban: choose list -> send ids (multiple lines) -> unban
# ✅ /removeban: choose list -> confirm -> clears selected list
# ✅ /autowinnerpost: On/Off (On => after close auto draw progress in channel then auto post winners)

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
    InputMediaPhoto,
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

# =========================================================
# THREAD SAFE STORAGE
# =========================================================
lock = threading.RLock()

# =========================================================
# GLOBAL STATE / JOB REFS
# =========================================================
data = {}
admin_state = None

job_live = None                 # channel live giveaway update (5s)
job_close_dots = None           # closed post dot animation (1s)
job_draw_progress = None        # draw progress animation (1s)
job_draw_finalize = None        # draw finalize once (40s)
job_reset_progress = None       # reset progress animation (1s)
job_reset_finalize = None       # reset finalize once (40s)

# =========================================================
# CONSTANTS
# =========================================================
LIVE_UPDATE_INTERVAL = 5  # seconds (giveaway post)
DRAW_DURATION_SECONDS = 40
DRAW_UPDATE_INTERVAL = 1  # fast
RESET_DURATION_SECONDS = 40
RESET_UPDATE_INTERVAL = 1  # fast
CLAIM_EXPIRE_HOURS = 24

DOTS = [".", "..", "...", "....", ".....", "......", "......."]
SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]  # rotating look

# =========================================================
# DATA / STORAGE
# =========================================================
def fresh_default_data():
    return {
        # giveaway state
        "active": False,
        "closed": False,

        "title": "",
        "prize": "",
        "winner_count": 0,
        "duration_seconds": 0,
        "rules": "",

        "start_time": None,

        # channel message ids
        "live_message_id": None,
        "closed_message_id": None,
        "winners_message_id": None,
        "draw_progress_message_id": None,

        # participants
        "participants": {},  # uid(str)->{"username":"@x","name":""}

        # first join winner
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # verify targets
        "verify_targets": [],  # [{"ref":"-100.. or @xxx","display":"..."}]

        # permanent block
        "permanent_block": {},  # uid -> {"username":"@x"}

        # old winner protection
        "old_winner_mode": "skip",  # "block" or "skip"
        "old_winners": {},          # used only if mode=block; uid -> {"username":"@x"}

        # draw / winners
        "winners": {},                 # uid -> {"username":"@x"}
        "pending_winners_text": "",

        # claim expiry
        "winners_post_time": None,      # timestamp utc
        "claim_expires_at": None,       # timestamp utc

        # automation
        "autowinnerpost": False,        # auto draw + auto post in channel after close

        # prize delivery proof pending (admin)
        "pending_prize_photos": [],     # list of file_id
        "pending_prize_caption": "",    # preview caption text

        # winner history
        "winner_history": [],           # list of dict entries
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
    """
    Accept:
    30 Second / 30 sec
    30 Minute
    11 Hour / 11 hr
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


def winners_approve_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Post", callback_data="winners_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="winners_reject"),
        ]]
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
            InlineKeyboardButton("❌ Reject", callback_data="reset_reject"),
        ]]
    )


def autowinnerpost_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Auto Post ON", callback_data="autopost_on"),
            InlineKeyboardButton("❌ Auto Post OFF", callback_data="autopost_off"),
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


def confirm_clear_list_markup(confirm_cb: str):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm", callback_data=confirm_cb),
            InlineKeyboardButton("❌ Reject", callback_data="cancel_clear_list"),
        ]]
    )


def prizeproof_approve_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Post", callback_data="prizeproof_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="prizeproof_reject"),
        ]]
    )


def completeprize_approve_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Post", callback_data="completeprize_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="completeprize_reject"),
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
# POPUP TEXTS (ONLY SPACING, NO EXTRA WORD/EMOJI ADDED)
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
    # short 199-style user wanted
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
        "Your prize claim time is over.\n"
        "Please wait for the next giveaway."
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
# GIVEAWAY TEXT BUILDERS
# =========================================================
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


def build_closed_post_text(dots: str = ".......") -> str:
    # user-provided "2 line" border style
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY OFFICIALLY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        f"🎯 Winner selection is currently in progress{dots}\n"
        "Please wait for the official announcement.\n\n"
        "🙏 Thank you to everyone who participated.\n"
        f"— {HOST_NAME} ⚡"
    )


def build_winners_post_text(first_uid: str, first_user: str, random_winners: list) -> str:
    # user requested style + box for 1st winner
    lines = []
    lines.append("🏆 GIVEAWAY WINNERS ANNOUNCEMENT 🏆")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"⚡ {data.get('title','')} ⚡")
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
    lines.append("👇 Click the button below to claim your prize:")
    return "\n".join(lines)


def build_draw_progress_text(percent: int, spinner: str, dots: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔎 Selecting winners... {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        f"{spinner} Winner selection is in progress{dots}\n\n"
        "✅ This draw is 100% fair & random.\n"
        "🔐 User ID based selection only.\n\n"
        f"Please wait{dots}"
    )


def build_reset_progress_text(percent: int, spinner: str, dots: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ FULL RESET IN PROGRESS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔎 Resetting system... {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        f"{spinner} Please wait{dots}\n\n"
        "⚠️ This will remove EVERYTHING."
    )


def build_prize_delivery_caption() -> str:
    # short caption as you wanted (no 24h rule here)
    # winners list auto from last winners in data["winners"]
    winners = data.get("winners", {}) or {}
    winner_lines = []
    for uid, info in winners.items():
        uname = (info or {}).get("username", "")
        if uname:
            winner_lines.append(f"👤 {uname} | 🆔 {uid}")
        else:
            winner_lines.append(f"👤 User ID: {uid}")
    winners_text = "\n".join(winner_lines) if winner_lines else "(no winners saved)"

    return (
        "🏆 PRIZE DELIVERY CONFIRMED ✅\n\n"
        "All giveaway prizes have been successfully delivered to the winners 🎉\n\n"
        f"🎁 Giveaway: {data.get('title','')}\n"
        "👑 Winners:\n"
        f"{winners_text}\n\n"
        "Thank you everyone for participating 💙\n"
        f"— {HOST_NAME} ⚡"
    )


def build_complete_prize_text() -> str:
    winners = data.get("winners", {}) or {}
    wl = []
    i = 1
    for uid, info in winners.items():
        uname = (info or {}).get("username", "")
        if uname:
            wl.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}")
        else:
            wl.append(f"{i}️⃣ 👤 User ID: {uid}")
        i += 1
    winners_list_text = "\n".join(wl) if wl else "No winners saved."

    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎉🏆 PRIZE DELIVERY COMPLETED 🏆🎉\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "We confirm that all giveaway prizes have been\n"
        "successfully delivered to the winners ✅\n\n"
        "👑 Confirmed Winners:\n"
        f"{winners_list_text}\n\n"
        f"📩 Need help?\nContact Admin: {ADMIN_CONTACT}\n\n"
        f"Thank you for staying with\n{HOST_NAME} 💙"
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
# LIVE GIVEAWAY JOB (5s)
# =========================================================
def stop_live_countdown():
    global job_live
    if job_live is not None:
        try:
            job_live.schedule_removal()
        except Exception:
            pass
    job_live = None


def start_live_countdown(job_queue):
    global job_live
    stop_live_countdown()
    job_live = job_queue.run_repeating(live_tick, interval=LIVE_UPDATE_INTERVAL, first=0)


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
        duration = int(data.get("duration_seconds", 1) or 1)
        elapsed = int((now - start).total_seconds())
        remaining = duration - elapsed

        live_mid = data.get("live_message_id")

    if remaining <= 0:
        close_giveaway(context, auto_trigger=True)
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
# CLOSED POST DOT ANIMATION (FAST)
# =========================================================
def stop_closed_dots():
    global job_close_dots
    if job_close_dots is not None:
        try:
            job_close_dots.schedule_removal()
        except Exception:
            pass
    job_close_dots = None


def start_closed_dots(job_queue):
    global job_close_dots
    stop_closed_dots()
    job_close_dots = job_queue.run_repeating(closed_dots_tick, interval=1, first=1)


def closed_dots_tick(context: CallbackContext):
    # animate only while CLOSED post exists and winners not posted yet
    with lock:
        mid = data.get("closed_message_id")
        winners_mid = data.get("winners_message_id")
        active = data.get("active")
        closed = data.get("closed")
        tick = data.get("_closed_dots_tick", 0) + 1
        data["_closed_dots_tick"] = tick
        save_data()

    if active:
        stop_closed_dots()
        return
    if not closed or not mid:
        stop_closed_dots()
        return
    if winners_mid:
        stop_closed_dots()
        return

    dots = DOTS[(tick - 1) % len(DOTS)]
    try:
        context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=mid,
            text=build_closed_post_text(dots=dots),
        )
    except Exception:
        pass


# =========================================================
# CLOSE GIVEAWAY (delete live -> post closed -> notify admin -> maybe auto draw)
# =========================================================
def close_giveaway(context: CallbackContext, auto_trigger: bool):
    global data
    stop_live_countdown()

    with lock:
        if data.get("active"):
            data["active"] = False
        data["closed"] = True
        live_mid = data.get("live_message_id")
        data["live_message_id"] = None
        save_data()

    # delete live post
    if live_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
        except Exception:
            pass

    # post closed message
    try:
        m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text(dots="......."))
        with lock:
            data["closed_message_id"] = m.message_id
            data["_closed_dots_tick"] = 0
            save_data()
        # start dots animation
        start_closed_dots(context.job_queue)
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

    # auto winner post ON => run draw inside channel
    with lock:
        auto_on = bool(data.get("autowinnerpost"))

    if auto_trigger and auto_on:
        try:
            # Start draw progress in CHANNEL and auto-post winners
            start_draw_progress(context, target_chat_id=CHANNEL_ID, auto_post=True)
        except Exception:
            pass


# =========================================================
# DRAW PROGRESS (40s) + FINALIZE
# =========================================================
def stop_draw_jobs():
    global job_draw_progress, job_draw_finalize
    if job_draw_progress is not None:
        try:
            job_draw_progress.schedule_removal()
        except Exception:
            pass
    job_draw_progress = None

    if job_draw_finalize is not None:
        try:
            job_draw_finalize.schedule_removal()
        except Exception:
            pass
    job_draw_finalize = None


def start_draw_progress(context: CallbackContext, target_chat_id: int, auto_post: bool):
    """
    target_chat_id:
      - ADMIN chat for manual /draw
      - CHANNEL_ID for auto draw
    auto_post:
      - True => after finalize, post winners directly to channel (no approve)
      - False => admin preview with approve/reject
    """
    stop_draw_jobs()

    msg = context.bot.send_message(
        chat_id=target_chat_id,
        text=build_draw_progress_text(0, SPINNER[0], DOTS[0]),
    )

    with lock:
        if target_chat_id == CHANNEL_ID:
            data["draw_progress_message_id"] = msg.message_id
            save_data()

    ctx = {
        "target_chat_id": target_chat_id,
        "progress_msg_id": msg.message_id,
        "start_ts": datetime.utcnow().timestamp(),
        "tick": 0,
        "auto_post": auto_post,
    }

    def draw_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] = jd.get("tick", 0) + 1

        elapsed = max(0.0, datetime.utcnow().timestamp() - jd["start_ts"])
        percent = int(round(min(100.0, (elapsed / float(DRAW_DURATION_SECONDS)) * 100.0)))

        spinner = SPINNER[(jd["tick"] - 1) % len(SPINNER)]
        dots = DOTS[(jd["tick"] - 1) % len(DOTS)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["target_chat_id"],
                message_id=jd["progress_msg_id"],
                text=build_draw_progress_text(percent, spinner, dots),
            )
        except Exception:
            pass

    global job_draw_progress, job_draw_finalize
    job_draw_progress = context.job_queue.run_repeating(
        draw_tick,
        interval=DRAW_UPDATE_INTERVAL,
        first=0,
        context=ctx,
        name="draw_progress",
    )

    job_draw_finalize = context.job_queue.run_once(
        draw_finalize,
        when=DRAW_DURATION_SECONDS,
        context=ctx,
        name="draw_finalize",
    )


def draw_finalize(context: CallbackContext):
    global data
    stop_draw_jobs()

    jd = context.job.context
    target_chat_id = jd["target_chat_id"]
    progress_msg_id = jd["progress_msg_id"]
    auto_post = bool(jd.get("auto_post"))

    # delete/stop closed dots animation if we are going to post winners
    stop_closed_dots()

    with lock:
        participants = data.get("participants", {}) or {}
        if not participants:
            try:
                context.bot.edit_message_text(
                    chat_id=target_chat_id,
                    message_id=progress_msg_id,
                    text="No participants to draw winners from.",
                )
            except Exception:
                pass
            return

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

        first_uid = str(first_uid)
        first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

        # random pool excludes first
        pool = [uid for uid in participants.keys() if uid != first_uid]
        need = max(0, winner_count - 1)
        if need > len(pool):
            need = len(pool)
        selected = random.sample(pool, need) if need > 0 else []

        winners_map = {first_uid: {"username": first_uname}}
        random_list = []
        for uid in selected:
            info = participants.get(uid, {}) or {}
            winners_map[uid] = {"username": info.get("username", "")}
            random_list.append((uid, info.get("username", "")))

        pending_text = build_winners_post_text(first_uid, first_uname, random_list)

        data["winners"] = winners_map
        data["pending_winners_text"] = pending_text
        save_data()

    # remove progress message (channel auto draw wants it removed)
    try:
        context.bot.delete_message(chat_id=target_chat_id, message_id=progress_msg_id)
    except Exception:
        pass

    # if auto_post: delete CLOSED post then post winners immediately
    if auto_post:
        try:
            closed_mid = data.get("closed_message_id")
            if closed_mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
                except Exception:
                    pass
                with lock:
                    data["closed_message_id"] = None
                    save_data()
        except Exception:
            pass

        post_winners_to_channel(context, text=pending_text)
        try:
            # notify admin that auto post completed
            context.bot.send_message(chat_id=ADMIN_ID, text="✅ Auto Winner Post completed in channel.")
        except Exception:
            pass
        return

    # manual: show preview to admin with approve/reject
    try:
        context.bot.send_message(
            chat_id=target_chat_id,
            text=pending_text,
            reply_markup=winners_approve_markup(),
        )
    except Exception:
        pass


def post_winners_to_channel(context: CallbackContext, text: str):
    with lock:
        # claim expiry timestamps
        now_ts = datetime.utcnow().timestamp()
        data["winners_post_time"] = now_ts
        data["claim_expires_at"] = (datetime.utcnow() + timedelta(hours=CLAIM_EXPIRE_HOURS)).timestamp()
        save_data()

    try:
        m = context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            reply_markup=claim_button_markup(),
        )
        with lock:
            data["winners_message_id"] = m.message_id
            save_data()
    except Exception:
        return

    # add to winner history (auto)
    save_winner_history_entries()


def save_winner_history_entries():
    with lock:
        title = data.get("title", "")
        prize = data.get("prize", "")
        winners = data.get("winners", {}) or {}
        first_uid = data.get("first_winner_id")
        now = datetime.utcnow()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        history = data.get("winner_history", []) or []

        for uid, info in winners.items():
            uname = (info or {}).get("username", "")
            win_type = "👑 Random Winner"
            if first_uid and str(uid) == str(first_uid):
                win_type = "🥇 1st Winner (First Join)"

            history.append({
                "uid": str(uid),
                "username": uname,
                "date": date_str,
                "time": time_str,
                "win_type": win_type,
                "giveaway_title": title,
                "prize": prize,
            })

        data["winner_history"] = history
        save_data()


# =========================================================
# RESET (Confirm -> 40s Progress -> FULL WIPE ALL)
# =========================================================
def stop_reset_jobs():
    global job_reset_progress, job_reset_finalize
    if job_reset_progress is not None:
        try:
            job_reset_progress.schedule_removal()
        except Exception:
            pass
    job_reset_progress = None

    if job_reset_finalize is not None:
        try:
            job_reset_finalize.schedule_removal()
        except Exception:
            pass
    job_reset_finalize = None


def start_reset_progress(context: CallbackContext, admin_chat_id: int, msg_id: int):
    stop_reset_jobs()

    ctx = {
        "admin_chat_id": admin_chat_id,
        "msg_id": msg_id,
        "start_ts": datetime.utcnow().timestamp(),
        "tick": 0,
    }

    def reset_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] = jd.get("tick", 0) + 1

        elapsed = max(0.0, datetime.utcnow().timestamp() - jd["start_ts"])
        percent = int(round(min(100.0, (elapsed / float(RESET_DURATION_SECONDS)) * 100.0)))

        spinner = SPINNER[(jd["tick"] - 1) % len(SPINNER)]
        dots = DOTS[(jd["tick"] - 1) % len(DOTS)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["admin_chat_id"],
                message_id=jd["msg_id"],
                text=build_reset_progress_text(percent, spinner, dots),
            )
        except Exception:
            pass

    global job_reset_progress, job_reset_finalize
    job_reset_progress = context.job_queue.run_repeating(
        reset_tick,
        interval=RESET_UPDATE_INTERVAL,
        first=0,
        context=ctx,
        name="reset_progress",
    )

    job_reset_finalize = context.job_queue.run_once(
        reset_finalize,
        when=RESET_DURATION_SECONDS,
        context=ctx,
        name="reset_finalize",
    )


def reset_finalize(context: CallbackContext):
    global data
    stop_reset_jobs()
    stop_live_countdown()
    stop_draw_jobs()
    stop_closed_dots()

    jd = context.job.context
    admin_chat_id = jd["admin_chat_id"]
    msg_id = jd["msg_id"]

    # delete channel messages if exist
    with lock:
        mids = [
            data.get("live_message_id"),
            data.get("closed_message_id"),
            data.get("winners_message_id"),
            data.get("draw_progress_message_id"),
        ]

    for mid in mids:
        if mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
            except Exception:
                pass

    # FULL WIPE (ALL)
    with lock:
        data = fresh_default_data()
        save_data()

    try:
        context.bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=msg_id,
            text=(
                "✅ RESET COMPLETED SUCCESSFULLY!\n\n"
                "All information has been removed.\n"
                "Bot is now completely fresh.\n\n"
                "Start again with:\n"
                "/newgiveaway"
            ),
        )
    except Exception:
        pass


# =========================================================
# COMMANDS
# =========================================================
def cmd_start(update: Update, context: CallbackContext):
    u = update.effective_user
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
            "Stay connected with our official updates channel.\n"
            "Giveaway posts will appear there when active.\n\n"
            "🔗 Official Channel:\n"
            f"{CHANNEL_LINK}\n\n"
            "✅ Join the channel and wait for the giveaway post."
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
        "/endgiveaway\n\n"
        "🤖 AUTO\n"
        "/autowinnerpost\n\n"
        "🔒 BLOCK SYSTEM\n"
        "/blockpermanent\n"
        "/blockoldwinner\n"
        "/oldwinnerblock\n"
        "/unban\n"
        "/blocklist\n"
        "/removeban\n\n"
        "✅ VERIFY SYSTEM\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "🏆 WINNERS\n"
        "/winnerlist\n"
        "/completePrize\n\n"
        "📦 PROOF\n"
        "/prizedeliveryprove\n\n"
        "♻️ RESET\n"
        "/reset"
    )


def cmd_autowinnerpost(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "Choose Auto Winner Post mode:",
        reply_markup=autowinnerpost_markup()
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
        # keep verify + ban lists + automation (you can change if you want)
        keep_verify = data.get("verify_targets", [])
        keep_perma = data.get("permanent_block", {})
        keep_oldw = data.get("old_winners", {})
        keep_old_mode = data.get("old_winner_mode", "skip")
        keep_auto = bool(data.get("autowinnerpost", False))
        keep_history = data.get("winner_history", [])

        data.clear()
        data.update(fresh_default_data())

        data["verify_targets"] = keep_verify
        data["permanent_block"] = keep_perma
        data["old_winners"] = keep_oldw
        data["old_winner_mode"] = keep_old_mode
        data["autowinnerpost"] = keep_auto
        data["winner_history"] = keep_history
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

    # Manual draw in admin chat (preview + approve/reject)
    start_draw_progress(context, target_chat_id=update.effective_chat.id, auto_post=False)


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
    # forces BLOCK mode and asks list
    with lock:
        data["old_winner_mode"] = "block"
        save_data()

    admin_state = "old_winner_block_list_direct"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⛔ OLD WINNER BLOCK LIST\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send old winners list (one per line):\n\n"
        "Format:\n"
        "@username | user_id\n\n"
        "Example:\n"
        "@minexxproo | 728272\n"
        "@user2 | 889900\n"
        "556677"
    )


def cmd_oldwinnerblock(update: Update, context: CallbackContext):
    # alias same as /blockoldwinner
    cmd_blockoldwinner(update, context)


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
    lines.append(f"OLD WINNER MODE: {str(data.get('old_winner_mode','skip')).upper()}")
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

    history = data.get("winner_history", []) or []
    if not history:
        update.message.reply_text("No winner history found yet.")
        return

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆 WINNER HISTORY LIST (ALL WINNERS)")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    # latest first
    for idx, item in enumerate(reversed(history), start=1):
        uname = item.get("username", "")
        uid = item.get("uid", "")
        date = item.get("date", "")
        time = item.get("time", "")
        win_type = item.get("win_type", "")
        prize = item.get("prize", "")
        title = item.get("giveaway_title", "")

        if uname:
            lines.append(f"{idx}️⃣ 👤 {uname} | 🆔 {uid}")
        else:
            lines.append(f"{idx}️⃣ 👤 User ID: {uid}")
        lines.append(f"📅 Win Date: {date}")
        lines.append(f"⏰ Win Time: {time}")
        lines.append(f"🏅 Win Type: {win_type}")
        lines.append("")
        lines.append("🎁 Prize Won:")
        lines.append(prize if prize else "-")
        lines.append("")
        lines.append("⚡ Giveaway:")
        lines.append(title if title else "-")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")

    update.message.reply_text("\n".join(lines).strip())


def cmd_completePrize(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if not (data.get("winners", {}) or {}):
        update.message.reply_text("No winners saved. Post winners first.")
        return
    update.message.reply_text(build_complete_prize_text(), reply_markup=completeprize_approve_markup())


def cmd_prizedeliveryprove(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return

    with lock:
        data["pending_prize_photos"] = []
        data["pending_prize_caption"] = ""
        save_data()

    admin_state = "prize_proof_collect"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📦 PRIZE DELIVERY PROOF\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "This system is used to post official proof that prizes were successfully delivered ✅\n\n"
        "📸 Now send proof photos (you can send many).\n"
        "When finished, type: DONE"
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

    # PRIZE PROOF DONE
    if admin_state == "prize_proof_collect":
        if msg.strip().upper() != "DONE":
            update.message.reply_text("Send photos or type: DONE")
            return

        with lock:
            photos = data.get("pending_prize_photos", []) or []
        if not photos:
            update.message.reply_text("No photos received. Send photos first, then type DONE.")
            return

        caption = build_prize_delivery_caption()
        with lock:
            data["pending_prize_caption"] = caption
            save_data()

        admin_state = None
        update.message.reply_text(
            "✅ Preview ready. Approve or Reject?",
            reply_markup=prizeproof_approve_markup()
        )
        # show caption as preview text
        update.message.reply_text(caption)
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

        # old winner mode choose
        admin_state = "old_winner_mode"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 OLD WINNER PROTECTION MODE\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "1️⃣ BLOCK OLD WINNERS\n"
            "• Old winners cannot join this giveaway\n\n"
            "2️⃣ SKIP OLD WINNERS\n"
            "• Old winner block popups will NOT show\n"
            "• Everyone can join\n\n"
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

        # BLOCK
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
            "@user2 | 889900\n"
            "556677"
        )
        return

    if admin_state in ("old_winner_block_list", "old_winner_block_list_direct"):
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            ow = data.get("old_winners", {}) or {}
            before = len(ow)
            for uid, uname in entries:
                ow[uid] = {"username": uname}
            data["old_winners"] = ow
            data["old_winner_mode"] = "block"
            save_data()
            added = len(ow) - before

        admin_state = None if admin_state == "old_winner_block_list_direct" else "rules"
        if admin_state is None:
            update.message.reply_text(f"✅ Old winners blocked successfully!\nTotal Added: {added}")
        else:
            update.message.reply_text(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ OLD WINNER BLOCK LIST SAVED!\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📌 Total Added: {added}\n"
                "🔒 These users can NOT join this giveaway.\n\n"
                "Now send Giveaway Rules (multi-line):"
            )
        return

    if admin_state == "rules":
        with lock:
            data["rules"] = msg
            save_data()
        admin_state = None
        update.message.reply_text("✅ Rules saved!\n\nShowing preview…")
        update.message.reply_text(build_preview_text(), reply_markup=preview_markup())
        return

    # PERMANENT BLOCK ADD
    if admin_state == "perma_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            perma = data.get("permanent_block", {}) or {}
            before = len(perma)
            for uid, uname in entries:
                perma[uid] = {"username": uname}
            data["permanent_block"] = perma
            save_data()
            added = len(perma) - before

        admin_state = None
        update.message.reply_text(f"✅ Permanent block saved!\nNew Added: {added}\nTotal Blocked: {len(data.get('permanent_block',{}))}")
        return

    # UNBAN permanent
    if admin_state == "unban_permanent_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @name | id)")
            return
        with lock:
            perma = data.get("permanent_block", {}) or {}
            removed = 0
            for uid, _uname in entries:
                if uid in perma:
                    del perma[uid]
                    removed += 1
            data["permanent_block"] = perma
            save_data()
        admin_state = None
        update.message.reply_text(f"✅ Unbanned from Permanent Block!\nRemoved: {removed}")
        return

    # UNBAN oldwinner
    if admin_state == "unban_oldwinner_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @name | id)")
            return
        with lock:
            ow = data.get("old_winners", {}) or {}
            removed = 0
            for uid, _uname in entries:
                if uid in ow:
                    del ow[uid]
                    removed += 1
            data["old_winners"] = ow
            save_data()
        admin_state = None
        update.message.reply_text(f"✅ Unbanned from Old Winner Block!\nRemoved: {removed}")
        return


# =========================================================
# PHOTO HANDLER (for prize delivery proof)
# =========================================================
def admin_photo_handler(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    if admin_state != "prize_proof_collect":
        return

    photos = update.message.photo or []
    if not photos:
        return

    # best quality last
    file_id = photos[-1].file_id
    with lock:
        lst = data.get("pending_prize_photos", []) or []
        lst.append(file_id)
        data["pending_prize_photos"] = lst
        save_data()

    update.message.reply_text(
        "✅ Photo added successfully.\n"
        f"Total photos saved: {len(data.get('pending_prize_photos', []) or [])}\n\n"
        "Send more photos or type: DONE"
    )


# =========================================================
# CALLBACK HANDLER (GLOBAL GUARD + ALL BUTTONS)
# =========================================================
def cb_handler(update: Update, context: CallbackContext):
    global admin_state, data
    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)

    # ✅ GLOBAL BUTTON GUARD (except admin)
    if uid != str(ADMIN_ID):
        # permanent blocked => any button => permanent popup
        if uid in (data.get("permanent_block", {}) or {}):
            try:
                query.answer(popup_permanent_blocked(), show_alert=True)
            except Exception:
                pass
            return

        # old winner blocked only if mode=block and list contains uid
        if str(data.get("old_winner_mode", "skip")) == "block":
            if uid in (data.get("old_winners", {}) or {}):
                try:
                    query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception:
                    pass
                return

    # ---------------- Verify Add buttons ----------------
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

    # ---------------- Auto winner post on/off ----------------
    if qd in ("autopost_on", "autopost_off"):
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
            data["autowinnerpost"] = (qd == "autopost_on")
            save_data()
        try:
            query.edit_message_text(f"✅ Auto Winner Post is now: {'ON' if qd == 'autopost_on' else 'OFF'}")
        except Exception:
            pass
        return

    # ---------------- Preview approve/reject/edit ----------------
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
                    data["start_time"] = datetime.utcnow().timestamp()

                    # reset runtime items for new giveaway
                    data["participants"] = {}
                    data["winners"] = {}
                    data["pending_winners_text"] = ""
                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""
                    data["closed_message_id"] = None
                    data["winners_message_id"] = None
                    data["draw_progress_message_id"] = None
                    data["winners_post_time"] = None
                    data["claim_expires_at"] = None

                    save_data()

                start_live_countdown(context.job_queue)
                try:
                    query.edit_message_text("✅ Giveaway approved and posted to channel!")
                except Exception:
                    pass
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

    # ---------------- End giveaway confirm/cancel ----------------
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

        close_giveaway(context, auto_trigger=False)

        try:
            query.edit_message_text("✅ Giveaway Closed Successfully! Now use /draw")
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
        try:
            query.answer()
        except Exception:
            pass
        try:
            query.edit_message_text("❌ Cancelled. Giveaway is still running.")
        except Exception:
            pass
        return

    # ---------------- Reset confirm/reject ----------------
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

        # Start 40s reset progress (no number)
        try:
            query.edit_message_text(build_reset_progress_text(0, SPINNER[0], DOTS[0]))
        except Exception:
            pass
        start_reset_progress(context, admin_chat_id=query.message.chat_id, msg_id=query.message.message_id)
        return

    if qd == "reset_reject":
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
            query.edit_message_text("❌ Reset cancelled.\nNo data was removed.")
        except Exception:
            pass
        return

    # ---------------- Unban choose ----------------
    if qd == "unban_permanent":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        admin_state = "unban_permanent_input"
        try:
            query.answer()
        except Exception:
            pass
        try:
            query.edit_message_text("Send User ID list (one per line) to unban from Permanent Block:")
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
        admin_state = "unban_oldwinner_input"
        try:
            query.answer()
        except Exception:
            pass
        try:
            query.edit_message_text("Send User ID list (one per line) to unban from Old Winner Block:")
        except Exception:
            pass
        return

    # ---------------- Removeban choose / confirm ----------------
    if qd == "reset_permanent_ban":
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
            query.edit_message_text("Reset Permanent Ban List?", reply_markup=confirm_clear_list_markup("confirm_clear_permanent"))
        except Exception:
            pass
        return

    if qd == "reset_oldwinner_ban":
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
            query.edit_message_text("Reset Old Winner Ban List?", reply_markup=confirm_clear_list_markup("confirm_clear_oldwinner"))
        except Exception:
            pass
        return

    if qd == "cancel_clear_list":
        try:
            query.answer()
        except Exception:
            pass
        try:
            query.edit_message_text("❌ Cancelled.")
        except Exception:
            pass
        return

    if qd == "confirm_clear_permanent":
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
            query.edit_message_text("✅ Permanent Ban List cleared successfully!")
        except Exception:
            pass
        return

    if qd == "confirm_clear_oldwinner":
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
            query.edit_message_text("✅ Old Winner Ban List cleared successfully!")
        except Exception:
            pass
        return

    # ---------------- Join giveaway button ----------------
    if qd == "join_giveaway":
        # active check
        if not data.get("active"):
            try:
                query.answer("This giveaway is not active right now.", show_alert=True)
            except Exception:
                pass
            return

        # verify required
        if not verify_user_join(context.bot, int(uid)):
            try:
                query.answer(popup_verify_required(), show_alert=True)
            except Exception:
                pass
            return

        # old winner mode=block already handled by global guard (and also here safe)
        if str(data.get("old_winner_mode", "skip")) == "block":
            if uid in (data.get("old_winners", {}) or {}):
                try:
                    query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception:
                    pass
                return

        # first winner clicks again -> same popup always
        with lock:
            first_uid = data.get("first_winner_id")

        if first_uid and uid == str(first_uid):
            tg_user = query.from_user
            uname = user_tag(tg_user.username or "") or data.get("first_winner_username", "") or "@username"
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

        # success join (and assign first winner if none)
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

        # instant update live post participants count
        try:
            live_mid = data.get("live_message_id")
            start_ts = data.get("start_time")
            if live_mid and start_ts:
                start = datetime.utcfromtimestamp(start_ts)
                now = datetime.utcnow()
                duration = int(data.get("duration_seconds", 1) or 1)
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

        # popup: first winner or normal
        with lock:
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

    # ---------------- Winners approve/reject (manual) ----------------
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

        # delete CLOSED post before posting winners (your rule)
        closed_mid = data.get("closed_message_id")
        if closed_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception:
                pass
            with lock:
                data["closed_message_id"] = None
                save_data()

        post_winners_to_channel(context, text=text)
        try:
            query.edit_message_text("✅ Approved! Winners list posted to channel (with Claim button).")
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
            save_data()
        try:
            query.edit_message_text("❌ Rejected! Winners list will NOT be posted.")
        except Exception:
            pass
        return

    # ---------------- Claim prize ----------------
    if qd == "claim_prize":
        winners = data.get("winners", {}) or {}
        if uid in winners:
            # expiry check
            expires_at = data.get("claim_expires_at")
            if expires_at and datetime.utcnow().timestamp() > float(expires_at):
                try:
                    query.answer(popup_claim_expired(), show_alert=True)
                except Exception:
                    pass
                return

            uname = winners.get(uid, {}).get("username", "") or "@username"
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

    # ---------------- Prize delivery proof approve/reject ----------------
    if qd == "prizeproof_approve":
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
            photos = data.get("pending_prize_photos", []) or []
            caption = data.get("pending_prize_caption", "") or build_prize_delivery_caption()

        if not photos:
            try:
                query.edit_message_text("No photos saved. Start again: /prizedeliveryprove")
            except Exception:
                pass
            return

        # send album to channel with caption in first item
        media = []
        for i, fid in enumerate(photos):
            if i == 0:
                media.append(InputMediaPhoto(media=fid, caption=caption))
            else:
                media.append(InputMediaPhoto(media=fid))

        try:
            context.bot.send_media_group(chat_id=CHANNEL_ID, media=media)
            with lock:
                data["pending_prize_photos"] = []
                data["pending_prize_caption"] = ""
                save_data()
            try:
                query.edit_message_text("✅ Approved! Prize delivery proof posted to channel.")
            except Exception:
                pass
        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post album: {e}")
            except Exception:
                pass
        return

    if qd == "prizeproof_reject":
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
            data["pending_prize_photos"] = []
            data["pending_prize_caption"] = ""
            save_data()
        try:
            query.edit_message_text("❌ Rejected. Nothing was posted.")
        except Exception:
            pass
        return

    # ---------------- completePrize approve/reject ----------------
    if qd == "completeprize_approve":
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
            context.bot.send_message(chat_id=CHANNEL_ID, text=build_complete_prize_text())
            try:
                query.edit_message_text("✅ Approved! Prize completion post sent to channel.")
            except Exception:
                pass
        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post in channel: {e}")
            except Exception:
                pass
        return

    if qd == "completeprize_reject":
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
            query.edit_message_text("❌ Rejected. Nothing was posted.")
        except Exception:
            pass
        return

    # default answer
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
    if ADMIN_ID == 0 or CHANNEL_ID == 0:
        raise SystemExit("ADMIN_ID / CHANNEL_ID missing in .env")

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # core
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("panel", cmd_panel))

    # giveaway
    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))

    # auto
    dp.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))

    # verify
    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    # bans
    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("blockoldwinner", cmd_blockoldwinner))
    dp.add_handler(CommandHandler("oldwinnerblock", cmd_oldwinnerblock))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))

    # winners
    dp.add_handler(CommandHandler("winnerlist", cmd_winnerlist))
    dp.add_handler(CommandHandler("completePrize", cmd_completePrize))

    # proof
    dp.add_handler(CommandHandler("prizedeliveryprove", cmd_prizedeliveryprove))

    # reset
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # admin flows
    dp.add_handler(MessageHandler(Filters.photo, admin_photo_handler))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # resume live giveaway after restart
    if data.get("active"):
        start_live_countdown(updater.job_queue)
    if data.get("closed") and data.get("closed_message_id") and not data.get("winners_message_id"):
        start_closed_dots(updater.job_queue)

    print("Bot is running (PTB 13, non-async) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
