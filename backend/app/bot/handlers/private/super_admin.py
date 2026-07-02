"""Super admin in-bot commands.

Only Telegram user IDs in `SUPER_ADMIN_TELEGRAM_IDS` env can use these.

Commands:
- /sa_help                          — list available commands
- /sa_grant <user_id> <amount>      — grant diamonds (system mint)
- /sa_revoke <user_id> <amount>     — revoke diamonds
- /sa_setdollars <user_id> <amount> — set dollars
- /sa_premium <user_id> <days>      — grant premium for N days
- /sa_ban <user_id> [reason]        — ban user
- /sa_unban <user_id>               — unban user
- /sa_userinfo <user_id>            — show user stats summary
- /sa_groups                        — list active groups
- /sa_broadcast <text>              — DM all users (use sparingly)
- /sa_stats                         — global KPI snapshot
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from app.bot.filters.super_admin import IsSuperAdmin
from app.db.models import (
    Game,
    Group,
    Transaction,
    TransactionStatus,
    TransactionType,
    User,
)
from app.services.audit_service import log_action
from app.services.i18n_service import get_translator

router = Router(name="private_super_admin")


def _sa_locale(user_id: int | None = None) -> str:
    """Return the SA's locale from their User row, falling back to 'uz'."""
    # Sync helper — callers must fetch the User row themselves if needed.
    # Used in async handlers via `await _get_sa_locale(user_id)`.
    return "uz"


async def _get_sa_locale(user_id: int) -> str:
    """Async: fetch SA's locale from DB, default 'uz'."""
    sa_user = await User.get_or_none(id=user_id)
    if sa_user and hasattr(sa_user, "language_code") and sa_user.language_code:
        return sa_user.language_code
    return "uz"


router.message.filter(F.chat.type == "private", IsSuperAdmin())


# === /sa_dashboard — open WebApp ===


