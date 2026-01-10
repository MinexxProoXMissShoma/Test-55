# ============================================================
# POWER POINT BREAK — Giveaway Bot (PTB 13.x, Non-Async)
# FULL A–Z FINAL | English Only | UserID-based (username optional)
# ------------------------------------------------------------
# Features:
# ✅ New giveaway wizard (/newgiveaway) -> Preview -> Approve -> Channel live post
# ✅ Join button (UserID based), First-Join Champion, Duplicate entry blocked
# ✅ Verify system (global): /addverifylink /removeverifylink (checked on JOIN + CLAIM)
# ✅ Permanent block (global): /blockpermanent + /unban + /removeban
# ✅ Old winner block (global): /blockoldwinner (ON/OFF + add list) + unban/reset list
# ✅ AutoWinnerPost (global): /autowinnerpost ON/OFF
#    - ON: giveaway ends -> channel shows 3-minute live spinner/progress (5s updates) -> auto post winners
#    - OFF: giveaway ends -> channel shows CLOSED spinner (5s updates) -> admin uses /draw
# ✅ Manual draw (/draw): 40-second progress (5s updates) -> preview -> Approve/Reject -> posts winners
# ✅ Winners post includes Prize, Winner Count, Prize delivery X/Y
# ✅ Claim button unique per giveaway, supports many claim buttons across channel
# ✅ /prizedelivery: add delivered winners -> edits SAME winners post -> updates delivery count
# ✅ Delivered winner clicking claim sees "Already delivered" popup
# ✅ Claim expires after 24h -> bot removes claim button automatically (best effort)
# ✅ /reset = FACTORY RESET (ALL data + ALL settings + deletes bot posts best effort)
#
# NOTE:
# - For PERFECT line/border stability on Telegram, channel posts use HTML <pre> monospace.
# - Telegram's channel header (channel name shown above post) cannot be removed by any bot.
# ============================================================

import os
import json
import random
import threading
import html
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
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/PowerPointBreak")
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@MinexxProo")
DATA_FILE = os.getenv("DATA_FILE", "giveaway_data.json")

# =========================================================
# THREAD SAFE STORAGE
# =========================================================
lock = threading.RLock()

# =========================================================
# GLOBAL STATE (admin wizard state)
# =========================================================
admin_state = None

# Repeating jobs
countdown_job = None          # live giveaway countdown updater (current giveaway)
closed_spin_job = None        # closed post spinner (when auto winner OFF)
channel_autodraw_job = None   # 3-min channel progress (when auto winner ON)
admin_draw_job = None         # 40s admin progress
admin_draw_finalize_job = None

SPINNER = ["🔄", "🔃", "🔁", "🔂"]

# =========================================================
# BASIC HELPERS
# =========================================================
def now_ts() -> float:
    return datetime.utcnow().timestamp()

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

def format_hms(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

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

def build_progress(percent: int, blocks: int = 10) -> str:
    percent = max(0, min(100, int(percent)))
    filled = int(round(blocks * percent / 100.0))
    empty = blocks - filled
    return "█" * filled + "░" * empty

def safe_line(n: int = 30) -> str:
    return "━" * n

def new_giveaway_id() -> str:
    # unique, sortable id
    return datetime.utcnow().strftime("%Y%m%d%H%M%S%f")

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
# TELEGRAM SAFE CHANNEL TEXT (NO BROKEN LINES)
# =========================================================
def tg_pre(text: str) -> str:
    return "<pre>" + html.escape(text or "") + "</pre>"

def ch_send(bot, text: str, reply_markup=None):
    return bot.send_message(
        chat_id=CHANNEL_ID,
        text=tg_pre(text),
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )

def ch_edit(bot, mid: int, text: str, reply_markup=None):
    return bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=mid,
        text=tg_pre(text),
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )

# =========================================================
# DATA MODEL
# =========================================================
def fresh_giveaway():
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
        "winners_message_id": None,

        # participants (uid -> {"username": "", "name": ""})
        "participants": {},

        # first join
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # winners (uid -> {"username": ""})
        "winners": {},
        "pending_winners_text": "",

        # claim window
        "claim_start_ts": None,
        "claim_expires_ts": None,

        # prize delivery (uid -> {"username": ""})
        "delivered": {},
    }

def fresh_default_data():
    # FULL factory default (reset clears EVERYTHING back to this)
    return {
        "current_id": None,
        "giveaways": {},

        # verify targets (global)
        "verify_targets": [],

        # blocks (global)
        "permanent_block": {},

        "old_winner_block_enabled": False,
        "old_winner_block": {},

        # auto winner (global)
        "auto_winner_post": False,
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
    # ensure types
    d.setdefault("giveaways", {})
    return d

def save_data():
    with lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

data = load_data()

def get_current_giveaway():
    gid = data.get("current_id")
    if not gid:
        return None, None
    gw = (data.get("giveaways") or {}).get(gid)
    return gid, gw

def get_giveaway(gid: str):
    return (data.get("giveaways") or {}).get(gid)

def participants_count(gw: dict) -> int:
    return len((gw or {}).get("participants", {}) or {})

# =========================================================
# VERIFY SYSTEM
# =========================================================
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
# POPUPS (ENGLISH ONLY)
# =========================================================
def popup_verify_required() -> str:
    return (
        "🔐 Access Restricted\n"
        "You must join the required channels to proceed.\n"
        "After joining, tap JOIN once more."
    )

def popup_permanent_blocked() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n"
        "You are permanently blocked from joining giveaways.\n"
        f"If you believe this is a mistake, contact admin: {ADMIN_CONTACT}"
    )

def popup_old_winner_blocked() -> str:
    return (
        "🚫 Access Denied\n"
        "You are blocked by Old Winner protection.\n"
        "Please wait for the next giveaway."
    )

def popup_first_winner(username: str, uid: str) -> str:
    return (
        "✨ CONGRATULATIONS 🌟\n"
        "You joined FIRST and secured the 🥇 1st Winner Spot!\n"
        f"👑 {username} | {uid}\n"
        "Take a screenshot & Post in the group to confirm your win 👈"
    )

def popup_already_joined() -> str:
    return (
        "❌ ENTRY Unsuccessful\n"
        "You’ve already joined\n"
        "this giveaway 🫵\n\n"
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

def popup_claim_not_winner() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "❌ NOT A WINNER\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Sorry! Your User ID is not in the winners list.\n"
        "Please wait for the next giveaway 🤍"
    )

def popup_prize_expired() -> str:
    return (
        "⏳ PRIZE EXPIRED\n"
        "Your 24-hour claim time has ended.\n"
        "This prize is no longer available."
    )

