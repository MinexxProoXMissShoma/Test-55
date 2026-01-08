import os
import json
import random
import threading
from datetime import datetime, timedelta

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

TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "6"))  # BD default +6

# =========================================================
# LOCK
# =========================================================
lock = threading.RLock()

# =========================================================
# GLOBALS
# =========================================================
data = {}
admin_state = None

live_job = None

# Draw jobs (admin manual)
draw_job = None
draw_finalize_job = None

# Auto draw jobs (channel)
auto_draw_job = None
auto_draw_finalize_job = None

# Reset jobs
reset_job = None
reset_finalize_job = None

# =========================================================
# CONSTANTS
# =========================================================
LIVE_UPDATE_INTERVAL = 5           # giveaway live post update
PROGRESS_UPDATE_INTERVAL = 5       # percent/progress updates each 5s
SPINNER_UPDATE_INTERVAL = 1        # spinner fast
MANUAL_DRAW_SECONDS = 40
AUTO_DRAW_SECONDS = 120            # 2 minutes

CLAIM_EXPIRE_SECONDS = 24 * 60 * 60

SPINNER = ["🔄", "🔃"]  # requested
BAR_BLOCKS = 10

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
        "winners_message_id": None,

        # participants
        "participants": {},  # uid(str) -> {"username": "@x", "name": ""}

        # verify targets (max 10)
        "verify_targets": [],  # [{"ref": "-100..." or "@xxx", "display": "..."}]

        # permanent block
        "permanent_block": {},  # uid -> {"username": "@x"}

        # old winner mode (setup time)
        "old_winner_mode": "skip",  # "skip" or "block"
        "old_winners": {},          # uid -> {"username": "@x"}  (blocked list)

        # first join winner
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # current winners map (for claim)
        "winners": {},  # uid -> {"username": "@x"}
        "pending_winners_text": "",
        "winners_posted_at": None,   # timestamp when winners posted (for claim expiry)

        # history (auto)
        "winner_history": [],  # list of dict entries

        # auto winner selection
        "autowinner": False,
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

    # safety types
    if not isinstance(d.get("verify_targets"), list):
        d["verify_targets"] = []
    if not isinstance(d.get("winner_history"), list):
        d["winner_history"] = []
    if not isinstance(d.get("participants"), dict):
        d["participants"] = {}
    if not isinstance(d.get("permanent_block"), dict):
        d["permanent_block"] = {}
    if not isinstance(d.get("old_winners"), dict):
        d["old_winners"] = {}
    if not isinstance(d.get("winners"), dict):
        d["winners"] = {}

    return d


def save_data():
    with lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


data = load_data()

# =========================================================
# HELPERS
# =========================================================
def bd_now():
    return datetime.utcnow() + timedelta(hours=TZ_OFFSET_HOURS)

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
    return f"{h:02d} : {m:02d} : {s:02d}"