@router.message(Command("sa_dashboard", "sa_webapp", "sa_open"))
async def sa_dashboard(message: Message) -> None:
    """Send a WebApp button that opens the SuperAdmin dashboard."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

    from app.config import settings

    url = f"https://{settings.public_domain}/webapp/sa"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛡 SuperAdmin Dashboard", web_app=WebAppInfo(url=url))],
        ]
    )
    await message.answer(
        "<b>🛡 SuperAdmin paneli</b>\n\n"
        "WebApp orqali kirib barcha statistika, foydalanuvchilar, "
        "guruhlar va tizim sozlamalari bilan ishlang.",
        reply_markup=kb,
        parse_mode="HTML",
    )


# === /sa_help ===


@router.message(Command("sa_help"))
async def sa_help(message: Message) -> None:
    text = (
        "<b>🛡 Super admin komandalar</b>\n\n"
        "<u>🌐 WebApp dashboard:</u>\n"
        "/sa_dashboard — WebApp panelini ochish\n\n"
        "<u>Foydalanuvchi boshqaruvi:</u>\n"
        "/sa_grant &lt;user_id&gt; &lt;amount&gt; — olmos berish\n"
        "/sa_revoke &lt;user_id&gt; &lt;amount&gt; — olmos olib qo'yish\n"
        "/sa_setdollars &lt;user_id&gt; &lt;amount&gt; — dollar o'rnatish\n"
        "/sa_premium &lt;user_id&gt; &lt;days&gt; — premium berish\n"
        "/sa_grouppremium &lt;group_id&gt; &lt;days&gt; — guruhga premium berish (days=0 → bekor qilish)\n"
        "/sa_ban &lt;user_id&gt; [sabab] — ban qilish\n"
        "/sa_unban &lt;user_id&gt; — banni olib tashlash\n"
        "/sa_userinfo &lt;user_id&gt; — foydalanuvchi haqida\n\n"
        "<u>Tizim:</u>\n"
        "/sa_groups — aktiv guruhlar\n"
        "/sa_broadcast &lt;matn&gt; — barchaga DM\n"
        "/sa_stats — umumiy KPI\n\n"
        "<u>Narxlar va sozlamalar:</u>\n"
        "/sa_prices — barcha narxlar va sozlamalar\n"
        "/sa_setprice &lt;item&gt; &lt;currency&gt; &lt;amount&gt; — item narxi\n"
        "  Misol: /sa_setprice shield dollars 150\n"
        "/sa_setreward &lt;key&gt; &lt;value&gt; — g'olib mukofoti\n"
        "  Keys: win_short_dollars, win_long_dollars, long_threshold_minutes,\n"
        "        mafia_singleton_bonus, xp_per_win\n"
        "/sa_setrate &lt;rate&gt; — 1💎 = N💵 kursi\n"
    )
    await message.answer(text, parse_mode="HTML")


# === /sa_grant <user_id> <amount> ===


@router.message(Command("sa_grant"))
async def sa_grant(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer(
            "Foydalanish: <code>/sa_grant &lt;user_id&gt; &lt;amount&gt;</code>", parse_mode="HTML"
        )
        return
    try:
        target_id, amount = int(parts[0]), int(parts[1])
    except ValueError:
        await message.answer("❌ Raqamlar noto'g'ri")
        return

    target = await User.get_or_none(id=target_id)
    if target is None:
        await message.answer(f"❌ User {target_id} topilmadi")
        return

    target.diamonds += amount
    await target.save(update_fields=["diamonds"])
    await Transaction.create(
        user=target,
        type=TransactionType.ADMIN_GRANT,
        diamonds_amount=amount,
        status=TransactionStatus.COMPLETED,
        note=f"Super admin grant by tg_id={message.from_user.id}",
    )
    await log_action(
        action="sa.grant.diamonds",
        target_type="user",
        target_id=str(target_id),
        payload={"amount": amount, "actor_tg_id": message.from_user.id},
    )
    await message.answer(
        f"✅ {amount} 💎 berildi {target.first_name} (id={target_id})\n"
        f"Yangi balans: <b>{target.diamonds}</b> 💎",
        parse_mode="HTML",
    )


# === /sa_revoke <user_id> <amount> ===


@router.message(Command("sa_revoke"))
async def sa_revoke(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer(
            "Foydalanish: <code>/sa_revoke &lt;user_id&gt; &lt;amount&gt;</code>", parse_mode="HTML"
        )
        return
    try:
        target_id, amount = int(parts[0]), int(parts[1])
    except ValueError:
        await message.answer("❌ Raqamlar noto'g'ri")
        return

    target = await User.get_or_none(id=target_id)
    if target is None:
        await message.answer(f"❌ User {target_id} topilmadi")
        return

    actual = min(target.diamonds, amount)
    target.diamonds -= actual
    await target.save(update_fields=["diamonds"])
    await Transaction.create(
        user=target,
        type=TransactionType.ADMIN_REVOKE,
        diamonds_amount=-actual,
        status=TransactionStatus.COMPLETED,
        note=f"Super admin revoke by tg_id={message.from_user.id}",
    )
    await log_action(
        action="sa.revoke.diamonds",
        target_type="user",
        target_id=str(target_id),
        payload={"amount": actual, "actor_tg_id": message.from_user.id},
    )
    await message.answer(
        f"✅ {actual} 💎 olib qo'yildi (talab: {amount}). Yangi balans: <b>{target.diamonds}</b> 💎",
        parse_mode="HTML",
    )


# === /sa_setdollars <user_id> <amount> ===


@router.message(Command("sa_setdollars"))
async def sa_setdollars(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer(
            "Foydalanish: <code>/sa_setdollars &lt;user_id&gt; &lt;amount&gt;</code>",
            parse_mode="HTML",
        )
        return
    try:
        target_id, amount = int(parts[0]), int(parts[1])
    except ValueError:
        await message.answer("❌ Raqamlar noto'g'ri")
        return

    target = await User.get_or_none(id=target_id)
    if target is None:
        await message.answer(f"❌ User {target_id} topilmadi")
        return

    delta = amount - target.dollars
    target.dollars = amount
    await target.save(update_fields=["dollars"])
    await log_action(
        action="sa.set.dollars",
        target_type="user",
        target_id=str(target_id),
        payload={"set_to": amount, "delta": delta, "actor_tg_id": message.from_user.id},
    )
    await message.answer(f"✅ Dollar o'rnatildi: <b>{amount}</b> 💵", parse_mode="HTML")


# === /sa_premium <user_id> <days> ===


@router.message(Command("sa_premium"))
async def sa_premium(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer(
            "Foydalanish: <code>/sa_premium &lt;user_id&gt; &lt;days&gt;</code>", parse_mode="HTML"
        )
        return
    try:
        target_id, days = int(parts[0]), int(parts[1])
    except ValueError:
        await message.answer("❌ Raqamlar noto'g'ri")
        return

    target = await User.get_or_none(id=target_id)
    if target is None:
        await message.answer(f"❌ User {target_id} topilmadi")
        return

    now = datetime.now(UTC)
    base = (
        target.premium_expires_at
        if target.premium_expires_at and target.premium_expires_at > now
        else now
    )
    target.premium_expires_at = base + timedelta(days=days)
    target.is_premium = True
    await target.save(update_fields=["premium_expires_at", "is_premium"])
    await log_action(
        action="sa.grant.premium",
        target_type="user",
        target_id=str(target_id),
        payload={
            "days": days,
            "until": target.premium_expires_at.isoformat(),
            "actor_tg_id": message.from_user.id,
        },
    )
    await message.answer(
        f"✅ Premium berildi: {days} kun\nMuddat: <b>{target.premium_expires_at:%Y-%m-%d %H:%M}</b>",
        parse_mode="HTML",
    )


# === /sa_grouppremium <group_id> <days> ===


@router.message(Command("sa_grouppremium"))
async def sa_grouppremium(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer(
            "Foydalanish: <code>/sa_grouppremium &lt;group_id&gt; &lt;days&gt;</code>\n"
            "<code>days=0</code> bo'lsa premium bekor qilinadi.",
            parse_mode="HTML",
        )
        return
    try:
        group_id, days = int(parts[0]), int(parts[1])
    except ValueError:
        await message.answer("❌ Raqamlar noto'g'ri")
        return

    from app.services import payment_service

    try:
        await payment_service.grant_group_premium(group_id, days)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return

    group = await Group.get(id=group_id)
    await log_action(
        action="sa.grant.group_premium",
        target_type="group",
        target_id=str(group_id),
        payload={"days": days, "actor_tg_id": message.from_user.id},
    )
    if days <= 0:
        await message.answer(
            f"🚫 Premium o'chirildi: <b>{group.title}</b> (id={group_id})", parse_mode="HTML"
        )
    else:
        until = group.premium_expires_at
        await message.answer(
            f"✅ Premium berildi: <b>{group.title}</b> (id={group_id})\n"
            f"Davomiylik: {days} kun\n"
            f"Muddat: <b>{until:%Y-%m-%d %H:%M}</b>",
            parse_mode="HTML",
        )


# === /sa_ban <user_id> [reason] ===


@router.message(Command("sa_ban"))
async def sa_ban(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split(maxsplit=1)
    if not parts:
        await message.answer(
            "Foydalanish: <code>/sa_ban &lt;user_id&gt; [sabab]</code>", parse_mode="HTML"
        )
        return
    try:
        target_id = int(parts[0])
    except ValueError:
        await message.answer("❌ user_id noto'g'ri")
        return
    reason = parts[1] if len(parts) > 1 else "No reason given"

    target = await User.get_or_none(id=target_id)
    if target is None:
        await message.answer(f"❌ User {target_id} topilmadi")
        return

    target.is_banned = True
    target.ban_reason = reason
    await target.save(update_fields=["is_banned", "ban_reason"])
    await log_action(
        action="sa.ban",
        target_type="user",
        target_id=str(target_id),
        payload={"reason": reason, "actor_tg_id": message.from_user.id},
    )
    await message.answer(
        f"🚫 {target.first_name} (id={target_id}) banlandi.\nSabab: {reason}",
        parse_mode="HTML",
    )


# === /sa_unban <user_id> ===


@router.message(Command("sa_unban"))
async def sa_unban(message: Message, command: CommandObject) -> None:
    try:
        target_id = int((command.args or "").strip())
    except ValueError:
        await message.answer(
            "Foydalanish: <code>/sa_unban &lt;user_id&gt;</code>", parse_mode="HTML"
        )
        return

    target = await User.get_or_none(id=target_id)
    if target is None:
        await message.answer(f"❌ User {target_id} topilmadi")
        return

    target.is_banned = False
    target.ban_reason = None
    await target.save(update_fields=["is_banned", "ban_reason"])
    await log_action(
        action="sa.unban",
        target_type="user",
        target_id=str(target_id),
        payload={"actor_tg_id": message.from_user.id},
    )
    await message.answer(f"✅ {target.first_name} unban qilindi")


# === /sa_userinfo <user_id> ===


@router.message(Command("sa_userinfo"))
async def sa_userinfo(message: Message, command: CommandObject) -> None:
    try:
        target_id = int((command.args or "").strip())
    except ValueError:
        await message.answer(
            "Foydalanish: <code>/sa_userinfo &lt;user_id&gt;</code>", parse_mode="HTML"
        )
        return

    target = await User.get_or_none(id=target_id)
    if target is None:
        await message.answer(f"❌ User {target_id} topilmadi")
        return

    await target.fetch_related("stats")
    s = target.stats  # type: ignore[attr-defined]
    text = (
        f"👤 <b>{target.first_name}</b> {target.last_name or ''}\n"
        f"id: <code>{target.id}</code>\n"
        f"username: {('@' + target.username) if target.username else '—'}\n\n"
        f"💎 <b>{target.diamonds}</b>  💵 {target.dollars}  ⭐ {target.xp}\n"
        f"Level: {target.level}\n"
        f"Premium: {'✅' if target.is_premium else '❌'}\n"
        f"Banned: {'🚫 ' + (target.ban_reason or 'yes') if target.is_banned else '❌'}\n"
        f"Joined: {target.joined_at:%Y-%m-%d}\n"
    )
    if s is not None:
        wr = (s.games_won / s.games_total * 100) if s.games_total else 0
        text += (
            f"\n<b>Statistika</b>\n"
            f"O'yinlar: {s.games_total}, Wins: {s.games_won} (WR {wr:.1f}%)\n"
            f"ELO: {s.elo}, Streak: {s.longest_win_streak}\n"
        )
    await message.answer(text, parse_mode="HTML")


# === /sa_groups ===


@router.message(Command("sa_groups"))
async def sa_groups(message: Message) -> None:
    groups = await Group.filter(is_active=True).limit(20)
    if not groups:
        await message.answer("Aktiv guruhlar yo'q")
        return
    lines = ["<b>🏘 Aktiv guruhlar (top 20)</b>\n"]
    for g in groups:
        lines.append(f"<code>{g.id}</code> — {g.title}")
    await message.answer("\n".join(lines), parse_mode="HTML")


# === /sa_broadcast <text> ===


@router.message(Command("sa_broadcast"))
async def sa_broadcast(message: Message, command: CommandObject, bot: Bot) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.answer(
            "Foydalanish: <code>/sa_broadcast &lt;matn&gt;</code> — barcha foydalanuvchilarga DM",
            parse_mode="HTML",
        )
        return

    users = await User.filter(is_banned=False).all()
    sent, failed = 0, 0
    for u in users:
        try:
            await bot.send_message(u.id, text)
            sent += 1
        except Exception:
            failed += 1
    await log_action(
        action="sa.broadcast",
        target_type="all",
        target_id="*",
        payload={
            "sent": sent,
            "failed": failed,
            "len": len(text),
            "actor_tg_id": message.from_user.id,
        },
    )
    await message.answer(f"📡 Yuborildi: {sent}, xato: {failed}")


# === /sa_stats ===


@router.message(Command("sa_stats"))
async def sa_stats(message: Message) -> None:
    users_total = await User.all().count()
    users_premium = await User.filter(is_premium=True).count()
    users_banned = await User.filter(is_banned=True).count()
    groups_total = await Group.filter(is_active=True).count()
    games_total = await Game.all().count()
    games_running = await Game.filter(status="running").count()

    text = (
        "<b>📊 Global KPI</b>\n\n"
        f"👤 Foydalanuvchilar: <b>{users_total}</b> "
        f"(💎 {users_premium} premium, 🚫 {users_banned} banned)\n"
        f"🏘 Aktiv guruhlar: <b>{groups_total}</b>\n"
        f"🎲 O'yinlar: <b>{games_total}</b> jami, <b>{games_running}</b> hozir o'ynalmoqda\n"
    )
    await message.answer(text, parse_mode="HTML")


# === Pricing commands ===


@router.message(Command("sa_prices"))
async def sa_prices(message: Message) -> None:
    """Show current prices, rewards, exchange-rate."""
    from app.services import pricing_service

    s = await pricing_service.get_settings(force=True)
    lines = ["<b>💎 Narxlar</b>\n"]
    lines.append("<u>Items:</u>")
    for code, spec in (s.item_prices or {}).items():
        d = int(spec.get("dollars", 0))
        dia = int(spec.get("diamonds", 0))
        parts = []
        if dia > 0:
            parts.append(f"💎 {dia}")
        if d > 0:
            parts.append(f"💵 {d}")
        if not parts:
            parts.append("⛔ disabled")
        lines.append(f"  <code>{code}</code> — {' / '.join(parts)}")

    lines.append("\n<u>Rewards:</u>")
    for k, v in (s.rewards or {}).items():
        lines.append(f"  <code>{k}</code> = {v}")

    lines.append("\n<u>Exchange:</u>")
    for k, v in (s.exchange or {}).items():
        lines.append(f"  <code>{k}</code> = {v}")

    lines.append("\n<u>Premium:</u>")
    for k, v in (s.premium or {}).items():
        lines.append(f"  <code>{k}</code> = {v}")

    if s.updated_at:
        lines.append(f"\n<i>Oxirgi yangilanish: {s.updated_at:%Y-%m-%d %H:%M}</i>")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("sa_setprice"))
async def sa_setprice(message: Message, command: CommandObject) -> None:
    """Set item price: /sa_setprice <item> <currency:dollars|diamonds> <amount>"""
    from app.services import pricing_service

    parts = (command.args or "").split()
    if len(parts) != 3:
        await message.answer(
            "Foydalanish: <code>/sa_setprice &lt;item&gt; &lt;dollars|diamonds&gt; &lt;amount&gt;</code>\n"
            "Misol: <code>/sa_setprice shield dollars 150</code>",
            parse_mode="HTML",
        )
        return

    item, currency, amount_str = parts
    if currency not in ("dollars", "diamonds"):
        await message.answer("❌ Currency: 'dollars' yoki 'diamonds'")
        return
    try:
        amount = int(amount_str)
    except ValueError:
        await message.answer("❌ Miqdor noto'g'ri")
        return
    if amount < 0:
        await message.answer("❌ Miqdor manfiy bo'lishi mumkin emas")
        return

    try:
        await pricing_service.update_setting(
            "item_prices", f"{item}.{currency}", amount, by_tg_id=message.from_user.id
        )
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return

    await log_action(
        action="sa.set.price",
        target_type="item_price",
        target_id=item,
        payload={
            "currency": currency,
            "amount": amount,
            "actor_tg_id": message.from_user.id,
        },
    )
    await message.answer(f"✅ <code>{item}.{currency}</code> = <b>{amount}</b>", parse_mode="HTML")


@router.message(Command("sa_setreward"))
async def sa_setreward(message: Message, command: CommandObject) -> None:
    """Set reward parameter: /sa_setreward <key> <value>"""
    from app.services import pricing_service

    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer(
            "Foydalanish: <code>/sa_setreward &lt;key&gt; &lt;value&gt;</code>\n"
            "Keys: win_short_dollars, win_long_dollars, long_threshold_minutes,\n"
            "      mafia_singleton_bonus, xp_per_win, xp_per_game, xp_per_survive",
            parse_mode="HTML",
        )
        return

    key, value_str = parts
    try:
        value = int(value_str)
    except ValueError:
        await message.answer("❌ Qiymat butun son bo'lishi kerak")
        return

    valid_keys = {
        "win_short_dollars",
        "win_long_dollars",
        "long_threshold_minutes",
        "mafia_singleton_bonus",
        "xp_per_game",
        "xp_per_win",
        "xp_per_survive",
    }
    if key not in valid_keys:
        await message.answer(f"❌ Unknown key. Valid: {', '.join(sorted(valid_keys))}")
        return

    await pricing_service.update_setting("rewards", key, value, by_tg_id=message.from_user.id)
    await log_action(
        action="sa.set.reward",
        target_type="reward",
        target_id=key,
        payload={"value": value, "actor_tg_id": message.from_user.id},
    )
    await message.answer(f"✅ Reward <code>{key}</code> = <b>{value}</b>", parse_mode="HTML")


@router.message(Command("sa_setrate"))
async def sa_setrate(message: Message, command: CommandObject) -> None:
    """Set diamond → dollar exchange rate: /sa_setrate <N>  (1💎 = N💵)"""
    from app.services import pricing_service

    try:
        rate = int((command.args or "").strip())
    except ValueError:
        await message.answer(
            "Foydalanish: <code>/sa_setrate &lt;rate&gt;</code> — 1💎 = N💵\n"
            "Misol: <code>/sa_setrate 1000</code>",
            parse_mode="HTML",
        )
        return
    if rate < 1:
        await message.answer("❌ Kurs 1 dan kam bo'lishi mumkin emas")
        return

    await pricing_service.update_setting(
        "exchange", "diamond_to_dollar_rate", rate, by_tg_id=message.from_user.id
    )
    await log_action(
        action="sa.set.exchange_rate",
        target_type="exchange",
        target_id="rate",
        payload={"rate": rate, "actor_tg_id": message.from_user.id},
    )
    await message.answer(f"✅ Yangi kurs: <b>1💎 = {rate}💵</b>", parse_mode="HTML")


# ===========================================================
# Forwarded-message broadcast prompt
# ===========================================================
#
# When a super-admin forwards / sends any non-command, non-empty
# message to the bot in private chat, we offer two ways to fan it
# out across every active user:
#
#   📋 Copy    — `copyMessage`, recipients see the bot as the sender
#                (no "Forwarded from …" header). Best for system
#                announcements.
#   ↗ Forward  — `forwardMessage`, preserves the original author's
#                name. Best for sharing news posts / channel content.
#
# Tapping a button creates a `BroadcastRun` row and detaches the
# worker. The SA receives an immediate ack here and a full report
# DM when the worker finishes.
#
# Routing notes: the `super_admin` router is included BEFORE
# `last_words` in `dispatcher.setup_routers`, so a SA's forward
# never falls through to the mafia-chat / last-words handler. The
# filter also requires text/caption to avoid offering broadcast on
# bare stickers (which neither copyMessage nor a meaningful summary
# can carry well — admins can always re-send through the dialog if
# they really want).


def _build_broadcast_prompt_kb(message_id: int) -> InlineKeyboardMarkup:
    """Two-button keyboard wired to the broadcast worker.

    Callback data shape: `sa:bcast:<method>:<source_message_id>`.
    `source_chat_id` isn't packed — the worker reads it from the
    callback query's chat (always the SA's private chat with the
    bot, which is where the prompt was issued).
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Copy",
                    callback_data=f"sa:bcast:copy:{message_id}",
                ),
                InlineKeyboardButton(
                    text="↗ Forward",
                    callback_data=f"sa:bcast:forward:{message_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Bekor qilish",
                    callback_data="sa:bcast:cancel",
                ),
            ],
        ]
    )