def popup_claim_winner(title: str, prize: str, username: str, uid: str) -> str:
    return (
        "🌟 Congratulations ✨\n"
        "You’ve won this giveaway ✅\n\n"
        f"🎯 Giveaway: {title}\n"
        f"🎁 Prize: {prize}\n\n"
        f"👤 {username} | 🆔 {uid}\n\n"
        "📩 Please contact admin to claim your prize:\n"
        f"👉 {ADMIN_CONTACT}"
    )

def popup_prize_already_delivered() -> str:
    return (
        "🌟 Congratulations!\n"
        "Your prize has already been successfully delivered to you ✅\n"
        f"If you face any issues, please contact our admin 📩 {ADMIN_CONTACT}"
    )

# =========================================================
# BUTTONS (Unique per giveaway id)
# =========================================================
def join_button_markup(gid: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data=f"join:{gid}")]]
    )

def claim_button_markup(gid: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data=f"claim:{gid}")]]
    )

def winners_approve_markup(gid: str):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Post", callback_data=f"wapprove:{gid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"wreject:{gid}"),
        ]]
    )

def preview_markup(gid: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✔️ Approve & Post", callback_data=f"papprove:{gid}"),
                InlineKeyboardButton("❌ Reject Giveaway", callback_data=f"preject:{gid}"),
            ],
            [InlineKeyboardButton("✏️ Edit Again", callback_data="pedit")],
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

def toggle_onoff_markup(on_cb: str, off_cb: str, is_on: bool):
    if is_on:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ ON (Enabled)", callback_data="noop"),
              InlineKeyboardButton("Turn OFF", callback_data=off_cb)]]
        )
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Turn ON", callback_data=on_cb),
          InlineKeyboardButton("❌ OFF (Disabled)", callback_data="noop")]]
    )

# =========================================================
# TEXT BUILDERS
# =========================================================
def format_rules_text(rules: str) -> str:
    r = (rules or "").strip()
    if not r:
        return (
            "✅ Must join official channel\n"
            "❌ One account per user\n"
            "🚫 No fake / duplicate accounts"
        )
    lines = [x.strip() for x in r.splitlines() if x.strip()]
    return "\n".join([f"✅ {x}" for x in lines])

def build_preview_text(gw: dict) -> str:
    remaining = int(gw.get("duration_seconds", 0) or 0)
    title = (gw.get("title") or "").strip()
    prize = (gw.get("prize") or "").rstrip()
    wc = int(gw.get("winner_count", 0) or 0)
    rules = format_rules_text(gw.get("rules", ""))

    return (
        f"{safe_line()}\n"
        "🔍 GIVEAWAY PREVIEW (ADMIN ONLY)\n"
        f"{safe_line()}\n\n"
        f"⚡ {title} ⚡\n\n"
        "🎁 PRIZE:\n"
        f"{prize}\n\n"
        f"Winner Count: {wc}\n"
        "Total Participants: 0\n\n"
        "⏳ TIME REMAINING\n"
        f"{format_hms(remaining)}\n"
        "📊 LIVE PROGRESS\n"
        f"{build_progress(0)} 0%\n\n"
        "📜 RULES\n"
        f"{rules}\n\n"
        f"📢 Hosted By: {HOST_NAME}\n\n"
        "👇 TAP THE BUTTON BELOW & JOIN NOW 👇"
    )

def build_live_text(gw: dict, remaining: int) -> str:
    duration = int(gw.get("duration_seconds", 1) or 1)
    elapsed = max(0, duration - remaining)
    percent = int(round((elapsed / float(duration)) * 100))
    bar = build_progress(percent)

    title = (gw.get("title") or "").strip()
    prize = (gw.get("prize") or "").rstrip()
    wc = int(gw.get("winner_count", 0) or 0)
    rules = format_rules_text(gw.get("rules", ""))

    return (
        f"{safe_line()}\n"
        f"⚡ {title} ⚡\n"
        f"{safe_line()}\n\n"
        "🎁 PRIZE POOL ✨\n"
        f"{prize}\n\n"
        f"👥 TOTAL PARTICIPANTS: {participants_count(gw)}\n"
        f"🏅 TOTAL WINNERS: {wc}\n"
        "🎯 WINNER SELECTION: 100% Randomly\n\n"
        "⏳ TIME REMAINING\n"
        f"{format_hms(remaining).replace(':', ' : ')}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{bar} {percent}%\n\n"
        "📜 RULES\n"
        f"{rules}\n\n"
        f"📢 HOSTED BY ⚡ {HOST_NAME}\n\n"
        "👇 TAP THE BUTTON BELOW & JOIN NOW 👇"
    )

def build_closed_text(gw: dict, spin: str) -> str:
    title = (gw.get("title") or "").strip()
    wc = int(gw.get("winner_count", 0) or 0)
    pcount = participants_count(gw)

    line_spin = ""
    if spin:
        line_spin = f"{spin} Winner selection status: waiting...\n\n"

    return (
        f"{safe_line()}\n"
        "🚫 GIVEAWAY OFFICIALLY CLOSED 🚫\n"
        f"{safe_line()}\n\n"
        f"🎯 Giveaway: {title}\n\n"
        "⏰ The giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {pcount}\n"
        f"🏆 Total Winners: {wc}\n\n"
        f"{line_spin}"
        "🙏 Thank you to everyone who participated.\n"
        f"— {HOST_NAME} ⚡"
    )

def build_channel_autodraw_progress(gw: dict, percent: int, spin: str) -> str:
    title = (gw.get("title") or "").strip()
    bar = build_progress(percent)
    return (
        f"{safe_line()}\n"
        "🎲 RANDOM WINNER SELECTION\n"
        f"{safe_line()}\n\n"
        f"🎯 Giveaway: {title}\n\n"
        f"{spin} Winner selection is in progress...\n\n"
        "📊 LIVE PROGRESS\n"
        f"{bar} {percent}%\n\n"
        "✅ 100% Random & Fair\n"
        "🔐 User ID based selection only.\n\n"
        f"— {HOST_NAME} ⚡"
    )

def build_winners_post_text(gw: dict, first_uid: str, first_user: str, random_winners: list) -> str:
    title = (gw.get("title") or "").strip()
    prize = (gw.get("prize") or "").rstrip()
    wc = int(gw.get("winner_count", 0) or 0)

    delivered_map = gw.get("delivered", {}) or {}
    delivered_count = len(delivered_map)

    lines = []
    lines.append("🏆 GIVEAWAY WINNERS ANNOUNCEMENT 🏆")
    lines.append("")
    lines.append(f"⚡ {title} ⚡")
    lines.append("")
    lines.append("🎁 PRIZE:")
    lines.append(prize if prize else "N/A")
    lines.append(f"Winner Count: {wc}")
    lines.append(f"Prize delivery: {delivered_count}/{wc}")
    lines.append("")
    lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
    if first_user:
        lines.append(f"👑 {first_user}")
        lines.append(f"🆔 {first_uid}")
    else:
        lines.append("👑 User ID")
        lines.append(f"🆔 {first_uid}")
    lines.append("🎯 Secured instantly by joining first")
    lines.append("")
    lines.append("👑 OTHER WINNERS (RANDOMLY SELECTED)")
    i = 1
    for uid, uname in random_winners:
        if uname:
            lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}")
        else:
            lines.append(f"{i}️⃣ 👤 User ID: {uid}")
        i += 1

    lines.append("")
    lines.append("⏳ Claim Rule:")
    lines.append("Prizes must be claimed within 24 hours.")
    lines.append("After 24 hours, claim will expire.")
    lines.append("")
    lines.append(f"📢 Hosted By: {HOST_NAME}")
    lines.append("👇 Click the button below to claim your prize")

    return "\n".join(lines)

