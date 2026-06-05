"""
Telegram Bot для моніторингу принтерів Bambu Lab
Паралельний запуск з printer_monitor з можливістю налаштування через бота
"""

import asyncio
import html
import json
import logging
import re
import socket
from typing import Dict, Optional, List
from datetime import datetime, timedelta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, 
    CommandHandler, 
    CallbackQueryHandler, 
    MessageHandler,
    ContextTypes,
    filters
)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError, Conflict

from printer_monitor import BambuPrinterMonitor, PrinterStatus, BAMBU_EXE_PATH
from version import __version__
import appconfig

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('telegram_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ],
    force=True  # Перезаписати попередню конфігурацію логування
)
logger = logging.getLogger(__name__)

# Приховуємо токен в HTTP логах
logging.getLogger("httpx").setLevel(logging.WARNING)


class BambuTelegramBot:
    """Telegram бот для управління моніторингом принтерів"""
    
    def __init__(self, config_path: Optional[str] = None):
        """
        Args:
            config_path: Шлях до config.json (типово поруч з exe / у поточній папці)
        """
        self.config_path = Path(config_path) if config_path else appconfig.config_path()
        self.config = self.load_config()
        self.monitor: Optional[BambuPrinterMonitor] = None
        self.application: Optional[Application] = None
        self.monitor_task: Optional[asyncio.Task] = None
        self.event_loop: Optional[asyncio.AbstractEventLoop] = None
        # Словник для зберігання активних статусних повідомлень {chat_id: message_id}
        self.active_status_messages: Dict[int, int] = {}
        # Буфер для накопичення сповіщень
        self.notification_buffer: List[Dict] = []
        self.notification_timer: Optional[asyncio.Task] = None
        self.notification_batch_delay: float = 2.0  # Затримка в секундах для батчингу
        
    def load_config(self) -> dict:
        """Завантаження конфігурації. Якщо файлу нема — створюємо дефолтний поруч."""
        if not self.config_path.exists():
            logger.warning(f"config.json не знайдено, створюю дефолтний: {self.config_path}")
            try:
                self.config_path.write_text(
                    json.dumps(appconfig.DEFAULT_CONFIG, indent=2, ensure_ascii=False),
                    encoding='utf-8')
            except Exception as e:
                logger.error(f"Не вдалося створити config.json: {e}")
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except Exception as e:
            logger.error(f"Не вдалося прочитати config.json ({e}); беру дефолт")
            config = dict(appconfig.DEFAULT_CONFIG)
        logger.info("✓ Конфігурацію завантажено")
        return config
    
    def save_config(self):
        """Збереження конфігурації"""
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)
        logger.info("✓ Конфігурацію збережено")
    
    def is_authorized(self, user_id: int) -> bool:
        """Перевірка чи користувач авторизований"""
        allowed_users = self.config.get('telegram', {}).get('allowed_users', [])
        return user_id in allowed_users or len(allowed_users) == 0
    
    def is_group_authorized(self, chat_id: int) -> bool:
        """Перевірка чи група авторизована"""
        allowed_groups = self.config.get('telegram', {}).get('allowed_groups', [])
        return chat_id in allowed_groups
    
    async def check_authorization(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Перевірка авторизації з відповіддю"""
        chat_type = update.effective_chat.type
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        # Якщо це група - перевіряємо чи група в списку дозволених
        if chat_type in ['group', 'supergroup']:
            if self.is_group_authorized(chat_id):
                return True
            # Якщо групи немає в списку - не відповідаємо (щоб не спамити в чужих групах)
            return False
        
        # Для приватних чатів - перевіряємо користувача
        if not self.is_authorized(user_id):
            if update.message:
                await update.message.reply_text(
                    "❌ У вас немає доступу до цього бота.\n"
                    f"Ваш ID: {user_id}\n\n"
                    "Додайте свій ID до конфігураційного файлу (config.json) у розділ telegram.allowed_users"
                )
            return False
        return True
    
    # === КОМАНДИ БОТА ===
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start"""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type  # 'private', 'group', 'supergroup', 'channel'
        
        # Додаємо першого користувача автоматично
        allowed_users = self.config.get('telegram', {}).get('allowed_users', [])
        if len(allowed_users) == 0:
            self.config['telegram']['allowed_users'] = [user_id]
            self.save_config()
            logger.info(f"✓ Додано першого користувача: {user_id}")
        
        # Якщо це група - автоматично додаємо її до списку дозволених груп
        if chat_type in ['group', 'supergroup']:
            if 'allowed_groups' not in self.config['telegram']:
                self.config['telegram']['allowed_groups'] = []
            
            if chat_id not in self.config['telegram']['allowed_groups']:
                self.config['telegram']['allowed_groups'].append(chat_id)
                self.save_config()
                logger.info(f"✓ Додано групу: {chat_id} ({update.effective_chat.title})")
                await update.message.reply_text(
                    f"✅ Групу '{update.effective_chat.title}' додано до списку сповіщень!\n"
                    f"Chat ID: {chat_id}"
                )
                return
        
        if not await self.check_authorization(update, context):
            return
        
        keyboard = [
            [KeyboardButton("📊 Статус"), KeyboardButton("📋 Список принтерів")],
            [KeyboardButton("⚙️ Налаштування"), KeyboardButton("🔄 Перезапуск")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(
            "B A M B U\n"
            "**█ █ █ █**\n"
            "L A B\n\n"
            "Вітаю! Я допоможу моніторити\n"
            "вашу ферму принтерів Bambu Lab\n\n"
            "**⚡ Швидкі команди:**\n"
            "├ /status — Статус ферми\n"
            "├ /printers — Список принтерів\n"
            "├ /settings — Налаштування\n"
            "├ /restart — Перезапуск\n"
            "└ /help — Довідка\n\n"
            "━━━━━━━━━━━\n"
            "💡 Використовуйте кнопки нижче",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /status - загальний статус"""
        logger.info(f"⚡ Команда /status від користувача {update.effective_user.id} (@{update.effective_user.username})")
        
        if not await self.check_authorization(update, context):
            return
        
        if not self.monitor or not self.monitor.running:
            await update.message.reply_text("❌ Моніторинг не запущений. Використайте /restart")
            return
        
        chat_id = update.effective_chat.id
        
        # Видаляємо старе статусне повідомлення якщо воно існує
        if chat_id in self.active_status_messages:
            try:
                await self.application.bot.delete_message(
                    chat_id=chat_id,
                    message_id=self.active_status_messages[chat_id]
                )
            except Exception as e:
                logger.debug(f"Не вдалося видалити старе повідомлення: {e}")
        
        # Відправляємо нове повідомлення
        msg = self._generate_status_message()
        sent_message = await update.message.reply_text(msg, parse_mode='HTML')
        
        # Зберігаємо message_id для автоматичного оновлення
        self.active_status_messages[chat_id] = sent_message.message_id
        logger.info(f"✓ Створено автооновлюване повідомлення статусу для чату {chat_id}")
    
    async def cmd_printers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /printers - детальна інформація"""
        logger.info(f"⚡ Команда /printers від користувача {update.effective_user.id} (@{update.effective_user.username})")
        
        if not await self.check_authorization(update, context):
            return
        
        if not self.monitor or not self.monitor.running:
            await update.message.reply_text("❌ Моніторинг не запущений. Використайте /restart")
            return
        
        printers = self.monitor.get_all_printers()
        
        if not printers:
            await update.message.reply_text("📝 Принтери не знайдені")
            return
        
        # Створюємо кнопки для кожного принтера
        keyboard = []
        for i, printer in enumerate(printers):
            status_icon = "🟢" if printer.online else "🔴"
            # Іконки для різних статусів
            work_icons = {
                'printing': '▶️',
                'idle': '⏸️',
                'finished': '✅',
                'stopped': '🛑',
                'offline': '❌'
            }
            work_icon = work_icons.get(printer.status, '❓')
            button_text = f"{status_icon} {work_icon} {printer.name}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"printer_{i}")])
        
        keyboard.append([InlineKeyboardButton("🔄 Оновити", callback_data="printers_refresh")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        summary = self.monitor.get_summary()
        msg = (
            "🖨️ **СПИСОК ПРИНТЕРІВ**\n\n"
            f"Всього: {summary['total']}\n"
            f"🟢 Онлайн: {summary['online']}\n"
            f"▶️ Друкують: {summary['printing']}\n"
            f"✅ Завершили друк: `{summary['finished']}`\n"
            f"⏸️ На паузі: `{summary.get('paused', 0)}`\n"
            f"💤 Очікують: {summary['idle']}\n\n"
            "Виберіть принтер для детальної інформації:"
        )
        
        await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /settings - налаштування"""
        if not await self.check_authorization(update, context):
            return
        
        keyboard = [
            [InlineKeyboardButton("🔔 Сповіщення", callback_data="settings_notifications")],
            [InlineKeyboardButton("⏱️ Інтервал оновлення", callback_data="settings_interval")],
            [InlineKeyboardButton("🌐 Proxy", callback_data="settings_proxy")],
            [InlineKeyboardButton("👥 Користувачі", callback_data="settings_users")],
            [InlineKeyboardButton("🔄 Перезавантажити конфіг", callback_data="settings_reload")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "⚙️ **НАЛАШТУВАННЯ**\n\n"
            "Виберіть розділ для налаштування:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def cmd_settings_inline(self, query):
        """Повернення до головного меню налаштувань (для callback)"""
        keyboard = [
            [InlineKeyboardButton("🔔 Сповіщення", callback_data="settings_notifications")],
            [InlineKeyboardButton("⏱️ Інтервал оновлення", callback_data="settings_interval")],
            [InlineKeyboardButton("🌐 Proxy", callback_data="settings_proxy")],
            [InlineKeyboardButton("👥 Користувачі", callback_data="settings_users")],
            [InlineKeyboardButton("🔄 Перезавантажити конфіг", callback_data="settings_reload")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "⚙️ **НАЛАШТУВАННЯ**\n\n"
            "Виберіть розділ для налаштування:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /restart - перезапуск моніторингу"""
        if not await self.check_authorization(update, context):
            return
        
        await update.message.reply_text("🔄 Перезапускаю моніторинг...")
        
        # Зупиняємо старий моніторинг
        if self.monitor and self.monitor.running:
            self.monitor.stop()
        
        # Перезавантажуємо конфіг
        self.config = self.load_config()
        
        # Запускаємо новий
        success = await self.start_monitor()
        
        if success:
            await update.message.reply_text("✅ Моніторинг успішно перезапущено!")
        else:
            await update.message.reply_text("❌ Помилка перезапуску моніторингу")
    
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /help - допомога"""
        if not await self.check_authorization(update, context):
            return
        
        msg = (
            "┏━━━━━━━━━━━━━━━━━━━┓\n"
            "┃  📖 **ДОВІДКА**  ┃\n"
            "┗━━━━━━━━━━━━━━━━━━━┛\n\n"
            "**⚡ Команди**\n"
            "├ /start — Головне меню\n"
            "├ /status — Статус ферми (авто-оновлення)\n"
            "├ /printers — Детальна інформація\n"
            "├ /settings — Налаштування бота\n"
            "├ /restart — Перезапуск моніторингу\n"
            "├ /id — Дізнатись свій ID\n"
            "├ /version — Версія бота\n"
            "└ /help — Ця довідка\n\n"
            "**🔔 Сповіщення**\n"
            "Бот автоматично повідомляє про:\n"
            "├ ✅ Завершення друку\n"
            "├ 🛑 Зміну статусу\n"
            "├ 🟢 Підключення принтера\n"
            "├ 🔴 Відключення принтера\n"
            "└ 📊 Періодичні оновлення\n\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "💡 Налаштуйте через `/settings`"
        )
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def cmd_debug(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /debug - детальна інформація про принтери"""
        if not await self.check_authorization(update, context):
            return
        
        if not self.monitor or not self.monitor.running:
            await update.message.reply_text("❌ Моніторинг не запущений")
            return
        
        printers = self.monitor.get_all_printers()
        
        if not printers:
            await update.message.reply_text("📝 Принтери не знайдені")
            return
        
        msg = "🔧 DEBUG INFO\n\n"

        limit = 10
        for i, printer in enumerate(printers[:limit], 1):
            msg += f"═══ Принтер {i} ═══\n"
            msg += f"name: '{printer.name}' (type: {type(printer.name).__name__})\n"
            msg += f"model: '{printer.model}' (type: {type(printer.model).__name__})\n"
            msg += f"status: '{printer.status}' (type: {type(printer.status).__name__})\n"
            msg += f"progress: {printer.progress} (type: {type(printer.progress).__name__})\n"
            msg += f"online: {printer.online} (type: {type(printer.online).__name__})\n"
            msg += f"current_file: '{printer.current_file}' (type: {type(printer.current_file).__name__})\n"
            msg += f"remaining_time: '{printer.remaining_time}' (type: {type(printer.remaining_time).__name__})\n"
            msg += f"nozzle_temp: '{printer.nozzle_temp}' (type: {type(printer.nozzle_temp).__name__})\n"
            msg += f"bed_temp: '{printer.bed_temp}' (type: {type(printer.bed_temp).__name__})\n"
            msg += f"speed: '{printer.speed}' (type: {type(printer.speed).__name__})\n"
            msg += f"last_update: {printer.last_update}\n\n"

        if len(printers) > limit:
            msg += f"... та ще {len(printers) - limit} (показано перші {limit})\n\n"

        summary = self.monitor.get_summary()
        msg += "═══ Summary ═══\n"
        for key, value in summary.items():
            msg += f"{key}: {value} (type: {type(value).__name__})\n"
        
        # Відправляємо великі повідомлення частинами
        if len(msg) > 4000:
            for i in range(0, len(msg), 4000):
                await update.message.reply_text(msg[i:i+4000])
        else:
            await update.message.reply_text(msg)

    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /id — показати свій ID та ID чату (доступна без авторизації)"""
        user = update.effective_user
        chat = update.effective_chat
        lines = ["🪪 <b>Ваші ідентифікатори</b>", ""]
        if user:
            uname = f" (@{html.escape(user.username)})" if user.username else ""
            lines.append(f"👤 User ID: <code>{user.id}</code>{uname}")
        lines.append(f"💬 Chat ID: <code>{chat.id}</code> ({chat.type})")
        if chat.type in ("group", "supergroup") and chat.title:
            lines.append(f"📛 Назва: {html.escape(chat.title)}")
        lines += ["", "Додайте User ID у config.json → telegram.allowed_users"]
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')

    async def cmd_version(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /version — версія бота"""
        await update.message.reply_text(f"farmwatch v{__version__}")

    async def cmd_debuglog(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /debuglog [on|off] — перемкнути детальне логування монітора.

        Без аргументу — перемикає поточний стан. Лог пишеться у printer_monitor.debug.log,
        а сирий HTML дашборда при зникненні принтерів — у debug_dumps/.
        """
        if not await self.check_authorization(update, context):
            return
        if not self.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ Лише адмін може керувати логуванням")
            return

        arg = (context.args[0].lower() if context.args else "")
        if arg in ("on", "1", "true", "увімк", "вкл"):
            new_state = True
        elif arg in ("off", "0", "false", "вимк", "выкл"):
            new_state = False
        else:
            new_state = not self.config.get('monitor', {}).get('debug_logging', False)

        self.config.setdefault('monitor', {})['debug_logging'] = new_state
        self.save_config()
        if self.monitor:
            self.monitor.set_debug_logging(new_state)

        await update.message.reply_text(
            f"🔧 Детальне логування монітора: {'УВІМКНЕНО ✅' if new_state else 'вимкнено ❌'}\n\n"
            f"📄 Лог: <code>printer_monitor.debug.log</code>\n"
            f"💾 HTML-дампи при зникненні: <code>debug_dumps/</code>\n\n"
            f"Зникнення принтерів фіксується у логах <b>завжди</b> (рядки «⚠️ ПРИНТЕРИ ЗНИКЛИ»); "
            f"debug-режим додає сирий HTML для розбору.",
            parse_mode='HTML'
        )

    # === ОБРОБНИКИ CALLBACK ===
    
    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Головний обробник всіх callback'ів"""
        query = update.callback_query
        if not query:
            logger.error("❌ Немає callback_query в update!")
            return
            
        data = query.data
        logger.info(f"🔘 Callback: '{data}' від @{query.from_user.username} (ID: {query.from_user.id})")
        
        try:
            # Налаштування
            if data == "settings_notifications":
                await query.answer()
                await self.settings_notifications(query)
            elif data == "settings_interval":
                await query.answer()
                await self.settings_interval(query)
            elif data == "settings_proxy":
                await query.answer()
                await self.settings_proxy(query)
            elif data == "settings_users":
                await query.answer()
                await self.settings_users(query)
            elif data == "settings_reload":
                self.config = self.load_config()
                await query.answer("✅ Перезавантажено")
                await query.edit_message_text("✅ Конфігурацію перезавантажено!")
            elif data == "settings_back":
                await query.answer()
                await self.cmd_settings_inline(query)
            
            # Сповіщення
            elif data.startswith("notif_toggle_"):
                await self.toggle_notification(query, data.replace("notif_toggle_", ""))
            
            # Proxy
            elif data == "proxy_toggle":
                await self.toggle_proxy(query)
            
            # Інтервал
            elif data.startswith("interval_"):
                await self.set_interval(query, data.replace("interval_", ""))
            
            # Принтери
            elif data == "printers_refresh":
                await self.show_printers_list(query, refresh=True)
            elif data.startswith("printer_"):
                await query.answer()
                printer_index = int(data.replace("printer_", ""))
                await self.show_printer_details(query, printer_index)
            else:
                await query.answer()
        except Exception as e:
            logger.error(f"Помилка обробки callback {data}: {e}")
            await query.answer("❌ Помилка обробки")
    
    async def settings_notifications(self, query):
        """Налаштування сповіщень"""
        notif = self.config.get('notifications', {})
        
        def get_icon(enabled: bool) -> str:
            return "✅" if enabled else "❌"
        
        keyboard = [
            [InlineKeyboardButton(
                f"{get_icon(notif.get('status_changes', True))} Зміна статусу",
                callback_data="notif_toggle_status_changes"
            )],
            [InlineKeyboardButton(
                f"{get_icon(notif.get('print_complete', True))} Завершення друку",
                callback_data="notif_toggle_print_complete"
            )],
            [InlineKeyboardButton(
                f"{get_icon(notif.get('printer_online', True))} Принтер онлайн",
                callback_data="notif_toggle_printer_online"
            )],
            [InlineKeyboardButton(
                f"{get_icon(notif.get('printer_offline', True))} Принтер офлайн",
                callback_data="notif_toggle_printer_offline"
            )],
            [InlineKeyboardButton(
                f"{get_icon(notif.get('periodic_updates', False))} Періодичні оновлення",
                callback_data="notif_toggle_periodic_updates"
            )],
            [InlineKeyboardButton("◀️ Назад", callback_data="settings_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        msg = (
            "🔔 **НАЛАШТУВАННЯ СПОВІЩЕНЬ**\n\n"
            "Натисніть на пункт щоб увімкнути/вимкнути:"
        )
        
        await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def toggle_notification(self, query, notif_type: str):
        """Перемикання сповіщення"""
        if 'notifications' not in self.config:
            self.config['notifications'] = {}
        
        current = self.config['notifications'].get(notif_type, True)
        self.config['notifications'][notif_type] = not current
        self.save_config()
        
        status = "увімкнено" if not current else "вимкнено"
        await query.answer(f"✅ Сповіщення {status}")
        
        # Оновлюємо відображення
        await self.settings_notifications(query)
    
    async def settings_interval(self, query):
        """Налаштування інтервалу оновлення"""
        current = self.config.get('monitor', {}).get('update_interval', 30)
        
        def get_icon(seconds: int) -> str:
            return "✅" if current == seconds else "⚪"
        
        keyboard = [
            [InlineKeyboardButton(f"{get_icon(10)} ⚡ 10 сек", callback_data="interval_10")],
            [InlineKeyboardButton(f"{get_icon(30)} 🚀 30 сек (рекомендовано)", callback_data="interval_30")],
            [InlineKeyboardButton(f"{get_icon(60)} ⏱️ 60 сек", callback_data="interval_60")],
            [InlineKeyboardButton(f"{get_icon(120)} 🐢 120 сек", callback_data="interval_120")],
            [InlineKeyboardButton("◀️ Назад", callback_data="settings_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        msg = f"⏱️ **ІНТЕРВАЛ ОНОВЛЕННЯ**\n\nПоточний: {current} секунд\n\nВиберіть новий інтервал:"
        
        await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def set_interval(self, query, interval: str):
        """Встановлення інтервалу оновлення"""
        interval_sec = int(interval)
        
        if 'monitor' not in self.config:
            self.config['monitor'] = {}
        
        self.config['monitor']['update_interval'] = interval_sec
        self.save_config()
        
        # Оновлюємо інтервал у запущеному моніторі
        if self.monitor:
            self.monitor.update_interval = interval_sec
        
        await query.answer(f"✅ Інтервал змінено на {interval_sec} секунд")
        await self.settings_interval(query)
    
    async def settings_proxy(self, query):
        """Налаштування proxy"""
        proxy = self.config.get('telegram', {}).get('proxy', {})
        enabled = proxy.get('enabled', False)
        proxy_type = proxy.get('type', 'http')
        host = proxy.get('host', '127.0.0.1')
        port = proxy.get('port', 8080)
        
        keyboard = [
            [InlineKeyboardButton(
                f"{'✅' if enabled else '❌'} Увімкнути Proxy",
                callback_data="proxy_toggle"
            )],
            [InlineKeyboardButton("◀️ Назад", callback_data="settings_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        msg = (
            "🌐 **НАЛАШТУВАННЯ PROXY**\n\n"
            f"Статус: {'✅ Увімкнено' if enabled else '❌ Вимкнено'}\n"
            f"Тип: {proxy_type}\n"
            f"Адреса: {host}:{port}\n\n"
            "Для зміни адреси відредагуйте config.json"
        )
        
        await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def toggle_proxy(self, query):
        """Перемикання proxy"""
        if 'telegram' not in self.config:
            self.config['telegram'] = {}
        if 'proxy' not in self.config['telegram']:
            self.config['telegram']['proxy'] = {'enabled': False}
        
        current = self.config['telegram']['proxy'].get('enabled', False)
        self.config['telegram']['proxy']['enabled'] = not current
        self.save_config()
        
        status = "увімкнено" if not current else "вимкнено"
        await query.answer(f"✅ Proxy {status}. Перезапустіть бота: /restart")
        await self.settings_proxy(query)
    
    async def show_printers_list(self, query, refresh=False):
        """Відображення списку принтерів"""
        if not self.monitor or not self.monitor.running:
            await query.edit_message_text("❌ Моніторинг не запущений. Використайте /restart")
            return
        
        printers = self.monitor.get_all_printers()
        
        if not printers:
            await query.edit_message_text("📝 Принтери не знайдені")
            return
        
        # Створюємо кнопки для кожного принтера
        keyboard = []
        for i, printer in enumerate(printers):
            status_icon = "🟢" if printer.online else "🔴"
            # Іконки для різних статусів
            work_icons = {
                'printing': '▶️',
                'idle': '⏸️',
                'finished': '✅',
                'stopped': '🛑',
                'paused': '⏸️',
                'offline': '❌'
            }
            work_icon = work_icons.get(printer.status, '❓')
            button_text = f"{status_icon} {work_icon} {printer.name}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"printer_{i}")])
        
        keyboard.append([InlineKeyboardButton("🔄 Оновити", callback_data="printers_refresh")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        summary = self.monitor.get_summary()
        msg = (
            "┏━━━━━━━━━━━━━━━━━━━━━━┓\n"
            "┃ 🖨️ **ВАШІ ПРИНТЕРИ** ┃\n"
            "┗━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            f"**📊 Загалом:** `{summary['total']}` принтерів\n"
            f"├ 🟢 Онлайн: `{summary['online']}`\n"
            f"├ ▶️ Друкують: `{summary['printing']}`\n"
            f"├ ✅ Завершили друк: `{summary['finished']}`\n"
            f"├ ⏸️ На паузі: `{summary.get('paused', 0)}`\n"
            f"└ 💤 Очікують: `{summary['idle']}`\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "👇 Виберіть принтер:"
        )
        
        if refresh:
            await query.answer("✅ Оновлено")
        
        await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def show_printer_details(self, query, printer_index: int):
        """Відображення детальної інформації про принтер"""
        if not self.monitor or not self.monitor.running:
            await query.edit_message_text("❌ Моніторинг не запущений")
            return
        
        printers = self.monitor.get_all_printers()
        
        if printer_index >= len(printers):
            await query.answer("❌ Принтер не знайдено")
            return
        
        printer = printers[printer_index]
        
        # Створюємо безпечне повідомлення без Markdown для уникнення помилок парсингу
        status_emoji = {
            'printing': '🖨️',
            'idle': '⏸️',
            'offline': '❌',
            'finished': '✅',
            'stopped': '🛑',
            'paused': '⏸️'
        }
        
        emoji = status_emoji.get(printer.status, '❓')
        
        # Заголовок
        msg = f"\n"
        msg += f"{emoji} {printer.name}\n"
        msg += f"\n\n"
        
        # Модель та статус
        msg += f"📦 Модель: {printer.model}\n"
        msg += f"  📊 Статус:   {printer.status.upper()}\n"
        msg += f"  🌐 З'єднання:   {'🟢 Онлайн' if printer.online else '🔴 Офлайн'}\n\n"
        
        if printer.status.lower() == 'printing':
            # Прогрес з візуальним баром
            progress_bar = self._create_progress_bar(printer.progress, 15)
            msg += f"  📈 Прогрес друку  \n"
            msg += f"{progress_bar}\n"
            msg += f"{printer.progress}% завершено\n\n"
            
            # Інформація про файл
            if printer.current_file:
                msg += f"  📄 Файл:  \n{printer.current_file}\n\n"
            
            # Час
            if printer.remaining_time:
                rt = str(printer.remaining_time).lstrip('-').strip()
                eta = self._eta_from_remaining(printer.remaining_time)
                if eta:
                    msg += f"  ⏱️ Залишилось:  \n{rt}  (завершення о {self._format_eta(eta)})\n\n"
                else:
                    msg += f"  ⏱️ Залишилось:  \n{rt}\n\n"
            
            # Температури
            msg += f"  🌡️ Температури  \n"
            msg += f"├ Сопло: {printer.nozzle_temp or 'N/A'}\n"
            msg += f"└ Стіл: {printer.bed_temp or 'N/A'}\n\n"
            
            # Швидкість
            msg += f"  ⚡ Режим:   {printer.speed or 'Standard'}\n"
            
        elif printer.status.lower() in ['finished', 'stopped']:
            progress_bar = self._create_progress_bar(printer.progress, 15)
            msg += f"  📈 Прогрес  \n"
            msg += f"{progress_bar}\n"
            msg += f"{printer.progress}% завершено\n\n"
            
            if printer.current_file:
                msg += f"  📄 Файл:  \n{printer.current_file}\n\n"
        
        elif printer.status.lower() == 'paused':
            progress_bar = self._create_progress_bar(printer.progress, 15)
            msg += f"  📈 Прогрес  \n"
            msg += f"{progress_bar}\n"
            msg += f"{printer.progress}% завершено\n\n"
            
            if printer.current_file:
                msg += f"  📄 Файл:  \n{printer.current_file}\n\n"
            
            msg += f"  ⏸️ Друк призупинено  \n"
        
        elif printer.status.lower() == 'idle':
            msg += f"  ✨ Принтер готовий до роботи  \n"
        
        # Футер
        msg += f"\n━━━━━━━━\n"
        msg += f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        
        keyboard = [
            [InlineKeyboardButton("🔄 Оновити", callback_data=f"printer_{printer_index}")],
            [InlineKeyboardButton("◀️ До списку", callback_data="printers_refresh")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(msg, reply_markup=reply_markup)
    
    async def settings_users(self, query):
        """Налаштування користувачів та груп"""
        allowed = self.config.get('telegram', {}).get('allowed_users', [])
        allowed_groups = self.config.get('telegram', {}).get('allowed_groups', [])
        user_roles = self.config.get('telegram', {}).get('user_roles', {})
        
        # Перший користувач в списку - адмін
        admin_id = allowed[0] if allowed else None
        
        msg = "👥 **КОРИСТУВАЧІ ТА ГРУПИ**\n\n"
        
        msg += "**Користувачі:**\n"
        if allowed:
            for i, user_id in enumerate(allowed):
                role = "👑 Адмін" if i == 0 else "👤 Користувач"
                msg += f"{role}: `{user_id}`\n"
        else:
            msg += "⚠️ Список порожній (доступ для всіх)\n"
        
        msg += "\n**Групи:**\n"
        if allowed_groups:
            for group_id in allowed_groups:
                msg += f"💬 `{group_id}`\n"
        else:
            msg += "📭 Груп не додано\n"
        
        msg += "\n💡 **Як додати:**\n"
        msg += "• Користувач: відправте `/start` боту приватно\n"
        msg += "• Група: додайте бота в групу і відправте /start\n"
        msg += "• Вручну: відредагуйте config.json\n\n"
        msg += "**Права:**\n"
        msg += "👑 Адмін - повний доступ до налаштувань\n"
        msg += "👤 Користувач - перегляд статусу принтерів"
        
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="settings_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode='Markdown')
    
    def is_admin(self, user_id: int) -> bool:
        """Перевірка чи користувач є адміном"""
        allowed = self.config.get('telegram', {}).get('allowed_users', [])
        # Перший в списку - завжди адмін
        return len(allowed) > 0 and allowed[0] == user_id
    
    async def change_user_role(self, query, user_id_str: str):
        """Зміна ролі користувача (поки не реалізовано)"""
        await query.answer("Управління ролями доступне через config.json")
    
    # === ОБРОБНИКИ ПОВІДОМЛЕНЬ ===
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробка текстових повідомлень (кнопки клавіатури)"""
        is_reply = update.message.reply_to_message is not None if update.message else False
        logger.info(f"📨 Отримано повідомлення від {update.effective_user.id} в чаті {update.effective_chat.id} ({update.effective_chat.type}): {update.message.text if update.message else 'None'} | Reply: {is_reply}")
        
        if not await self.check_authorization(update, context):
            logger.warning(f"❌ Авторизація не пройдена для користувача {update.effective_user.id} в чаті {update.effective_chat.id}")
            return
        
        text = update.message.text
        logger.info(f"✅ Обробка команди: {text}")
        
        if text == "📊 Статус":
            await self.cmd_status(update, context)
        elif text == "📋 Список принтерів":
            await self.cmd_printers(update, context)
        elif text == "⚙️ Налаштування":
            await self.cmd_settings(update, context)
        elif text == "🔄 Перезапуск":
            await self.cmd_restart(update, context)

    # === ОБРОБНИК ПОМИЛОК ===

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Глобальний обробник помилок PTB — логуємо, не даємо боту впасти"""
        err = context.error
        if isinstance(err, (TimedOut, NetworkError, Conflict)):
            logger.warning(f"Тимчасова помилка Telegram: {type(err).__name__}: {err}")
        else:
            logger.error(
                f"Необроблена помилка при обробці апдейта: {type(err).__name__}: {err}",
                exc_info=err,
            )

    # === CALLBACK'И ВІД МОНІТОРА ===
    
    async def on_status_change(self, name: str, old: PrinterStatus, new: PrinterStatus):
        """Callback при зміні статусу"""
        if not self.config.get('notifications', {}).get('status_changes', True):
            return
        
        # Додаємо в буфер замість прямої відправки
        self.notification_buffer.append({
            'type': 'status_change',
            'name': name,
            'old_status': old.status,
            'new_status': new.status
        })
        await self._schedule_notification_batch()
    
    async def on_print_complete(self, name: str, printer: PrinterStatus):
        """Callback при завершенні друку"""
        if not self.config.get('notifications', {}).get('print_complete', True):
            return
        
        self.notification_buffer.append({
            'type': 'print_complete',
            'name': name,
            'file': printer.current_file
        })
        await self._schedule_notification_batch()
    
    async def on_printer_online(self, name: str, printer: PrinterStatus):
        """Callback коли принтер з'являється онлайн"""
        if not self.config.get('notifications', {}).get('printer_online', True):
            return
        
        self.notification_buffer.append({
            'type': 'printer_online',
            'name': name
        })
        await self._schedule_notification_batch()
    
    async def on_printer_offline(self, name: str, printer: PrinterStatus):
        """Callback коли принтер йде в офлайн"""
        if not self.config.get('notifications', {}).get('printer_offline', True):
            return
        
        self.notification_buffer.append({
            'type': 'printer_offline',
            'name': name
        })
        await self._schedule_notification_batch()
    
    def _generate_status_message(self) -> str:
        """Генерація повідомлення зі статусом принтерів"""
        if not self.monitor:
            return "❌ Моніторинг не запущений"
        
        summary = self.monitor.get_summary()
        printers = self.monitor.get_all_printers()
        
        # Заголовок
        msg = "📊 СТАТУС ФЕРМИ\n\n"

        # Загальна статистика
        msg += "📈 Загальна інформація\n"
        msg += f"├ 🖨️ Всього принтерів: <b>({summary['total']})</b>\n"
        msg += f"├ 🟢 Онлайн: <b>({summary['online']})</b>\n"
        msg += f"├ 🔴 Офлайн: <b>({summary['offline']})</b>\n"
        msg += f"├ ▶️ Активно друкують: <b>({summary['printing']})</b>\n"
        msg += f"├ ✅ Завершили друк: <b>({summary['finished']})</b>\n"
        msg += f"├ ⏸️ На паузі: <b>({summary.get('paused', 0)})</b>\n"
        msg += f"└ 💤 В очікуванні: <b>({summary['idle']})</b>\n\n"
        
        # Активні принтери (тільки друкують, БЕЗ паузи)
        active_printers = [p for p in printers if p.status.lower() == 'printing']
        if active_printers:
            msg += "<b>🔥 Активні завдання</b>\n"
            for i, printer in enumerate(active_printers):
                prefix = "└" if i == len(active_printers) - 1 else "├"
                progress_bar = self._create_progress_bar(printer.progress)
                
                # Іконка статусу
                status_icon = "▶️" if printer.status.lower() == 'printing' else "⏸️"
                
                # Назва принтера з файлом
                file_name = ""
                if printer.current_file:
                    # Обрізаємо назву файлу якщо вона занадто довга
                    max_len = 25
                    if len(printer.current_file) > max_len:
                        file_name = f" - {html.escape(printer.current_file[:max_len-7])}..."
                    else:
                        file_name = f" - {html.escape(printer.current_file)}"

                msg += f"{prefix} {status_icon} <b>{html.escape(printer.name)}</b>{file_name}\n"
                msg += f"  {progress_bar} <b>{printer.progress}%</b>\n"
                if printer.remaining_time and printer.status.lower() == 'printing':
                    rt = html.escape(str(printer.remaining_time).lstrip('-').strip())
                    eta = self._eta_from_remaining(printer.remaining_time)
                    if eta:
                        msg += f"  ⏱ {rt} → <b>{self._format_eta(eta)}</b>\n"
                    else:
                        msg += f"  ⏱ {rt}\n"
            msg += "\n"
        
        # Завершені принтери
        finished = [p for p in printers if p.status.lower() == 'finished']
        if finished:
            msg += "<b>✅ Завершено</b>\n"
            for printer in finished:
                msg += f"└ {html.escape(printer.name)} • <code>{html.escape(printer.current_file or 'N/A')}</code>\n"
            msg += "\n"
        
        # Призупинені принтери
        paused = [p for p in printers if p.status.lower() == 'paused']
        if paused:
            msg += "⏸️ На паузі\n"
            for printer in paused:
                if printer.current_file and len(printer.current_file) > 20:
                    file_display = html.escape(printer.current_file[:20]) + "..."
                else:
                    file_display = html.escape(printer.current_file or 'N/A')
                msg += f"└ {html.escape(printer.name)} • <b>{printer.progress}%</b> • {file_display}\n"
            msg += "\n"
        
        # Зупинені принтери
        stopped = [p for p in printers if p.status.lower() == 'stopped']
        if stopped:
            msg += "<b>🛑 Зупинено</b>\n"
            for printer in stopped:
                msg += f"└ {html.escape(printer.name)} • <b>{printer.progress}%</b>\n"
            msg += "\n"
        
        # Футер
        msg += "━━━━━━━━━━━\n"
        msg += f"🕐 Оновлено: <b>{datetime.now().strftime('%H:%M:%S')}</b>"
        return msg
    
    def _create_progress_bar(self, progress: int, length: int = 10) -> str:
        """Створення візуального прогрес-бару"""
        filled = int(length * progress / 100)
        bar = "█" * filled + "░" * (length - filled)
        return f"[{bar}]"

    @staticmethod
    def _eta_from_remaining(remaining: Optional[str]) -> Optional[datetime]:
        """'-7h11m' / '45m' / '1h' → час, коли друк завершиться (None, якщо не розпарсити)"""
        if not remaining:
            return None
        d = re.search(r'(\d+)\s*d', remaining)
        h = re.search(r'(\d+)\s*h', remaining)
        m = re.search(r'(\d+)\s*m', remaining)
        days = int(d.group(1)) if d else 0
        hours = int(h.group(1)) if h else 0
        minutes = int(m.group(1)) if m else 0
        if days == 0 and hours == 0 and minutes == 0:
            return None
        return datetime.now() + timedelta(days=days, hours=hours, minutes=minutes)

    @staticmethod
    def _format_eta(eta: datetime) -> str:
        """Час завершення: тільки годину, якщо сьогодні; інакше з датою"""
        if eta.date() == datetime.now().date():
            return eta.strftime('%H:%M')
        return eta.strftime('%d.%m %H:%M')
    
    def _schedule_callback(self, coro):
        """Безпечне планування async callback з sync потоку"""
        if self.event_loop and self.event_loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self.event_loop)
        else:
            logger.error("❌ Event loop не доступний для callback")
    
    async def _schedule_notification_batch(self):
        """Планування відправки батчу сповіщень з затримкою"""
        # Якщо таймер вже запущений - скасовуємо його
        if self.notification_timer and not self.notification_timer.done():
            self.notification_timer.cancel()
        
        # Запускаємо новий таймер
        self.notification_timer = asyncio.create_task(self._send_notification_batch())
    
    async def _send_notification_batch(self):
        """Відправка батчу накопичених сповіщень"""
        # Чекаємо затримку для накопичення подій
        await asyncio.sleep(self.notification_batch_delay)
        
        if not self.notification_buffer:
            return
        
        # Групуємо сповіщення за типами
        status_changes = [n for n in self.notification_buffer if n['type'] == 'status_change']
        print_completes = [n for n in self.notification_buffer if n['type'] == 'print_complete']
        printers_online = [n for n in self.notification_buffer if n['type'] == 'printer_online']
        printers_offline = [n for n in self.notification_buffer if n['type'] == 'printer_offline']
        
        # Формуємо об'єднане повідомлення (HTML; динамічний текст екрануємо)
        def e(value) -> str:
            return html.escape(str(value)) if value is not None else "N/A"

        msg_parts = []

        if print_completes:
            if len(print_completes) == 1:
                n = print_completes[0]
                msg_parts.append(
                    f"✅ <b>ДРУК ЗАВЕРШЕНО</b>\n\n"
                    f"Принтер: {e(n['name'])}\n"
                    f"Файл: {e(n['file'])}\n"
                    f"Час: {datetime.now().strftime('%H:%M:%S')}"
                )
            else:
                msg = f"✅ <b>ДРУК ЗАВЕРШЕНО</b> (×{len(print_completes)})\n\n"
                for n in print_completes:
                    msg += f"• {e(n['name'])} — <code>{e(n['file'])}</code>\n"
                msg += f"\nЧас: {datetime.now().strftime('%H:%M:%S')}"
                msg_parts.append(msg)

        if status_changes:
            if len(status_changes) == 1:
                n = status_changes[0]
                name = e(n['name'])
                # Генеруємо красиве повідомлення в залежності від нового статусу
                status_messages = {
                    'printing': f"▶️ <b>ДРУК РОЗПОЧАТО</b>\n\n🖨️ Принтер <b>{name}</b> розпочав друк",
                    'finished': f"✅ <b>ДРУК ЗАВЕРШЕНО</b>\n\n🎉 Принтер <b>{name}</b> завершив роботу!",
                    'stopped': f"🛑 <b>ДРУК ЗУПИНЕНО</b>\n\n⚠️ Принтер <b>{name}</b> призупинив роботу",
                    'paused': f"⏸️ <b>ДРУК НА ПАУЗІ</b>\n\n⏸️ Принтер <b>{name}</b> призупинив друк",
                    'idle': f"⏸️ <b>ПРИНТЕР ОЧІКУЄ</b>\n\n💤 Принтер <b>{name}</b> готовий до роботи",
                    'offline': f"🔴 <b>ПРИНТЕР ОФЛАЙН</b>\n\n📡 Принтер <b>{name}</b> відключився"
                }
                msg = status_messages.get(n['new_status'],
                    f"🔄 <b>ЗМІНА СТАТУСУ</b>\n\n"
                    f"🖨️ Принтер <b>{name}</b>\n"
                    f"└ <code>{e(n['old_status'])}</code> → <code>{e(n['new_status'])}</code>"
                )
                msg_parts.append(msg)
            else:
                # Групуємо за новим статусом для множинних змін
                by_status = {}
                for n in status_changes:
                    status = n['new_status']
                    if status not in by_status:
                        by_status[status] = []
                    by_status[status].append(n['name'])

                status_headers = {
                    'printing': '▶️ <b>ДРУК РОЗПОЧАТО</b>',
                    'finished': '✅ <b>ДРУК ЗАВЕРШЕНО</b>',
                    'stopped': '🛑 <b>ДРУК ЗУПИНЕНО</b>',
                    'paused': '⏸️ <b>НА ПАУЗІ</b>',
                    'idle': '⏸️ <b>ОЧІКУЮТЬ</b>',
                    'offline': '🔴 <b>ОФЛАЙН</b>'
                }

                parts = []
                for status, names in by_status.items():
                    header = status_headers.get(status, f'🔄 <b>{e(status.upper())}</b>')
                    parts.append(f"{header} (×{len(names)})\n" + "\n".join([f"• {e(name)}" for name in names]))

                msg_parts.append("\n\n━━━━━━━━━━━\n\n".join(parts))

        if printers_online:
            if len(printers_online) == 1:
                msg_parts.append(f"🟢 <b>ПРИНТЕР ОНЛАЙН</b>\n\nПринтер {e(printers_online[0]['name'])} підключився")
            else:
                msg = f"🟢 <b>ПРИНТЕРИ ОНЛАЙН</b> (×{len(printers_online)})\n\n"
                msg += "\n".join([f"• {e(n['name'])}" for n in printers_online])
                msg_parts.append(msg)

        if printers_offline:
            if len(printers_offline) == 1:
                msg_parts.append(f"🔴 <b>ПРИНТЕР ОФЛАЙН</b>\n\nПринтер {e(printers_offline[0]['name'])} відключився")
            else:
                msg = f"🔴 <b>ПРИНТЕРИ ОФЛАЙН</b> (×{len(printers_offline)})\n\n"
                msg += "\n".join([f"• {e(n['name'])}" for n in printers_offline])
                msg_parts.append(msg)

        # Відправляємо об'єднане повідомлення
        if msg_parts:
            combined_msg = "\n\n━━━━━━━━━━━━━━━━━━━━━\n\n".join(msg_parts)
            await self.send_notification(combined_msg)
        
        # Очищуємо буфер
        self.notification_buffer.clear()
    
    async def update_status_messages(self):
        """Оновлення всіх активних статусних повідомлень"""
        if not self.application or not self.monitor:
            return
        
        if not self.active_status_messages:
            return  # Немає активних статусних повідомлень
        
        msg = self._generate_status_message()
        
        # Список чатів для видалення (якщо оновлення не вдалося)
        chats_to_remove = []
        updated_count = 0
        
        for chat_id, message_id in list(self.active_status_messages.items()):
            try:
                await self.application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=msg,
                    parse_mode='HTML'
                )
                updated_count += 1
            except Exception as e:
                # Якщо повідомлення видалене або недоступне - видаляємо з списку
                if "message to edit not found" in str(e).lower() or "message can't be edited" in str(e).lower():
                    chats_to_remove.append(chat_id)
                    logger.debug(f"Видалено недоступне статусне повідомлення для чату {chat_id}")
                elif "message is not modified" not in str(e).lower():
                    # Ігноруємо помилку "message is not modified"
                    logger.debug(f"Помилка оновлення статусу для чату {chat_id}: {e}")
        
        # Видаляємо недоступні чати
        for chat_id in chats_to_remove:
            del self.active_status_messages[chat_id]
        
        if updated_count > 0:
            logger.info(f"🔄 Оновлено {updated_count} статусних повідомлень")
    
    async def send_notification(self, message: str):
        """Відправка сповіщення всім дозволеним користувачам та групам"""
        if not self.application:
            return
        
        # Відправка користувачам
        allowed_users = self.config.get('telegram', {}).get('allowed_users', [])
        for user_id in allowed_users:
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Помилка відправки повідомлення користувачу {user_id}: {e}")

        # Відправка в групи
        allowed_groups = self.config.get('telegram', {}).get('allowed_groups', [])
        for group_id in allowed_groups:
            try:
                await self.application.bot.send_message(
                    chat_id=group_id,
                    text=message,
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Помилка відправки повідомлення в групу {group_id}: {e}")
    
    # === ЗАПУСК ТА ЗУПИНКА ===
    
    async def start_monitor(self) -> bool:
        """Запуск моніторингу принтерів"""
        try:
            monitor_config = self.config.get('monitor', {})
            
            self.monitor = BambuPrinterMonitor(
                debug_port=monitor_config.get('debug_port', 9222),
                update_interval=monitor_config.get('update_interval', 30),
                exe_path=monitor_config.get('exe_path', BAMBU_EXE_PATH),
                auto_launch=monitor_config.get('auto_launch', True),
                debug_logging=monitor_config.get('debug_logging', False),
                debug_dump_dir=monitor_config.get('debug_dump_dir', 'debug_dumps')
            )
            
            # Встановлення callback'ів
            # Використовуємо run_coroutine_threadsafe для виклику async функцій з sync потоку
            self.monitor.on_printer_status_change = lambda n, o, p: self._schedule_callback(self.on_status_change(n, o, p))
            self.monitor.on_print_complete = lambda n, p: self._schedule_callback(self.on_print_complete(n, p))
            self.monitor.on_printer_online = lambda n, p: self._schedule_callback(self.on_printer_online(n, p))
            self.monitor.on_printer_offline = lambda n, p: self._schedule_callback(self.on_printer_offline(n, p))
            # Callback для оновлення статусних повідомлень після кожного update
            self.monitor.on_update_complete = lambda: self._schedule_callback(self.update_status_messages())
            
            # Запуск у окремому потоці
            success = await asyncio.get_event_loop().run_in_executor(
                None, self.monitor.start
            )
            
            if success:
                logger.info("✅ Моніторинг принтерів запущено")
                return True
            else:
                logger.error("❌ Не вдалося запустити моніторинг")
                return False
                
        except Exception as e:
            logger.error(f"❌ Помилка запуску моніторингу: {e}")
            return False
    
    async def periodic_status_update(self):
        """Періодичне відправлення статусу"""
        while True:
            try:
                notif = self.config.get('notifications', {})
                if notif.get('periodic_updates', False):
                    interval = notif.get('periodic_interval', 3600)
                    await asyncio.sleep(interval)
                    
                    if self.monitor and self.monitor.running:
                        summary = self.monitor.get_summary()
                        msg = (
                            f"📊 <b>ПЕРІОДИЧНЕ ОНОВЛЕННЯ</b>\n\n"
                            f"🖨️ Всього: {summary['total']}\n"
                            f"🟢 Онлайн: {summary['online']}\n"
                            f"▶️ Друкують: {summary['printing']}\n"
                            f"✅ Завершили друк: {summary['finished']}\n"
                            f"⏸️ На паузі: {summary.get('paused', 0)}\n"
                            f"💤 Очікують: {summary['idle']}\n"
                            f"🔴 Офлайн: {summary['offline']}"
                        )
                        await self.send_notification(msg)
                else:
                    await asyncio.sleep(60)  # Перевіряємо кожну хвилину чи увімкнено
            except Exception as e:
                logger.error(f"Помилка періодичного оновлення: {e}")
                await asyncio.sleep(60)
    
    def build_application(self) -> Application:
        """Створення Telegram Application з proxy якщо налаштовано"""
        proxy_config = self.config.get('telegram', {}).get('proxy', {})
        
        # Налаштування proxy
        if proxy_config.get('enabled', False):
            proxy_url = f"{proxy_config.get('type', 'http')}://"
            
            username = proxy_config.get('username', '')
            password = proxy_config.get('password', '')
            if username and password:
                proxy_url += f"{username}:{password}@"
            
            proxy_url += f"{proxy_config.get('host', '127.0.0.1')}:{proxy_config.get('port', 8080)}"
            
            logger.info(f"🌐 Використовуємо proxy: {proxy_url}")
            logger.info("⚠️ УВАГА: Переконайтеся що SOCKS5 proxy працює стабільно!")
            
            # Використовуємо простий підхід - створюємо request з proxy URL
            # HTTPXRequest автоматично створить правильний клієнт
            request = HTTPXRequest(
                proxy=proxy_url,
                connection_pool_size=8,
                connect_timeout=30.0,
                read_timeout=30.0,
                write_timeout=30.0,
                pool_timeout=30.0
            )
            
            app = Application.builder().token(
                self.config['telegram']['bot_token']
            ).request(request).build()
        else:
            app = Application.builder().token(
                self.config['telegram']['bot_token']
            ).build()
        
        return app
    
    async def start(self):
        """Запуск бота"""
        try:
            # Зберігаємо event loop для використання в callback'ах
            self.event_loop = asyncio.get_running_loop()
            
            # Перевірка токена
            if not appconfig.token_is_set(self.config):
                logger.error(
                    "❌ Токен бота не налаштовано. Впишіть telegram.bot_token у %s "
                    "(або через GUI-панель) і запустіть знову.", self.config_path)
                return
            
            # Створення Application з proxy підтримкою
            self.application = self.build_application()
            
            # Реєстрація handlers
            logger.info("📝 Реєстрація handlers...")
            self.application.add_handler(CommandHandler("start", self.cmd_start))
            self.application.add_handler(CommandHandler("status", self.cmd_status))
            self.application.add_handler(CommandHandler("printers", self.cmd_printers))
            self.application.add_handler(CommandHandler("settings", self.cmd_settings))
            self.application.add_handler(CommandHandler("restart", self.cmd_restart))
            self.application.add_handler(CommandHandler("help", self.cmd_help))
            self.application.add_handler(CommandHandler("id", self.cmd_id))
            self.application.add_handler(CommandHandler("version", self.cmd_version))
            self.application.add_handler(CommandHandler("debug", self.cmd_debug))
            self.application.add_handler(CommandHandler("debuglog", self.cmd_debuglog))
            logger.info("   ✓ Зареєстровано 10 команд")

            # Один головний обробник для всіх callback'ів
            self.application.add_handler(CallbackQueryHandler(self.callback_handler))
            logger.info("   ✓ CallbackQueryHandler зареєстровано")

            # MessageHandler для всіх текстових повідомлень (включно з групами)
            self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
            logger.info("   ✓ MessageHandler зареєстровано (обробка всіх текстових повідомлень)")
            logger.info("   ⚠️ УВАГА: В групах потрібно вимкнути Privacy Mode або зробити бота адміном!")

            # Глобальний обробник помилок
            self.application.add_error_handler(self.error_handler)
            logger.info("   ✓ Error handler зареєстровано")
            
            # Запуск моніторингу
            logger.info("🚀 Запуск моніторингу принтерів...")
            monitor_success = await self.start_monitor()
            if monitor_success:
                logger.info(f"   ✓ Моніторинг запущено (інтервал: {self.config.get('monitor', {}).get('update_interval', 30)}с)")
            else:
                logger.error("   ✗ Помилка запуску моніторингу")
            
            # Запуск періодичних оновлень
            asyncio.create_task(self.periodic_status_update())
            
            # Запуск бота
            logger.info("🤖 Запуск Telegram бота...")
            await self.application.initialize()
            await self.application.start()
            
            # Виводимо статистику
            allowed_users = self.config.get('telegram', {}).get('allowed_users', [])
            allowed_groups = self.config.get('telegram', {}).get('allowed_groups', [])
            logger.info(f"   ✓ Дозволено користувачів: {len(allowed_users)}")
            logger.info(f"   ✓ Дозволено груп: {len(allowed_groups)}")
            
            # Запуск updater з правильними параметрами
            await self.application.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True
            )
            
            logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            logger.info(f"✅ farmwatch v{__version__} ЗАПУЩЕНО!")
            logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

            # Сповіщення про запуск
            await self.send_notification(f"🚀 farmwatch v{__version__} запущено!")
            
            # Тримаємо бота запущеним
            while True:
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"❌ Помилка запуску бота: {e}")
            raise
        finally:
            if self.application and self.application.updater and self.application.updater.running:
                await self.stop()
    
    async def stop(self):
        """Зупинка бота"""
        logger.info("⏹️ Зупинка бота...")
        
        # Зупинка моніторингу
        if self.monitor:
            await asyncio.get_event_loop().run_in_executor(
                None, self.monitor.stop
            )
        
        # Зупинка бота
        if self.application:
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
        
        logger.info("✅ Бот зупинено")


# === ГОЛОВНА ФУНКЦІЯ ===

# Тримаємо сокет відкритим увесь час життя процесу — він і є "замком"
_single_instance_sock: Optional[socket.socket] = None


def acquire_single_instance_lock(port: int = 48217) -> bool:
    """Перевірка, що запущений лише один екземпляр бота.

    Telegram дозволяє тільки один активний getUpdates на токен — другий екземпляр
    отримує Conflict у циклі. Тримаємо TCP-порт на 127.0.0.1: якщо bind не вдався —
    значить бот уже працює. Сокет звільняється автоматично, коли процес завершується.
    """
    global _single_instance_sock
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        return False
    _single_instance_sock = sock
    return True


async def main():
    """Головна функція"""
    if not acquire_single_instance_lock():
        logger.error(
            "❌ Бот уже запущений (інший екземпляр тримає блокування на 127.0.0.1:48217). "
            "Закрийте попередній процес перед повторним запуском."
        )
        return

    bot = BambuTelegramBot()

    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("\n⏹️ Отримано сигнал зупинки...")
    except Exception as e:
        logger.error(f"❌ Критична помилка: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