def build_bar(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    filled = int(round((BAR_BLOCKS * percent) / 100))
    filled = max(0, min(BAR_BLOCKS, filled))
    empty = BAR_BLOCKS - filled
    return ("▰" * filled) + ("▱" * empty)

def parse_duration(text: str) -> int:
    t = (text or "").strip().lower()
    if not t:
        return 0
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

def parse_user_lines(text: str):
    """
    Accept:
      123456789
      @name | 123456789
    Username optional, BUT admin can always send both.
    Bot works by USER ID only.
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

def autowinner_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Auto Post ON", callback_data="autowinner_on"),
            InlineKeyboardButton("❌ Auto Post OFF", callback_data="autowinner_off"),
        ]]
    )

def reset_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel"),
        ]]
    )

# =========================================================
# VERIFY CHECK
# =========================================================
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
# POPUPS (clean spacing, no extra words/emojis)
# =========================================================
def popup_verify_required() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n\n"
        "To join this giveaway, please join all required\n"
        "channels/groups first ✅\n\n"
        "After joining, tap JOIN GIVEAWAY again."
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

def popup_claim_expired() -> str:
    return (
        "⏳ CLAIM EXPIRED\n\n"
        "Your prize claim time is over.\n"
        "This prize is no longer available."
    )

def popup_claim_winner(username: str, uid: str) -> str:
    return (
        "🌟Congratulations✨\n"
        "You’ve won this giveaway.\n"
        f"👤 {username} | 🆔 {uid}\n"
        "📩 Please contact admin to claim your prize:\n"
        f"👉 {ADMIN_CONTACT}"
    )

def popup_claim_not_winner_no_border() -> str:
    return (
        "❌ YOU ARE NOT A WINNER\n\n"
        "Sorry🥺! Your User ID is not in the winners list.\n"
        "Please wait for the next giveaway❤️‍🩹"
    )

# =========================================================
# TEXT BUILDERS (CHANNEL POSTS)
# =========================================================
def format_rules_lines() -> str:
    rules = (data.get("rules") or "").strip()
    if not rules:
        return (
            "✅ Must join official channel\n"
            "❌ One account per user\n"
            "🚫 No fake / duplicate accounts"
        )
    lines = [l.strip() for l in rules.splitlines() if l.strip()]
    return "\n".join(lines)

def build_live_post_text(remaining: int, percent: int) -> str:
    bar = build_bar(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚡️🔥 POWER POINT BREAK GIVEAWAY 🔥⚡️\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL✨\n"
        f"{data.get('prize','')}\n\n"
        f"👥 TOTAL PARTICIPANTS: {participants_count()}\n"
        f"🏅 TOTAL WINNERS: {data.get('winner_count',0)}\n"
        "🎯 WINNER SELECTION: 100% Random & Fair\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms(remaining)}\n"
        f"📊 LIVE PROGRESS  {bar} {percent}%\n\n"
        "📜 RULES....\n"
        f"{format_rules_lines()}\n\n"
        f"📢 HOSTED BY⚡️ {HOST_NAME}\n"
        "👇 READY TO WIN?\n"
        "👇✨ TAP THE BUTTON BELOW & JOIN NOW 👇"
    )

def build_closed_post_text_unique() -> str:
    # aggressive + no line break issues (kept short width borders)
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
    lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
    if first_user:
        lines.append(f"👑 {first_user}")
        lines.append(f"🆔 {first_uid}")
    else:
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
# PREVIEW TEXT
# =========================================================
def build_preview_text() -> str:
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
        f"⏱ Duration: {data.get('duration_seconds',0)} seconds\n\n"
        "📜 Rules:\n"
        f"{format_rules_lines()}\n\n"
        f"📢 Hosted By: {HOST_NAME}\n"
        f"🔗 Official Channel: {CHANNEL_USERNAME}\n\n"
        "👇 Please tap the button below to post giveaway"
    )

# =========================================================
# LIVE GIVEAWAY LOOP
# =========================================================
def stop_live_job():
    global live_job
    if live_job is not None:
        try:
            live_job.schedule_removal()
        except Exception:
            pass
    live_job = None

def start_live_job(job_queue):
    global live_job
    stop_live_job()
    live_job = job_queue.run_repeating(live_tick, interval=LIVE_UPDATE_INTERVAL, first=0)

def live_tick(context: CallbackContext):
    global data
    with lock:
        if not data.get("active"):
            stop_live_job()
            return

        start_ts = data.get("start_time")
        if start_ts is None:
            data["start_time"] = datetime.utcnow().timestamp()
            save_data()
            start_ts = data["start_time"]

        duration = int(data.get("duration_seconds", 1)) or 1
        elapsed = int(datetime.utcnow().timestamp() - float(start_ts))
        remaining = duration - elapsed

        if remaining <= 0:
            # CLOSE
            data["active"] = False
            data["closed"] = True
            save_data()

            live_mid = data.get("live_message_id")
            stop_live_job()

        else:
            live_mid = data.get("live_message_id")

    # Update live post
    if remaining > 0 and live_mid:
        percent = int(round(((duration - remaining) / float(duration)) * 100))
        try:
            context.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=live_mid,
                text=build_live_post_text(remaining, percent),
                reply_markup=join_button_markup(),
            )
        except Exception:
            pass
        return

    # When remaining <= 0 (closed)
    # delete live post
    try:
        if live_mid:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
    except Exception:
        pass

    # post closed message
    try:
        m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text_unique())
        with lock:
            data["closed_message_id"] = m.message_id
            data["live_message_id"] = None
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
                "Now use /draw to select winners.\n"
                "(Auto Winner depends on /autowinnerpost setting)"
            ),
        )
    except Exception:
        pass

    # AUTO WINNER (if enabled)
    with lock:
        auto_on = bool(data.get("autowinner", False))
        closed_mid = data.get("closed_message_id")

    if auto_on:
        try:
            start_channel_auto_draw(context, closed_mid)
        except Exception:
            pass

# =========================================================
# DRAW ENGINE (shared)
# =========================================================
def choose_winners():
    """
    Returns: first_uid, first_uname, random_list[(uid, uname)], winners_map
    """
    parts = data.get("participants", {}) or {}
    if not parts:
        return None, "", [], {}

    winner_count = int(data.get("winner_count", 1)) or 1
    winner_count = max(1, winner_count)

    first_uid = data.get("first_winner_id")
    if not first_uid or first_uid not in parts:
        first_uid = next(iter(parts.keys()))
        info = parts.get(first_uid, {}) or {}
        data["first_winner_id"] = first_uid
        data["first_winner_username"] = info.get("username", "")
        data["first_winner_name"] = info.get("name", "")

    first_uname = data.get("first_winner_username", "") or (parts.get(first_uid, {}) or {}).get("username", "")

    pool = [uid for uid in parts.keys() if uid != first_uid]
    need = max(0, winner_count - 1)
    if need > len(pool):
        need = len(pool)
    selected = random.sample(pool, need) if need > 0 else []

    winners_map = {}
    winners_map[first_uid] = {"username": first_uname}

    random_list = []
    for uid in selected:
        info = parts.get(uid, {}) or {}
        winners_map[uid] = {"username": info.get("username", "")}
        random_list.append((uid, info.get("username", "")))

    return first_uid, first_uname, random_list, winners_map

def save_winner_history_entry():
    # Build one history entry per winner (including first)
    now = bd_now()
    title = data.get("title", "")
    prize = data.get("prize", "")
    winners_map = data.get("winners", {}) or {}

    for uid, info in winners_map.items():
        uname = (info or {}).get("username", "") or ""
        win_type = "👑 Random Winner"
        if str(uid) == str(data.get("first_winner_id")):
            win_type = "🥇 1st Winner (First Join)"

        data["winner_history"].append({
            "uid": str(uid),
            "username": uname,
            "title": title,
            "prize": prize,
            "win_type": win_type,
            "date": now.strftime("%d/%m/%Y"),
            "time": now.strftime("%H:%M:%S"),
        })

# =========================================================
# MANUAL DRAW (ADMIN) - 40s
# Spinner edits every 1s, % updates every 5s
# =========================================================
def stop_manual_draw_jobs():
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

def build_draw_text(percent: int, spin: str) -> str:
    bar = build_bar(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔎 Selecting winners... {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        f"{spin} Winner selection is in progress\n\n"
        "✅ This draw is 100% fair & random.\n"
        "🔐 User ID based selection only.\n\n"
        "Please wait"
    )

def start_manual_draw(context: CallbackContext, admin_chat_id: int):
    global draw_job, draw_finalize_job
    stop_manual_draw_jobs()

    msg = context.bot.send_message(chat_id=admin_chat_id, text=build_draw_text(0, SPINNER[0]))

    ctx = {
        "chat_id": admin_chat_id,
        "msg_id": msg.message_id,
        "start_ts": datetime.utcnow().timestamp(),
        "tick": 0,
        "last_percent_step": -1,
        "duration": MANUAL_DRAW_SECONDS,
    }

    def tick_fn(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] += 1
        elapsed = max(0, int(datetime.utcnow().timestamp() - jd["start_ts"]))
        dur = jd["duration"]
        # percent updates each 5 seconds step
        step = elapsed // PROGRESS_UPDATE_INTERVAL
        if step != jd["last_percent_step"]:
            jd["last_percent_step"] = step
        percent = int(round(min(100, (elapsed / float(dur)) * 100)))

        spin = SPINNER[(jd["tick"] % len(SPINNER))]
        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["chat_id"],
                message_id=jd["msg_id"],
                text=build_draw_text(percent, spin),
            )
        except Exception:
            pass

    draw_job = context.job_queue.run_repeating(
        tick_fn,
        interval=SPINNER_UPDATE_INTERVAL,
        first=0,
        context=ctx,
        name="manual_draw_job",
    )

    draw_finalize_job = context.job_queue.run_once(
        manual_draw_finalize,
        when=MANUAL_DRAW_SECONDS,
        context=ctx,
        name="manual_draw_finalize",
    )

def manual_draw_finalize(context: CallbackContext):
    global data
    stop_manual_draw_jobs()

    jd = context.job.context
    chat_id = jd["chat_id"]
    msg_id = jd["msg_id"]

    with lock:
        if not (data.get("participants", {}) or {}):
            try:
                context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="No participants to draw winners from.")
            except Exception:
                pass
            return

        first_uid, first_uname, random_list, winners_map = choose_winners()
        if not first_uid:
            try:
                context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="No participants to draw winners from.")
            except Exception:
                pass
            return

        data["winners"] = winners_map
        winners_text = build_winners_post_text(first_uid, first_uname, random_list)
        data["pending_winners_text"] = winners_text
        save_data()

    try:
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=winners_text,
            reply_markup=winners_approve_markup(),
        )
    except Exception:
        context.bot.send_message(chat_id=chat_id, text=winners_text, reply_markup=winners_approve_markup())

# =========================================================
# AUTO DRAW (CHANNEL) - 2 minutes
# Channel shows progress (no numbers visible except %)
# Spinner fast, % updates each 5s step (still edited every 1s for spinner)
# =========================================================
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

def start_channel_auto_draw(context: CallbackContext, closed_mid: int):
    """
    When autowinner ON:
      - keep closed post (no live dots)
      - post a new message in channel "RANDOM WINNER SELECTION" and pin it (best effort)
      - update it for 2 minutes
      - then auto post winners, remove closed post, and stop selection post updates
    """
    global auto_draw_job, auto_draw_finalize_job
    stop_auto_draw_jobs()

    # post selection message in channel
    msg = context.bot.send_message(chat_id=CHANNEL_ID, text=build_draw_text(0, SPINNER[0]))
    try:
        context.bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=msg.message_id, disable_notification=True)
    except Exception:
        pass

    ctx = {
        "chat_id": CHANNEL_ID,
        "msg_id": msg.message_id,
        "start_ts": datetime.utcnow().timestamp(),
        "tick": 0,
        "duration": AUTO_DRAW_SECONDS,
        "closed_mid": closed_mid,
    }

    def tick_fn(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] += 1
        elapsed = max(0, int(datetime.utcnow().timestamp() - jd["start_ts"]))
        dur = jd["duration"]
        percent = int(round(min(100, (elapsed / float(dur)) * 100)))
        spin = SPINNER[(jd["tick"] % len(SPINNER))]
        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["chat_id"],
                message_id=jd["msg_id"],
                text=build_draw_text(percent, spin),
            )
        except Exception:
            pass

    auto_draw_job = context.job_queue.run_repeating(
        tick_fn,
        interval=SPINNER_UPDATE_INTERVAL,
        first=0,
        context=ctx,
        name="auto_draw_job",
    )

    auto_draw_finalize_job = context.job_queue.run_once(
        auto_draw_finalize,
        when=AUTO_DRAW_SECONDS,
        context=ctx,
        name="auto_draw_finalize",
    )

def auto_draw_finalize(context: CallbackContext):
    global data
    stop_auto_draw_jobs()

    jd = context.job.context
    sel_chat = jd["chat_id"]
    sel_mid = jd["msg_id"]
    closed_mid = jd.get("closed_mid")

    with lock:
        if not (data.get("participants", {}) or {}):
            # no participants, just stop
            try:
                context.bot.edit_message_text(chat_id=sel_chat, message_id=sel_mid, text="No participants to draw winners from.")
            except Exception:
                pass
            return

        first_uid, first_uname, random_list, winners_map = choose_winners()
        data["winners"] = winners_map
        winners_text = build_winners_post_text(first_uid, first_uname, random_list)
        save_data()

    # remove closed post (your rule)
    try:
        if closed_mid:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
    except Exception:
        pass

    # post winners to channel (auto)
    try:
        m = context.bot.send_message(chat_id=CHANNEL_ID, text=winners_text, reply_markup=claim_button_markup())
        with lock:
            data["winners_message_id"] = m.message_id
            data["closed_message_id"] = None
            data["winners_posted_at"] = datetime.utcnow().timestamp()
            save_winner_history_entry()
            save_data()
    except Exception:
        pass

    # stop selection post (best effort delete)
    try:
        context.bot.delete_message(chat_id=sel_chat, message_id=sel_mid)
    except Exception:
        pass

# =========================================================
# RESET (40s progress, FULL RESET ALL)
# =========================================================
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

def build_reset_progress_text(percent: int, spin: str) -> str:
    bar = build_bar(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ FULL RESET IN PROGRESS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Progress: {bar} {percent}%\n\n"
        f"{spin} Resetting system...\n\n"
        "Please wait"
    )

def start_reset_progress(context: CallbackContext, chat_id: int, msg_id: int):
    global reset_job, reset_finalize_job
    stop_reset_jobs()

    ctx = {
        "chat_id": chat_id,
        "msg_id": msg_id,
        "start_ts": datetime.utcnow().timestamp(),
        "tick": 0,
        "duration": 40,
    }

    def tick_fn(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] += 1
        elapsed = max(0, int(datetime.utcnow().timestamp() - jd["start_ts"]))
        dur = jd["duration"]
        percent = int(round(min(100, (elapsed / float(dur)) * 100)))
        spin = SPINNER[(jd["tick"] % len(SPINNER))]
        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["chat_id"],
                message_id=jd["msg_id"],
                text=build_reset_progress_text(percent, spin),
            )
        except Exception:
            pass

    reset_job = context.job_queue.run_repeating(
        tick_fn,
        interval=SPINNER_UPDATE_INTERVAL,
        first=0,
        context=ctx,
        name="reset_job",
    )

    reset_finalize_job = context.job_queue.run_once(
        reset_finalize,
        when=40,
        context=ctx,
        name="reset_finalize",
    )

def reset_finalize(context: CallbackContext):
    global data
    stop_reset_jobs()

    jd = context.job.context
    chat_id = jd["chat_id"]
    msg_id = jd["msg_id"]

    # stop all jobs
    stop_live_job()
    stop_manual_draw_jobs()
    stop_auto_draw_jobs()

    # delete channel messages if exists
    try:
        for key in ["live_message_id", "closed_message_id", "winners_message_id"]:
            mid = data.get(key)
            if mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
                except Exception:
                    pass
    except Exception:
        pass

    with lock:
        data = fresh_default_data()
        save_data()

    try:
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=(
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ RESET COMPLETED\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "All data has been cleared.\n"
                "Start again with /newgiveaway"
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
            "🛡️ ADMIN ONLINE ✅\n\n"
            "/panel"
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
        "🛠 ADMIN CONTROL PANEL\n\n"
        "📌 GIVEAWAY\n"
        "/newgiveaway\n"
        "/participants\n"
        "/draw\n"
        "/endgiveaway\n"
        "/autowinnerpost\n"
        "/winnerlist\n"
        "/complete\n\n"
        "✅ VERIFY\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "🔒 BLOCK\n"
        "/blockpermanent\n"
        "/blockoldwinner\n"
        "/blocklist\n"
        "/unban\n"
        "/removeban\n\n"
        "♻️ RESET\n"
        "/reset"
    )

def cmd_autowinnerpost(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "Auto Winner Post setting:",
        reply_markup=autowinner_markup()
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
        "Max targets: 10"
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
        # keep bans + verify (not reset here)
        perma = data.get("permanent_block", {}) or {}
        oldw = data.get("old_winners", {}) or {}
        verify = data.get("verify_targets", []) or []
        hist = data.get("winner_history", []) or []
        auto = bool(data.get("autowinner", False))

        data.clear()
        data.update(fresh_default_data())
        data["permanent_block"] = perma
        data["old_winners"] = oldw
        data["verify_targets"] = verify
        data["winner_history"] = hist
        data["autowinner"] = auto
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
        "👥 PARTICIPANTS LIST",
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
    # Manual draw only when autowinner OFF (your final rule)
    if not is_admin(update):
        return
    if not data.get("closed"):
        update.message.reply_text("Giveaway is not closed yet.")
        return
    with lock:
        if bool(data.get("autowinner", False)):
            update.message.reply_text("Auto Winner is ON. Winners will be selected automatically.")
            return
    if not (data.get("participants", {}) or {}):
        update.message.reply_text("No participants to draw winners from.")
        return
    start_manual_draw(context, update.effective_chat.id)

def cmd_winnerlist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    hist = data.get("winner_history", []) or []
    if not hist:
        update.message.reply_text("Winner history is empty.")
        return

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆 WINNER HISTORY LIST")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    # newest first
    for idx, w in enumerate(reversed(hist), start=1):
        uname = w.get("username", "")
        uid = w.get("uid", "")
        title = w.get("title", "")
        prize = w.get("prize", "")
        win_type = w.get("win_type", "")
        date = w.get("date", "")
        time = w.get("time", "")
        lines.append(f"{idx}) {uname if uname else 'User ID'} | {uid}")
        lines.append(f"📅 {date}  ⏰ {time}")
        lines.append(f"🏅 {win_type}")
        lines.append(f"⚡ Giveaway: {title}")
        lines.append("🎁 Prize:")
        lines.append(prize)
        lines.append("")
    update.message.reply_text("\n".join(lines))

def cmd_complete(update: Update, context: CallbackContext):
    # simple completion post (admin manual approve not needed here, per your latest remove)
    if not is_admin(update):
        return
    winners_map = data.get("winners", {}) or {}
    if not winners_map:
        update.message.reply_text("No winners found. Post winners first.")
        return

    # build a compact delivery confirmation (no 24h here)
    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("✅ PRIZE DELIVERY COMPLETED")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("All giveaway prizes have been delivered ✅")
    lines.append("")
    lines.append(f"🏆 Giveaway: {data.get('title','')}")
    lines.append("🎁 Prize:")
    lines.append(f"{data.get('prize','')}")
    lines.append("")
    lines.append("👑 Winners:")
    i = 1
    for uid, info in winners_map.items():
        uname = (info or {}).get("username", "")
        if uname:
            lines.append(f"{i}) {uname} | {uid}")
        else:
            lines.append(f"{i}) User ID: {uid}")
        i += 1
    lines.append("")
    lines.append(f"— {HOST_NAME} ⚡")
    text = "\n".join(lines)

    try:
        context.bot.send_message(chat_id=CHANNEL_ID, text=text)
        update.message.reply_text("✅ Posted prize delivery completion to channel.")
    except Exception as e:
        update.message.reply_text(f"Failed to post in channel: {e}")

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
    admin_state = "oldwinner_block_cmd"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⛔ OLD WINNER BLOCK\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send list (one per line):\n"
        "@username | user_id\n"
        "or\n"
        "user_id"
    )

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

def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "⚠️ This will remove EVERYTHING.\n\nConfirm reset?",
        reply_markup=reset_confirm_markup()
    )

# =========================================================
# ADMIN TEXT FLOW
# =========================================================
def admin_text_handler(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    if not admin_state:
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
            if len(targets) >= 10:
                update.message.reply_text("Max verify targets reached (10). Remove some first.")
                return

            # prevent duplicates
            for t in targets:
                if (t or {}).get("ref") == ref:
                    update.message.reply_text("This verify target already exists.")
                    return

            targets.append({"ref": ref, "display": ref})
            data["verify_targets"] = targets
            save_data()

        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET ADDED SUCCESSFULLY!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Added: {ref}\n"
            f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}",
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

        admin_state = "old_winner_mode"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 OLD WINNER PROTECTION MODE\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "1️⃣ BLOCK OLD WINNERS\n"
            "• Old winners cannot join this giveaway\n\n"
            "2️⃣ SKIP OLD WINNERS\n"
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

        with lock:
            data["old_winner_mode"] = "block" if msg == "1" else "skip"
            # NOTE: old_winners list is always respected if filled (because /blockoldwinner)
            save_data()

        admin_state = "rules"
        update.message.reply_text("✅ Mode saved!\n\nNow send Giveaway Rules (multi-line):")
        return

    if admin_state == "rules":
        with lock:
            data["rules"] = msg
            save_data()
        admin_state = None
        update.message.reply_text("✅ Rules saved!\nShowing preview…")
        update.message.reply_text(build_preview_text(), reply_markup=preview_markup())
        return

    # PERMA BLOCK LIST
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
            f"✅ Permanent block saved!\nNew Added: {len(data['permanent_block']) - before}\nTotal Blocked: {len(data['permanent_block'])}"
        )
        return

    # OLD WINNER BLOCK CMD
    if admin_state == "oldwinner_block_cmd":
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
        admin_state = None
        update.message.reply_text(
            f"✅ Old winner blocked!\nNew Added: {len(data['old_winners']) - before}\nTotal Old Winner Blocked: {len(data['old_winners'])}"
        )
        return

    # UNBAN INPUTS
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

# =========================================================
# CALLBACKS
# =========================================================
def cb_handler(update: Update, context: CallbackContext):
    global admin_state
    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)

    def alert(txt):
        try:
            query.answer(txt, show_alert=True)
        except Exception:
            pass

    # BLOCKED USERS -> any button click shows block popup (your rule)
    if uid in (data.get("permanent_block", {}) or {}):
        alert(popup_permanent_blocked())
        return
    # old winner list only triggers if list has entries
    oldw = data.get("old_winners", {}) or {}
    if oldw and uid in oldw:
        alert(popup_old_winner_blocked())
        return

    # verify add buttons
    if qd == "verify_add_more":
        if uid != str(ADMIN_ID):
            alert("Admin only.")
            return
        admin_state = "add_verify"
        try:
            query.answer()
            query.edit_message_text("Send another Chat ID or @username:")
        except Exception:
            pass
        return

    if qd == "verify_add_done":
        if uid != str(ADMIN_ID):
            alert("Admin only.")
            return
        admin_state = None
        try:
            query.answer()
            query.edit_message_text(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ VERIFY SETUP COMPLETED\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}"
            )
        except Exception:
            pass
        return

    # autowinner on/off
    if qd == "autowinner_on":
        if uid != str(ADMIN_ID):
            alert("Admin only.")
            return
        with lock:
            data["autowinner"] = True
            save_data()
        try:
            query.answer()
            query.edit_message_text("✅ Auto Winner Post: ON")
        except Exception:
            pass
        return

    if qd == "autowinner_off":
        if uid != str(ADMIN_ID):
            alert("Admin only.")
            return
        with lock:
            data["autowinner"] = False
            save_data()
        try:
            query.answer()
            query.edit_message_text("❌ Auto Winner Post: OFF")
        except Exception:
            pass
        return

    # preview approve/reject/edit
    if qd.startswith("preview_"):
        if uid != str(ADMIN_ID):
            alert("Admin only.")
            return

        if qd == "preview_approve":
            try:
                query.answer()
            except Exception:
                pass

            try:
                duration = int(data.get("duration_seconds", 0)) or 1

                # Reset giveaway runtime state
                with lock:
                    data["participants"] = {}
                    data["winners"] = {}
                    data["pending_winners_text"] = ""
                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""
                    data["closed_message_id"] = None
                    data["winners_message_id"] = None
                    data["winners_posted_at"] = None

                # Remove old giveaway post if exists
                old_mid = data.get("live_message_id")
                if old_mid:
                    try:
                        context.bot.delete_message(chat_id=CHANNEL_ID, message_id=old_mid)
                    except Exception:
                        pass

                # Post new giveaway live post
                m = context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=build_live_post_text(duration, 0),
                    reply_markup=join_button_markup(),
                )

                with lock:
                    data["live_message_id"] = m.message_id
                    data["active"] = True
                    data["closed"] = False
                    data["start_time"] = datetime.utcnow().timestamp()
                    save_data()

                start_live_job(context.job_queue)

                query.edit_message_text("✅ Giveaway approved and posted to channel!")
            except Exception as e:
                try:
                    query.edit_message_text(f"Failed to post in channel. Make sure bot is admin.\nError: {e}")
                except Exception:
                    pass
            return

        if qd == "preview_reject":
            try:
                query.answer()
                query.edit_message_text("❌ Giveaway rejected.")
            except Exception:
                pass
            return

        if qd == "preview_edit":
            try:
                query.answer()
                query.edit_message_text("✏️ Edit Mode\n\nStart again with /newgiveaway")
            except Exception:
                pass
            return

    # end giveaway confirm/cancel
    if qd == "end_confirm":
        if uid != str(ADMIN_ID):
            alert("Admin only.")
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
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text_unique())
            with lock:
                data["closed_message_id"] = m.message_id
                data["live_message_id"] = None
                save_data()
        except Exception:
            pass

        stop_live_job()
        try:
            query.edit_message_text("✅ Giveaway Closed Successfully!")
        except Exception:
            pass

        # auto winner selection if ON
        with lock:
            auto_on = bool(data.get("autowinner", False))
            closed_mid = data.get("closed_message_id")
        if auto_on:
            try:
                start_channel_auto_draw(context, closed_mid)
            except Exception:
                pass
        return

    if qd == "end_cancel":
        if uid != str(ADMIN_ID):
            alert("Admin only.")
            return
        try:
            query.answer()
            query.edit_message_text("❌ Cancelled. Giveaway is still running.")
        except Exception:
            pass
        return

    # reset confirm/cancel
    if qd == "reset_confirm":
        if uid != str(ADMIN_ID):
            alert("Admin only.")
            return
        try:
            query.answer()
        except Exception:
            pass
        # turn the same message into progress
        try:
            query.edit_message_text(build_reset_progress_text(0, SPINNER[0]))
            start_reset_progress(context, query.message.chat_id, query.message.message_id)
        except Exception:
            pass
        return

    if qd == "reset_cancel":
        if uid != str(ADMIN_ID):
            alert("Admin only.")
            return
        try:
            query.answer()
            query.edit_message_text("❌ Reset cancelled.")
        except Exception:
            pass
        return

    # unban choose
    if qd == "unban_permanent":
        if uid != str(ADMIN_ID):
            alert("Admin only.")
            return
        admin_state = "unban_permanent_input"
        try:
            query.answer()
            query.edit_message_text("Send User ID (or @name | id) to unban from Permanent Block:")
        except Exception:
            pass
        return

    if qd == "unban_oldwinner":
        if uid != str(ADMIN_ID):
            alert("Admin only.")
            return
        admin_state = "unban_oldwinner_input"
        try:
            query.answer()
            query.edit_message_text("Send User ID (or @name | id) to unban from Old Winner Block:")
        except Exception:
            pass
        return

    # removeban choose
    if qd in ("reset_permanent_ban", "reset_oldwinner_ban"):
        if uid != str(ADMIN_ID):
            alert("Admin only.")
            return
        try:
            query.answer()
        except Exception:
            pass

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
        try:
            query.answer()
            query.edit_message_text("Cancelled.")
        except Exception:
            pass
        return

    if qd == "confirm_reset_permanent":
        if uid != str(ADMIN_ID):
            alert("Admin only.")
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
            alert("Admin only.")
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

    # JOIN GIVEAWAY
    if qd == "join_giveaway":
        # active check
        if not data.get("active"):
            alert("This giveaway is not active right now.")
            return

        # verify required
        if not verify_user_join(context.bot, int(uid)):
            alert(popup_verify_required())
            return

        # already joined?
        parts = data.get("participants", {}) or {}
        if uid in parts:
            # first winner clicking again -> show first winner popup always
            if data.get("first_winner_id") and str(data.get("first_winner_id")) == uid:
                uname = user_tag(query.from_user.username or "") or data.get("first_winner_username", "") or "@username"
                alert(popup_first_winner(uname, uid))
                return
            alert(popup_already_joined())
            return

        # join success
        tg_user = query.from_user
        uname = user_tag(tg_user.username or "")
        full_name = (tg_user.full_name or "").strip()

        with lock:
            # first join becomes first winner
            if not data.get("first_winner_id"):
                data["first_winner_id"] = uid
                data["first_winner_username"] = uname
                data["first_winner_name"] = full_name

            data["participants"][uid] = {"username": uname, "name": full_name}
            save_data()

        # instant update live post (participants + time)
        try:
            live_mid = data.get("live_message_id")
            start_ts = data.get("start_time")
            if live_mid and start_ts:
                duration = int(data.get("duration_seconds", 1)) or 1
                elapsed = int(datetime.utcnow().timestamp() - float(start_ts))
                remaining = max(0, duration - elapsed)
                percent = int(round(((duration - remaining) / float(duration)) * 100)) if duration else 0
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=live_mid,
                    text=build_live_post_text(remaining, percent),
                    reply_markup=join_button_markup(),
                )
        except Exception:
            pass

        # popup for first winner or normal join
        if str(data.get("first_winner_id")) == uid:
            alert(popup_first_winner(uname or "@username", uid))
        else:
            alert(popup_join_success(uname or "@Username", uid))
        return

    # Winners approve/reject (manual only)
    if qd == "winners_approve":
        if uid != str(ADMIN_ID):
            alert("Admin only.")
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
                data["winners_posted_at"] = datetime.utcnow().timestamp()
                save_winner_history_entry()
                save_data()
            query.edit_message_text("✅ Approved! Winners list posted to channel.")
        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post winners in channel: {e}")
            except Exception:
                pass
        return

    if qd == "winners_reject":
        if uid != str(ADMIN_ID):
            alert("Admin only.")
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

    # CLAIM PRIZE (with 24h expiry)
    if qd == "claim_prize":
        winners = data.get("winners", {}) or {}
        if uid in winners:
            # expiry check
            posted_at = data.get("winners_posted_at")
            if posted_at:
                if (datetime.utcnow().timestamp() - float(posted_at)) > CLAIM_EXPIRE_SECONDS:
                    alert(popup_claim_expired())
                    return
            uname = winners.get(uid, {}).get("username", "") or "@username"
            alert(popup_claim_winner(uname, uid))
        else:
            alert(popup_claim_not_winner_no_border())
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

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("panel", cmd_panel))

    dp.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))

    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))

    dp.add_handler(CommandHandler("winnerlist", cmd_winnerlist))
    dp.add_handler(CommandHandler("complete", cmd_complete))

    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("blockoldwinner", cmd_blockoldwinner))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))

    dp.add_handler(CommandHandler("reset", cmd_reset))

    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # resume live if active
    if data.get("active"):
        start_live_job(updater.job_queue)

    print("Bot is running (PTB 13, hosting-friendly) ...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