# =========================================================
# WINNER SELECTION (UserID-based)
# =========================================================
def select_winners(gw: dict):
    participants = (gw.get("participants") or {})
    if not participants:
        return None

    winner_count = max(1, int(gw.get("winner_count", 1) or 1))

    first_uid = gw.get("first_winner_id")
    if not first_uid:
        first_uid = next(iter(participants.keys()))
        info = participants.get(first_uid, {}) or {}
        gw["first_winner_id"] = first_uid
        gw["first_winner_username"] = info.get("username", "")
        gw["first_winner_name"] = info.get("name", "")

    first_uname = gw.get("first_winner_username", "")
    if not first_uname:
        first_uname = (participants.get(first_uid, {}) or {}).get("username", "")

    pool = [uid for uid in participants.keys() if uid != first_uid]
    remaining_needed = max(0, winner_count - 1)
    remaining_needed = min(remaining_needed, len(pool))
    selected = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

    winners_map = {str(first_uid): {"username": first_uname}}
    random_list = []
    for uid in selected:
        info = participants.get(uid, {}) or {}
        winners_map[str(uid)] = {"username": info.get("username", "")}
        random_list.append((str(uid), info.get("username", "")))

    gw["winners"] = winners_map
    text = build_winners_post_text(gw, str(first_uid), first_uname, random_list)
    gw["pending_winners_text"] = text
    return text

# =========================================================
# JOB UTILITIES
# =========================================================
def stop_job(job_ref_name: str):
    global countdown_job, closed_spin_job, channel_autodraw_job, admin_draw_job, admin_draw_finalize_job
    job = globals().get(job_ref_name)
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:
            pass
    globals()[job_ref_name] = None

def stop_all_jobs(job_queue=None):
    stop_job("countdown_job")
    stop_job("closed_spin_job")
    stop_job("channel_autodraw_job")
    stop_job("admin_draw_job")
    stop_job("admin_draw_finalize_job")
    # Also remove any claim-expire jobs
    if job_queue is not None:
        try:
            jobs = job_queue.get_jobs_by_name("claim_expire")
            for j in jobs:
                try:
                    j.schedule_removal()
                except Exception:
                    pass
        except Exception:
            pass

# =========================================================
# LIVE COUNTDOWN (Channel live post updated every 5 sec)
# =========================================================
def start_live_countdown(job_queue):
    global countdown_job
    stop_job("countdown_job")
    countdown_job = job_queue.run_repeating(live_tick, interval=5, first=0, name="live_countdown")

def live_tick(context: CallbackContext):
    with lock:
        gid, gw = get_current_giveaway()
        if not gid or not gw or not gw.get("active"):
            stop_job("countdown_job")
            return

        start_time = gw.get("start_time")
        if start_time is None:
            gw["start_time"] = now_ts()
            save_data()
            start_time = gw["start_time"]

        start = datetime.utcfromtimestamp(start_time)
        duration = int(gw.get("duration_seconds", 1) or 1)
        elapsed = int((datetime.utcnow() - start).total_seconds())
        remaining = duration - elapsed

        live_mid = gw.get("live_message_id")

        if remaining <= 0:
            # close giveaway automatically
            gw["active"] = False
            gw["closed"] = True
            save_data()

            # delete live message
            if live_mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
                except Exception:
                    pass

            # post closed message
            try:
                spin = "" if data.get("auto_winner_post") else SPINNER[0]
                m = ch_send(context.bot, build_closed_text(gw, spin))
                gw["closed_message_id"] = m.message_id
                save_data()
            except Exception:
                pass

            # Auto ON -> channel auto draw (3 minutes)
            if data.get("auto_winner_post"):
                start_channel_autodraw(context.job_queue, gid)
            else:
                # Auto OFF -> closed spinner
                start_closed_spinner(context.job_queue, gid)

            # notify admin
            try:
                context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "⏰ Giveaway Closed!\n\n"
                        f"Giveaway: {gw.get('title','')}\n"
                        f"Total Participants: {participants_count(gw)}\n\n"
                        + ("Auto Winner Post: ON (Channel auto draw running)" if data.get("auto_winner_post") else "Auto Winner Post: OFF (Use /draw)")
                    ),
                )
            except Exception:
                pass

            stop_job("countdown_job")
            return

        if not live_mid:
            return

        try:
            ch_edit(
                context.bot,
                live_mid,
                build_live_text(gw, remaining),
                reply_markup=join_button_markup(gid),
            )
        except Exception:
            pass

# =========================================================
# CLOSED SPINNER (Only when AutoWinnerPost OFF)
# =========================================================
def start_closed_spinner(job_queue, gid: str):
    global closed_spin_job
    stop_job("closed_spin_job")
    closed_spin_job = job_queue.run_repeating(
        closed_spin_tick,
        interval=5,
        first=0,
        context={"gid": gid, "tick": 0},
        name="closed_spinner",
    )

def closed_spin_tick(context: CallbackContext):
    ctx = context.job.context or {}
    gid = ctx.get("gid")
    tick = int(ctx.get("tick", 0)) + 1
    ctx["tick"] = tick
    context.job.context = ctx

    gw = get_giveaway(gid)
    if not gw:
        stop_job("closed_spin_job")
        return

    # stop if winners already posted OR auto winner enabled
    if gw.get("winners_message_id") or data.get("auto_winner_post"):
        stop_job("closed_spin_job")
        return

    mid = gw.get("closed_message_id")
    if not mid:
        stop_job("closed_spin_job")
        return

    spin = SPINNER[(tick - 1) % len(SPINNER)]
    try:
        ch_edit(context.bot, mid, build_closed_text(gw, spin))
    except Exception:
        pass

# =========================================================
# CHANNEL AUTO DRAW (Only when AutoWinnerPost ON)
# 3 minutes progress, update every 5 seconds, then auto post winners
# =========================================================
AUTO_DRAW_TOTAL = 180
AUTO_DRAW_INTERVAL = 5