@router.message(F.text & ~F.text.startswith("/"))
async def sa_broadcast_prompt_text(message: Message) -> None:
    """Catch a plain text SA → bot DM and offer the broadcast dialog."""
    if message.from_user is None or message.text is None:
        return
    # Skip empty / whitespace-only payloads — nothing to broadcast.
    if not message.text.strip():
        return
    locale = await _get_sa_locale(message.from_user.id)
    t = get_translator(locale)
    await message.answer(
        t("sa-broadcast-prompt-text"),
        parse_mode="HTML",
        reply_markup=_build_broadcast_prompt_kb(message.message_id),
    )


@router.message(F.forward_origin)
async def sa_broadcast_prompt_forward(message: Message) -> None:
    """Catch a forwarded message (any media type) — same dialog as text."""
    if message.from_user is None:
        return
    locale = await _get_sa_locale(message.from_user.id)
    t = get_translator(locale)
    await message.answer(
        t("sa-broadcast-prompt-forward"),
        parse_mode="HTML",
        reply_markup=_build_broadcast_prompt_kb(message.message_id),
    )


@router.message(F.caption & (F.photo | F.video | F.animation | F.document))
async def sa_broadcast_prompt_media(message: Message) -> None:
    """Catch a media + caption SA → bot DM (e.g. a photo with text)."""
    if message.from_user is None:
        return
    locale = await _get_sa_locale(message.from_user.id)
    t = get_translator(locale)
    await message.answer(
        t("sa-broadcast-prompt-media"),
        parse_mode="HTML",
        reply_markup=_build_broadcast_prompt_kb(message.message_id),
    )