def start_channel_autodraw(job_queue, gid: str):
    global channel_autodraw_job
    stop_job("channel_autodraw_job")
    channel_autodraw_job = job_queue.run_repeating(
        channel_autodraw_tick,
        interval=AUTO_DRAW_INTERVAL,
        first=0,
        context={"gid": gid, "start_ts": now_ts(), "tick": 0},
        name="channel_autodraw",
    )

def channel_autodraw_tick(context: CallbackContext):
    ctx = context.job.context or {}
    gid = ctx.get("gid")
    start_ts = float(ctx.get("start_ts", now_ts()))
    tick = int(ctx.get("tick", 0)) + 1
    ctx["tick"] = tick
    context.job.context = ctx

    gw = get_giveaway(gid)
    if not gw:
        stop_job("channel_autodraw_job")
        return

    # If winners already posted, stop
    if gw.get("winners_message_id"):
        stop_job("channel_autodraw_job")
        return

    mid = gw.get("closed_message_id")
    if not mid:
        stop_job("channel_autodraw_job")
        return

    elapsed = max(0.0, now_ts() - start_ts)
    percent = int(round(min(100, (elapsed / float(AUTO_DRAW_TOTAL)) * 100)))
    spin = SPINNER[(tick - 1) % len(SPINNER)]

    # edit selection progress (no winner numbers during progress)
    try:
        ch_edit(context.bot, mid, build_channel_autodraw_progress(gw, percent, spin))
    except Exception:
        pass

    # finalize at 100%
    if elapsed >= AUTO_DRAW_TOTAL:
        with lock:
            if not gw.get("participants"):
                stop_job("channel_autodraw_job")
                return
            text = select_winners(gw)
            save_data()

        if not text:
            stop_job("channel_autodraw_job")
            return

        # delete closed/progress message
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
        except Exception:
            pass

        # post winners
        try:
            m = ch_send(context.bot, text, reply_markup=claim_button_markup(gid))
            with lock:
                gw["winners_message_id"] = m.message_id
                gw["closed_message_id"] = None

                ts = now_ts()
                gw["claim_start_ts"] = ts
                gw["claim_expires_ts"] = ts + 24 * 3600
                save_data()

            # schedule claim button removal
            schedule_claim_expire(context.job_queue, gid)
        except Exception:
            pass

        stop_job("channel_autodraw_job")

# =========================================================
# ADMIN DRAW (Manual /draw) 40s progress (every 5 sec)
# =========================================================
ADMIN_DRAW_TOTAL = 40
ADMIN_DRAW_INTERVAL = 5

def start_admin_draw_progress(context: CallbackContext, admin_chat_id: int, gid: str):
    global admin_draw_job, admin_draw_finalize_job
    stop_job("admin_draw_job")
    stop_job("admin_draw_finalize_job")

    gw = get_giveaway(gid) or fresh_giveaway()
    msg = context.bot.send_message(
        chat_id=admin_chat_id,
        text=tg_pre(build_channel_autodraw_progress(gw, 0, SPINNER[0])),
        parse_mode="HTML"
    )

    ctx = {
        "gid": gid,
        "admin_chat_id": admin_chat_id,
        "admin_msg_id": msg.message_id,
        "start_ts": now_ts(),
        "tick": 0,
    }

    def draw_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        tick = int(jd.get("tick", 0)) + 1
        jd["tick"] = tick

        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(ADMIN_DRAW_TOTAL)) * 100)))
        spin = SPINNER[(tick - 1) % len(SPINNER)]

        gwx = get_giveaway(jd["gid"]) or fresh_giveaway()
        txt = build_channel_autodraw_progress(gwx, percent, spin)

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["admin_chat_id"],
                message_id=jd["admin_msg_id"],
                text=tg_pre(txt),
                parse_mode="HTML",
            )
        except Exception:
            pass

    admin_draw_job = context.job_queue.run_repeating(
        draw_tick,
        interval=ADMIN_DRAW_INTERVAL,
        first=0,
        context=ctx,
        name="admin_draw_progress",
    )

    admin_draw_finalize_job = context.job_queue.run_once(
        admin_draw_finalize,
        when=ADMIN_DRAW_TOTAL,
        context=ctx,
        name="admin_draw_finalize",
    )

def admin_draw_finalize(context: CallbackContext):
    stop_job("admin_draw_job")
    stop_job("admin_draw_finalize_job")

    jd = context.job.context
    gid = jd["gid"]
    admin_chat_id = jd["admin_chat_id"]
    admin_msg_id = jd["admin_msg_id"]

    with lock:
        gw = get_giveaway(gid)
        if not gw:
            return
        if not gw.get("participants"):
            try:
                context.bot.edit_message_text(
                    chat_id=admin_chat_id,
                    message_id=admin_msg_id,
                    text="No participants to draw winners from."
                )
            except Exception:
                pass
            return

        text = select_winners(gw)
        save_data()

    if not text:
        return

    # show preview + approve/reject
    try:
        context.bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            text=tg_pre(text),
            parse_mode="HTML",
            reply_markup=winners_approve_markup(gid),
        )
    except Exception:
        context.bot.send_message(
            chat_id=admin_chat_id,
            text=tg_pre(text),
            parse_mode="HTML",
            reply_markup=winners_approve_markup(gid),
        )

# =========================================================
# CLAIM BUTTON EXPIRY (24h -> remove claim button)
# =========================================================
def schedule_claim_expire(job_queue, gid: str):
    gw = get_giveaway(gid)
    if not gw:
        return
    mid = gw.get("winners_message_id")
    exp = gw.get("claim_expires_ts")
    if not mid or not exp:
        return

    remain = float(exp) - now_ts()
    if remain <= 0:
        # already expired: remove button best effort
        try:
            job_queue.run_once(expire_claim_button_job, when=1, context={"gid": gid}, name="claim_expire")
        except Exception:
            pass
        return

    # schedule a per-giveaway expiry job (same name; PTB allows multiple jobs with same name)
    job_queue.run_once(
        expire_claim_button_job,
        when=remain,
        context={"gid": gid},
        name="claim_expire",
    )

def expire_claim_button_job(context: CallbackContext):
    gid = (context.job.context or {}).get("gid")
    gw = get_giveaway(gid) if gid else None
    if not gw:
        return
    mid = gw.get("winners_message_id")
    if not mid:
        return
    try:
        context.bot.edit_message_reply_markup(
            chat_id=CHANNEL_ID,
            message_id=mid,
            reply_markup=None
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
            "🛡️ ADMIN PANEL READY ✅\n\n"
            "Use /panel to open controls."
        )
    else:
        update.message.reply_text(
            "Giveaway System is running.\n"
            f"Please join our channel and wait for giveaway posts:\n{CHANNEL_LINK}"
        )

def cmd_panel(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "🛠 ADMIN CONTROL PANEL\n\n"
        "📌 GIVEAWAY\n"
        "/newgiveaway\n"
        "/participants\n"
        "/draw\n"
        "/endgiveaway\n\n"
        "⚙️ SYSTEM\n"
        "/autowinnerpost\n"
        "/blockoldwinner\n"
        "/prizedelivery\n\n"
        "✅ VERIFY\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "🔒 BAN\n"
        "/blockpermanent\n"
        "/unban\n"
        "/blocklist\n"
        "/removeban\n\n"
        "♻️ RESET\n"
        "/reset"
    )

# ---------- Verify ----------
def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    update.message.reply_text(
        "✅ ADD VERIFY TARGET\n\n"
        "Send Chat ID (recommended) OR @username:\n\n"
        "Examples:\n"
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

    lines = ["🗑 REMOVE VERIFY TARGET\n", "Current targets:\n"]
    for i, t in enumerate(targets, start=1):
        lines.append(f"{i}) {t.get('display','')}")
    lines.append("\nSend a number to remove.\n")
    lines.append("99) Remove ALL verify targets")
    admin_state = "remove_verify_pick"
    update.message.reply_text("\n".join(lines))

# ---------- New Giveaway Setup ----------
def cmd_newgiveaway(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return

    # stop running jobs (not claim expiry jobs; they can remain for old winners posts)
    stop_job("countdown_job")
    stop_job("closed_spin_job")
    stop_job("channel_autodraw_job")
    stop_job("admin_draw_job")
    stop_job("admin_draw_finalize_job")

    with lock:
        gid = new_giveaway_id()
        gw = fresh_giveaway()
        data["giveaways"][gid] = gw
        data["current_id"] = gid
        save_data()

    admin_state = "title"
    update.message.reply_text(
        "🆕 NEW GIVEAWAY SETUP\n\n"
        "STEP 1 — Send Giveaway Title:"
    )

def cmd_participants(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    gid, gw = get_current_giveaway()
    if not gid or not gw:
        update.message.reply_text("No giveaway found.")
        return
    parts = gw.get("participants", {}) or {}
    if not parts:
        update.message.reply_text("Participants list is empty.")
        return

    lines = [f"👥 PARTICIPANTS (Total: {len(parts)})\n"]
    i = 1
    for uid, info in parts.items():
        uname = (info or {}).get("username", "")
        if uname:
            lines.append(f"{i}. {uname} | {uid}")
        else:
            lines.append(f"{i}. {uid}")
        i += 1
    update.message.reply_text("\n".join(lines))

def cmd_endgiveaway(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    gid, gw = get_current_giveaway()
    if not gid or not gw or not gw.get("active"):
        update.message.reply_text("No active giveaway is running.")
        return
    update.message.reply_text(
        "⚠️ END GIVEAWAY CONFIRMATION\n\n"
        "Are you sure you want to end now?",
        reply_markup=end_confirm_markup()
    )

def cmd_draw(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    gid, gw = get_current_giveaway()
    if not gid or not gw:
        update.message.reply_text("No giveaway found.")
        return
    if not gw.get("closed"):
        update.message.reply_text("Giveaway is not closed yet.")
        return
    if gw.get("winners_message_id"):
        update.message.reply_text("Winners already posted.")
        return
    if not gw.get("participants"):
        update.message.reply_text("No participants to draw winners from.")
        return
    start_admin_draw_progress(context, update.effective_chat.id, gid)

# ---------- Permanent Block ----------
def cmd_blockpermanent(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "perma_block_list"
    update.message.reply_text(
        "🔒 PERMANENT BLOCK\n\n"
        "Send list (one per line):\n"
        "User ID only OR username + id\n\n"
        "Examples:\n"
        "7297292\n"
        "@MinexxProo | 7297292"
    )

def cmd_blocklist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    perma = data.get("permanent_block", {}) or {}
    oldw = data.get("old_winner_block", {}) or {}
    ow_on = data.get("old_winner_block_enabled", False)
    aw_on = data.get("auto_winner_post", False)

    lines = []
    lines.append("📌 SYSTEM STATUS")
    lines.append("")
    lines.append(f"Auto Winner Post: {'ON' if aw_on else 'OFF'}")
    lines.append(f"Old Winner Block: {'ON' if ow_on else 'OFF'}")
    lines.append("")
    lines.append(f"Permanent Blocked: {len(perma)}")
    lines.append(f"Old Winner Blocked: {len(oldw)}")

    update.message.reply_text("\n".join(lines))

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

# ---------- Old Winner Block Toggle + Add list ----------
def cmd_blockoldwinner(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    is_on = bool(data.get("old_winner_block_enabled", False))
    update.message.reply_text(
        "🔐 OLD WINNER BLOCK SYSTEM\n\n"
        "If ON: old winners cannot JOIN (popup will show).\n"
        "Username is optional. UserID is required.\n\n"
        "Use buttons:",
        reply_markup=toggle_onoff_markup("ow_on", "ow_off", is_on)
    )

# ---------- Auto Winner Post Toggle ----------
def cmd_autowinnerpost(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    is_on = bool(data.get("auto_winner_post", False))
    update.message.reply_text(
        "⚙️ AUTO WINNER POST\n\n"
        "If ON: when giveaway ends, bot will auto select winners in channel\n"
        "with 3 minutes progress (updates every 5 seconds), then post winners.\n\n"
        "If OFF: bot posts CLOSED spinner (updates every 5 seconds), and you use /draw.\n\n"
        "Use buttons:",
        reply_markup=toggle_onoff_markup("aw_on", "aw_off", is_on)
    )

# ---------- Prize Delivery ----------
def cmd_prizedelivery(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return

    # choose latest giveaway which has winners_message_id
    giveaways = data.get("giveaways", {}) or {}
    latest_gid = None
    latest_ts = -1
    for gid, gw in giveaways.items():
        mid = (gw or {}).get("winners_message_id")
        ts = float((gw or {}).get("claim_start_ts") or 0)
        if mid and ts > latest_ts:
            latest_ts = ts
            latest_gid = gid

    if not latest_gid:
        update.message.reply_text("No winners post found in channel yet.")
        return

    context.user_data["prize_delivery_gid"] = latest_gid
    admin_state = "prize_delivery_list"
    update.message.reply_text(
        "✅ PRIZE DELIVERY\n\n"
        "Send delivered winners list (one per line):\n\n"
        "Format:\n"
        "@username | user_id\n"
        "OR\n"
        "user_id\n\n"
        "Example:\n"
        "@minexxproo | 8293728\n"
        "6953353566"
    )

# ---------- RESET (Factory Reset ALL ALL ALL) ----------
def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "⚠️ FULL FACTORY RESET\n\n"
        "This will DELETE EVERYTHING:\n"
        "- All giveaways\n"
        "- All participants\n"
        "- All winners & deliveries\n"
        "- All verify targets\n"
        "- All blocks (permanent + old winners)\n"
        "- Auto settings\n\n"
        "Are you sure?",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ YES, RESET ALL", callback_data="reset_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel"),
            ]]
        )
    )

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

    # ---------- Add Verify ----------
    if admin_state == "add_verify":
        ref = normalize_verify_ref(msg)
        if not ref:
            update.message.reply_text("Invalid input. Send Chat ID like -100123... or @username.")
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
            f"✅ VERIFY TARGET ADDED: {ref}\n"
            f"Total Targets: {len(data.get('verify_targets', []) or [])}",
            reply_markup=verify_add_more_done_markup()
        )
        return

    # ---------- Remove Verify ----------
    if admin_state == "remove_verify_pick":
        if not msg.isdigit():
            update.message.reply_text("Send a valid number (1,2,3... or 99).")
            return
        n = int(msg)
        with lock:
            targets = data.get("verify_targets", []) or []
            if not targets:
                admin_state = None
                update.message.reply_text("No verify targets remain.")
                return
            if n == 99:
                data["verify_targets"] = []
                save_data()
                admin_state = None
                update.message.reply_text("✅ All verify targets removed.")
                return
            if n < 1 or n > len(targets):
                update.message.reply_text("Invalid number. Try again.")
                return
            removed = targets.pop(n - 1)
            data["verify_targets"] = targets
            save_data()
        admin_state = None
        update.message.reply_text(f"✅ Removed: {removed.get('display','')}")
        return

    # ---------- Permanent Block list ----------
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
        admin_state = None
        update.message.reply_text(
            "✅ Permanent block saved!\n"
            f"New Added: {len(data['permanent_block']) - before}\n"
            f"Total Blocked: {len(data['permanent_block'])}"
        )
        return

    # ---------- Old Winner list add ----------
    if admin_state == "old_winner_add_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            ow = data.get("old_winner_block", {}) or {}
            before = len(ow)
            for uid, uname in entries:
                ow[uid] = {"username": uname}
            data["old_winner_block"] = ow
            save_data()
        admin_state = None
        update.message.reply_text(
            "✅ Old winner block list updated!\n"
            f"New Added: {len(data['old_winner_block']) - before}\n"
            f"Total Blocked: {len(data['old_winner_block'])}"
        )
        return

    # ---------- Prize Delivery list ----------
    if admin_state == "prize_delivery_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return

        dg_id = context.user_data.get("prize_delivery_gid")
        gwx = get_giveaway(dg_id) if dg_id else None
        if not gwx:
            admin_state = None
            update.message.reply_text("Giveaway not found for delivery update.")
            return

        with lock:
            delivered = gwx.get("delivered", {}) or {}
            before = len(delivered)
            for uid, uname in entries:
                old = delivered.get(uid, {}) or {}
                if uname:
                    delivered[uid] = {"username": uname}
                else:
                    delivered[uid] = {"username": old.get("username", "")}
            gwx["delivered"] = delivered
            save_data()

        # rebuild winners text and edit SAME channel post
        try:
            winners_map = gwx.get("winners", {}) or {}
            first_uid = str(gwx.get("first_winner_id") or "")
            first_uname = gwx.get("first_winner_username", "") or (winners_map.get(first_uid, {}) or {}).get("username", "")

            random_list = []
            for wuid, info in winners_map.items():
                if str(wuid) == first_uid:
                    continue
                random_list.append((str(wuid), (info or {}).get("username", "")))

            new_text = build_winners_post_text(gwx, first_uid, first_uname, random_list)
            mid = gwx.get("winners_message_id")
            if mid:
                ch_edit(context.bot, mid, new_text, reply_markup=claim_button_markup(dg_id))
            with lock:
                gwx["pending_winners_text"] = new_text
                save_data()
        except Exception:
            pass

        admin_state = None
        update.message.reply_text(
            "✅ Prize delivery saved successfully!\n"
            f"New Added: {len(gwx.get('delivered', {}) or {}) - before}\n"
            f"Total Delivered: {len(gwx.get('delivered', {}) or {})}/{int(gwx.get('winner_count',0) or 0)}"
        )
        return

    # ---------- Giveaway Setup (wizard) ----------
    gid, gw = get_current_giveaway()
    if not gid or not gw:
        admin_state = None
        update.message.reply_text("No active setup found. Use /newgiveaway")
        return

    if admin_state == "title":
        with lock:
            gw["title"] = msg
            save_data()
        admin_state = "prize"
        update.message.reply_text("✅ Title saved.\n\nNow send Prize (multi-line allowed):")
        return

    if admin_state == "prize":
        with lock:
            gw["prize"] = msg
            save_data()
        admin_state = "winners"
        update.message.reply_text("✅ Prize saved.\n\nNow send Winner Count (1 - 1000000):")
        return

    if admin_state == "winners":
        if not msg.isdigit():
            update.message.reply_text("Send a valid number for winner count.")
            return
        count = max(1, min(1000000, int(msg)))
        with lock:
            gw["winner_count"] = count
            save_data()
        admin_state = "duration"
        update.message.reply_text(
            f"✅ Winner count saved: {count}\n\n"
            "Now send Giveaway Duration\n"
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
            gw["duration_seconds"] = seconds
            save_data()
        admin_state = "rules"
        update.message.reply_text("✅ Duration saved.\n\nNow send Rules (multi-line):")
        return

    if admin_state == "rules":
        with lock:
            gw["rules"] = msg
            save_data()
        admin_state = None
        update.message.reply_text("✅ Rules saved.\n\nPreview shown below:")
        update.message.reply_text(
            tg_pre(build_preview_text(gw)),
            parse_mode="HTML",
            reply_markup=preview_markup(gid)
        )
        return

# =========================================================
# CALLBACK HANDLER
# =========================================================
def cb_handler(update: Update, context: CallbackContext):
    global admin_state
    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)

    # no-op
    if qd == "noop":
        try:
            query.answer()
        except Exception:
            pass
        return

    # Verify Add More/Done
    if qd == "verify_add_more":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        admin_state = "add_verify"
        try:
            query.edit_message_text("Send another Chat ID or @username:")
        except Exception:
            pass
        return

    if qd == "verify_add_done":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        admin_state = None
        try:
            query.edit_message_text(
                f"✅ VERIFY SETUP COMPLETED\n\n"
                f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}\n"
                "Users must join ALL targets to proceed."
            )
        except Exception:
            pass
        return

    # Toggle Old Winner Block
    if qd == "ow_on":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        with lock:
            data["old_winner_block_enabled"] = True
            save_data()
        query.answer("Old Winner Block: ON", show_alert=True)
        try:
            query.edit_message_text(
                "✅ Old Winner Block ENABLED.\n\n"
                "Now send old winner list (one per line):\n"
                "@username | user_id OR user_id"
            )
        except Exception:
            pass
        admin_state = "old_winner_add_list"
        return

    if qd == "ow_off":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        with lock:
            data["old_winner_block_enabled"] = False
            save_data()
        query.answer("Old Winner Block: OFF", show_alert=True)
        try:
            query.edit_message_text("❌ Old Winner Block DISABLED.")
        except Exception:
            pass
        admin_state = None
        return

    # Toggle Auto Winner Post
    if qd == "aw_on":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        with lock:
            data["auto_winner_post"] = True
            save_data()
        query.answer("Auto Winner Post: ON", show_alert=True)
        try:
            query.edit_message_text("✅ Auto Winner Post ENABLED.")
        except Exception:
            pass
        return

    if qd == "aw_off":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        with lock:
            data["auto_winner_post"] = False
            save_data()
        query.answer("Auto Winner Post: OFF", show_alert=True)
        try:
            query.edit_message_text("❌ Auto Winner Post DISABLED.")
        except Exception:
            pass
        return

    # Preview Approve/Reject/Edit
    if qd.startswith("papprove:") or qd.startswith("preject:") or qd == "pedit":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return

        if qd == "pedit":
            query.answer()
            admin_state = None
            try:
                query.edit_message_text("✏️ Edit mode: Start again with /newgiveaway")
            except Exception:
                pass
            return

        gid = qd.split(":", 1)[1]
        gw = get_giveaway(gid)
        if not gw:
            query.answer("Giveaway not found.", show_alert=True)
            return

        if qd.startswith("preject:"):
            query.answer()
            try:
                query.edit_message_text("❌ Giveaway rejected.")
            except Exception:
                pass
            return

        # Approve & Post
        query.answer()
        try:
            duration = int(gw.get("duration_seconds", 0) or 1)
            m = ch_send(context.bot, build_live_text(gw, duration), reply_markup=join_button_markup(gid))

            with lock:
                data["current_id"] = gid

                gw["live_message_id"] = m.message_id
                gw["active"] = True
                gw["closed"] = False
                gw["start_time"] = now_ts()

                gw["closed_message_id"] = None
                gw["winners_message_id"] = None

                gw["participants"] = {}
                gw["winners"] = {}
                gw["pending_winners_text"] = ""
                gw["first_winner_id"] = None
                gw["first_winner_username"] = ""
                gw["first_winner_name"] = ""

                gw["claim_start_ts"] = None
                gw["claim_expires_ts"] = None
                gw["delivered"] = {}

                save_data()

            stop_job("closed_spin_job")
            stop_job("channel_autodraw_job")
            start_live_countdown(context.job_queue)

            try:
                query.edit_message_text("✅ Giveaway posted to channel successfully!")
            except Exception:
                pass
        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post in channel. Ensure bot is admin.\nError: {e}")
            except Exception:
                pass
        return

    # End giveaway confirm/cancel
    if qd == "end_confirm":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()

        gid, gw = get_current_giveaway()
        if not gid or not gw or not gw.get("active"):
            try:
                query.edit_message_text("No active giveaway is running.")
            except Exception:
                pass
            return

        with lock:
            gw["active"] = False
            gw["closed"] = True
            save_data()

        live_mid = gw.get("live_message_id")
        if live_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
            except Exception:
                pass

        # post closed
        try:
            spin = "" if data.get("auto_winner_post") else SPINNER[0]
            m = ch_send(context.bot, build_closed_text(gw, spin))
            with lock:
                gw["closed_message_id"] = m.message_id
                save_data()
        except Exception:
            pass

        stop_job("countdown_job")

        if data.get("auto_winner_post"):
            start_channel_autodraw(context.job_queue, gid)
        else:
            start_closed_spinner(context.job_queue, gid)

        try:
            query.edit_message_text("✅ Giveaway Closed.")
        except Exception:
            pass
        return

    if qd == "end_cancel":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        try:
            query.edit_message_text("❌ Cancelled. Giveaway is still running.")
        except Exception:
            pass
        return

    # RESET confirm/cancel (Factory)
    if qd == "reset_confirm":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()

        # stop all jobs (including claim expiry)
        stop_all_jobs(context.job_queue)

        # delete all known channel messages (best effort)
        try:
            giveaways = data.get("giveaways", {}) or {}
            for _, gw in giveaways.items():
                for mid_key in ["live_message_id", "closed_message_id", "winners_message_id"]:
                    mid = (gw or {}).get(mid_key)
                    if mid:
                        try:
                            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
                        except Exception:
                            pass
        except Exception:
            pass

        with lock:
            data.clear()
            data.update(fresh_default_data())
            save_data()

        try:
            query.edit_message_text("✅ FULL FACTORY RESET COMPLETED.\nStart again with /newgiveaway")
        except Exception:
            pass
        return

    if qd == "reset_cancel":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        try:
            query.edit_message_text("❌ Reset cancelled.")
        except Exception:
            pass
        return

    # Unban choose
    if qd == "unban_permanent":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        admin_state = "unban_permanent_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Permanent Block:")
        except Exception:
            pass
        return

    if qd == "unban_oldwinner":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        admin_state = "unban_oldwinner_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Old Winner Block:")
        except Exception:
            pass
        return

    # removeban choose confirm
    if qd in ("reset_permanent_ban", "reset_oldwinner_ban"):
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()

        if qd == "reset_permanent_ban":
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Confirm Reset Permanent", callback_data="confirm_reset_permanent"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
                ]]
            )
            try:
                query.edit_message_text("Confirm reset Permanent Ban List?", reply_markup=kb)
            except Exception:
                pass
            return

        if qd == "reset_oldwinner_ban":
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Confirm Reset Old Winner", callback_data="confirm_reset_oldwinner"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
                ]]
            )
            try:
                query.edit_message_text("Confirm reset Old Winner Ban List?", reply_markup=kb)
            except Exception:
                pass
            return

    if qd == "cancel_reset_ban":
        query.answer()
        admin_state = None
        try:
            query.edit_message_text("Cancelled.")
        except Exception:
            pass
        return

    if qd == "confirm_reset_permanent":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
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
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        with lock:
            data["old_winner_block"] = {}
            save_data()
        try:
            query.edit_message_text("✅ Old Winner Ban List has been reset.")
        except Exception:
            pass
        return

    # Join Giveaway (unique per giveaway)
    if qd.startswith("join:"):
        gid = qd.split(":", 1)[1]
        gw = get_giveaway(gid)
        if not gw or not gw.get("active"):
            query.answer("This giveaway is not active right now.", show_alert=True)
            return

        # Verify required channels
        if not verify_user_join(context.bot, int(uid)):
            query.answer(popup_verify_required(), show_alert=True)
            return

        # Permanent block
        if uid in (data.get("permanent_block", {}) or {}):
            query.answer(popup_permanent_blocked(), show_alert=True)
            return

        # Old winner block (global) if enabled
        if data.get("old_winner_block_enabled"):
            if uid in (data.get("old_winner_block", {}) or {}):
                query.answer(popup_old_winner_blocked(), show_alert=True)
                return

        # Already joined?
        if uid in (gw.get("participants", {}) or {}):
            query.answer(popup_already_joined(), show_alert=True)
            return

        tg_user = query.from_user
        uname = user_tag(tg_user.username or "")
        full_name = (tg_user.full_name or "").strip()

        with lock:
            # first winner
            if not gw.get("first_winner_id"):
                gw["first_winner_id"] = uid
                gw["first_winner_username"] = uname
                gw["first_winner_name"] = full_name

            gw["participants"][uid] = {"username": uname, "name": full_name}
            save_data()

        # update live post
        try:
            live_mid = gw.get("live_message_id")
            start_ts = gw.get("start_time")
            if live_mid and start_ts:
                start = datetime.utcfromtimestamp(start_ts)
                duration = int(gw.get("duration_seconds", 1) or 1)
                elapsed = int((datetime.utcnow() - start).total_seconds())
                remaining = duration - elapsed
                if remaining < 0:
                    remaining = 0
                ch_edit(context.bot, live_mid, build_live_text(gw, remaining), reply_markup=join_button_markup(gid))
        except Exception:
            pass

        # popup (first or normal)
        if gw.get("first_winner_id") == uid:
            query.answer(popup_first_winner(uname or "@username", uid), show_alert=True)
        else:
            query.answer(popup_join_success(uname or "@Username", uid), show_alert=True)
        return

    # Winners Approve/Reject (manual draw)
    if qd.startswith("wapprove:") or qd.startswith("wreject:"):
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return

        gid = qd.split(":", 1)[1]
        gw = get_giveaway(gid)
        if not gw:
            query.answer("Giveaway not found.", show_alert=True)
            return

        if qd.startswith("wreject:"):
            query.answer()
            with lock:
                gw["pending_winners_text"] = ""
                save_data()
            try:
                query.edit_message_text("❌ Rejected! Winners will NOT be posted.")
            except Exception:
                pass
            return

        # approve
        query.answer()
        text = (gw.get("pending_winners_text") or "").strip()
        if not text:
            try:
                query.edit_message_text("No pending winners preview found.")
            except Exception:
                pass
            return

        # stop closed spinner if running
        stop_job("closed_spin_job")

        # delete closed message if exists
        closed_mid = gw.get("closed_message_id")
        if closed_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception:
                pass

        # post winners
        try:
            m = ch_send(context.bot, text, reply_markup=claim_button_markup(gid))
            with lock:
                gw["winners_message_id"] = m.message_id
                gw["closed_message_id"] = None

                ts = now_ts()
                gw["claim_start_ts"] = ts
                gw["claim_expires_ts"] = ts + 24 * 3600
                save_data()

            schedule_claim_expire(context.job_queue, gid)

            try:
                query.edit_message_text("✅ Approved! Winners posted to channel.")
            except Exception:
                pass
        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post winners in channel: {e}")
            except Exception:
                pass
        return

    # Claim prize (unique per giveaway)
    if qd.startswith("claim:"):
        gid = qd.split(":", 1)[1]
        gw = get_giveaway(gid)
        if not gw:
            query.answer("Giveaway not found.", show_alert=True)
            return

        # Verify required channels on claim too
        if not verify_user_join(context.bot, int(uid)):
            query.answer(popup_verify_required(), show_alert=True)
            return

        winners = gw.get("winners", {}) or {}

        # not winner
        if uid not in winners:
            query.answer(popup_claim_not_winner(), show_alert=True)
            return

        # already delivered
        delivered = gw.get("delivered", {}) or {}
        if uid in delivered:
            query.answer(popup_prize_already_delivered(), show_alert=True)
            return

        # expired
        exp_ts = gw.get("claim_expires_ts")
        if exp_ts:
            try:
                if now_ts() > float(exp_ts):
                    query.answer(popup_prize_expired(), show_alert=True)
                    return
            except Exception:
                pass

        title = (gw.get("title") or "").strip()
        prize = (gw.get("prize") or "").strip()
        uname = winners.get(uid, {}).get("username", "") or user_tag(query.from_user.username or "") or "@username"
        query.answer(popup_claim_winner(title, prize, uname, uid), show_alert=True)
        return

    try:
        query.answer()
    except Exception:
        pass