@router.callback_query(F.data == "sa:bcast:cancel")
async def cb_broadcast_cancel(query: CallbackQuery) -> None:
    import contextlib

    locale = await _get_sa_locale(query.from_user.id if query.from_user else 0)
    t = get_translator(locale)
    await query.answer(t("sa-broadcast-toast-cancelled"), show_alert=False)
    if isinstance(query.message, Message):
        with contextlib.suppress(Exception):
            await query.message.edit_text(t("sa-broadcast-cancelled-msg"))


@router.callback_query(F.data.startswith("sa:bcast:"))
async def cb_broadcast_choose_method(query: CallbackQuery) -> None:
    """Dispatch to `broadcast_service.schedule_broadcast`.

    Callback data: `sa:bcast:<copy|forward>:<source_message_id>`.
    """
    if query.data is None or query.from_user is None:
        await query.answer()
        return
    parts = query.data.split(":")
    locale = await _get_sa_locale(query.from_user.id)
    t = get_translator(locale)
    if len(parts) != 4 or parts[2] not in ("copy", "forward"):
        await query.answer(t("sa-broadcast-toast-invalid-request"), show_alert=True)
        return
    method_str = parts[2]
    try:
        source_message_id = int(parts[3])
    except ValueError:
        await query.answer(t("sa-broadcast-toast-invalid-message-id"), show_alert=True)
        return

    if not isinstance(query.message, Message):
        await query.answer()
        return

    # The prompt was posted in the SA's private chat with the bot —
    # the original-message chat is the same chat.
    source_chat_id = query.from_user.id

    from app.db.models import BroadcastMethod
    from app.services import broadcast_service

    method = BroadcastMethod.COPY if method_str == "copy" else BroadcastMethod.FORWARD
    run = await broadcast_service.schedule_broadcast(
        initiator_tg_id=query.from_user.id,
        method=method,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
    )

    import contextlib

    await query.answer(t("sa-broadcast-toast-started"), show_alert=False)
    with contextlib.suppress(Exception):
        await query.message.edit_text(
            t("sa-broadcast-started-msg", method=method.value, run_id=str(run.id)),
            parse_mode="HTML",
        )

    await log_action(
        action="sa.broadcast.start",
        target_type="broadcast",
        target_id=str(run.id),
        payload={
            "method": method.value,
            "source_chat_id": source_chat_id,
            "source_message_id": source_message_id,
            "actor_tg_id": query.from_user.id,
        },
    )


logger.info("Super admin handlers loaded")