# =========================================================
# UNBAN INPUT HANDLERS (admin text)
# =========================================================
def admin_unban_text(update: Update, msg: str):
    global admin_state
    if admin_state == "unban_permanent_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @name | id)")
            return True
        uid, _ = entries[0]
        with lock:
            perma = data.get("permanent_block", {}) or {}
            if uid in perma:
                del perma[uid]
                data["permanent_block"] = perma
                save_data()
                update.message.reply_text("✅ Unbanned from Permanent Block.")
            else:
                update.message.reply_text("User ID not found in Permanent Block list.")
        admin_state = None
        return True

    if admin_state == "unban_oldwinner_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @name | id)")
            return True
        uid, _ = entries[0]
        with lock:
            ow = data.get("old_winner_block", {}) or {}
            if uid in ow:
                del ow[uid]
                data["old_winner_block"] = ow
                save_data()
                update.message.reply_text("✅ Unbanned from Old Winner Block.")
            else:
                update.message.reply_text("User ID not found in Old Winner Block list.")
        admin_state = None
        return True

    return False

# wrap admin_text_handler to include unban handling
_old_admin_text_handler = admin_text_handler
def admin_text_handler(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    if admin_state is None:
        return
    msg = (update.message.text or "").strip()
    if not msg:
        return
    if admin_unban_text(update, msg):
        return
    _old_admin_text_handler(update, context)

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

    # system toggles
    dp.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))
    dp.add_handler(CommandHandler("blockoldwinner", cmd_blockoldwinner))
    dp.add_handler(CommandHandler("prizedelivery", cmd_prizedelivery))

    # bans
    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))

    # reset
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # handlers
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # ======================
    # RESUME AFTER RESTART
    # ======================
    gid, gw = get_current_giveaway()
    if gid and gw:
        if gw.get("active"):
            start_live_countdown(updater.job_queue)
        elif gw.get("closed") and gw.get("closed_message_id") and not gw.get("winners_message_id"):
            if data.get("auto_winner_post"):
                start_channel_autodraw(updater.job_queue, gid)
            else:
                start_closed_spinner(updater.job_queue, gid)

    # Resume claim expiry timers for all giveaways with active claim window
    try:
        for gid2, gw2 in (data.get("giveaways", {}) or {}).items():
            if (gw2 or {}).get("winners_message_id") and (gw2 or {}).get("claim_expires_ts"):
                remain = float(gw2["claim_expires_ts"]) - now_ts()
                if remain > 0:
                    schedule_claim_expire(updater.job_queue, gid2)
    except Exception:
        pass

    print("Bot is running (PTB 13.x, non-async) ...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
