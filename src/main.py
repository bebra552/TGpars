import sys
import os
import asyncio
import logging
import csv
from datetime import datetime
from pathlib import Path
from io import StringIO
from typing import List, Dict, Any

# GUI
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
    QWidget, QPushButton, QLineEdit, QTextEdit, QLabel,
    QProgressBar, QFileDialog, QGroupBox, QFormLayout,
    QMessageBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QInputDialog, QComboBox
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt

# Telethon
from telethon import TelegramClient, errors, functions, types
from telethon.tl.types import (
    UserStatusOnline, UserStatusOffline, UserStatusRecently,
    UserStatusLastWeek, UserStatusLastMonth, UserStatusEmpty
)

import re

# ----------------------------------------------------------------------------
# Helpers for Telethon status mapping
# ----------------------------------------------------------------------------

def get_user_status_text(status_obj: types.TypeUserStatus | None) -> str:
    """Return human-readable user status."""
    if status_obj is None:
        return "Скрыто"
    if isinstance(status_obj, UserStatusOnline):
        return "Онлайн"
    if isinstance(status_obj, UserStatusOffline):
        return "Оффлайн"
    if isinstance(status_obj, UserStatusRecently):
        return "Недавно"
    if isinstance(status_obj, UserStatusLastWeek):
        return "Был на этой неделе"
    if isinstance(status_obj, UserStatusLastMonth):
        return "Был в этом месяце"
    if isinstance(status_obj, UserStatusEmpty):
        return "Скрыто"
    # Для неизвестных статусов (старых, удалённых) выводим «Давно»
    return "Давно"


# ----------------------------------------------------------------------------
# Base Thread using Telethon
# ----------------------------------------------------------------------------

class TelegramParserThread(QThread):
    """Thread for various Telegram data collection tasks (Telethon)."""

    progress_signal = pyqtSignal(str)
    progress_value = pyqtSignal(int)
    finished_signal = pyqtSignal(str, list)
    error_signal = pyqtSignal(str)
    auth_code_needed = pyqtSignal(str)
    auth_password_needed = pyqtSignal()

    def __init__(self, api_id: str, api_hash: str, link: str,
                 limit: int = 1000, session_name: str | None = None):
        super().__init__()
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.link = link
        self.limit = limit
        self.session_name = session_name or "telegram_parser_session"
        self.client: TelegramClient | None = None
        # auth flow
        self.auth_code: str | None = None
        self.auth_password: str | None = None
        self.is_running = True

    # ---------------------------------------------------------------------
    # Telethon helpers
    # ---------------------------------------------------------------------

    async def ensure_client(self):
        """Creates (if needed) and connects client."""
        if self.client is None:
            self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)
        if not self.client.is_connected():
            await self.client.connect()

    async def ensure_auth(self) -> bool:
        """Interactive authorization similar to original logic."""
        await self.ensure_client()
        if await self.client.is_user_authorized():
            me = await self.client.get_me()
            self.progress_signal.emit(f"✅ Авторизован как: {me.first_name}")
            return True

        # Need phone
        self.auth_code_needed.emit("Введите номер телефона (например: +1234567890)")
        while self.auth_code is None and self.is_running:
            await asyncio.sleep(0.1)
        if not self.is_running:
            return False
        phone = self.auth_code.strip()
        self.auth_code = None

        self.progress_signal.emit(f"📤 Отправляем код на {phone}…")
        try:
            await self.client.send_code_request(phone)
        except errors.FloodWaitError as e:
            self.progress_signal.emit(f"⏳ FloodWait: {e.seconds} сек")
            await asyncio.sleep(e.seconds)
            return await self.ensure_auth()
        except Exception as e:
            self.error_signal.emit(f"❌ Не удалось отправить код: {str(e)}")
            return False

        # ask for code
        self.auth_code_needed.emit(f"Введите код из SMS/Telegram для {phone}")
        while self.auth_code is None and self.is_running:
            await asyncio.sleep(0.1)
        if not self.is_running:
            return False
        code = self.auth_code.strip()
        self.auth_code = None

        try:
            await self.client.sign_in(phone=phone, code=code)
            self.progress_signal.emit("✅ Авторизация успешна")
            return True
        except errors.SessionPasswordNeededError:
            # Need 2FA password
            self.progress_signal.emit("🔐 Требуется пароль 2FA…")
            self.auth_password_needed.emit()
            while self.auth_password is None and self.is_running:
                await asyncio.sleep(0.1)
            if not self.is_running:
                return False
            try:
                await self.client.sign_in(password=self.auth_password)
                self.progress_signal.emit("✅ Авторизация с 2FA успешна")
                return True
            except Exception as pwd_error:
                self.error_signal.emit(f"❌ Неверный пароль 2FA: {pwd_error}")
                return False
        except Exception as sign_err:
            self.error_signal.emit(f"❌ Ошибка авторизации: {sign_err}")
            return False

    # ------------------------------------------------------------------
    # Cleanup & control
    # ------------------------------------------------------------------

    async def cleanup(self):
        if self.client and self.client.is_connected():
            await self.client.disconnect()

    def stop(self):
        self.is_running = False


# ----------------------------------------------------------------------------
# Participants parser thread
# ----------------------------------------------------------------------------

class MembersParserThread(TelegramParserThread):
    def __init__(self, api_id, api_hash, chat_link, limit, session_name=None):
        super().__init__(api_id, api_hash, chat_link, limit, session_name)

    async def parse(self):
        old_stdin = sys.stdin
        try:
            sys.stdin = StringIO("")
            if not self.is_running:
                return

            self.progress_signal.emit("🔄 Инициализация клиента…")
            if not await self.ensure_auth():
                return

            if not self.is_running:
                return

            chat_username = self._clean_link(self.link)
            self.progress_signal.emit(f"🔍 Поиск группы: @{chat_username}")
            try:
                entity = await self.client.get_entity(chat_username)
            except Exception as e:
                self.error_signal.emit(f"❌ Не удалось найти группу: {e}")
                return

            full_chat = await self.client(functions.channels.GetFullChannelRequest(channel=entity)) if isinstance(entity, types.Channel) else None
            members_count = full_chat.full_chat.participants_count if full_chat else 'Неизвестно'
            self.progress_signal.emit(f"📊 Группа: {getattr(entity, 'title', '')}")
            self.progress_signal.emit(f"👥 Участников: {members_count}")

            # Получаем список администраторов, чтобы быстро определять Is Admin
            admin_ids: set[int] = set()
            try:
                async for adm in self.client.iter_participants(entity, filter=types.ChannelParticipantsAdmins, aggressive=True):
                    admin_ids.add(adm.id)
            except Exception:
                pass  # Если не удалось – оставим список пустым

            # iterate participants
            members: List[types.User] = []
            async for user in self.client.iter_participants(entity, limit=self.limit, aggressive=True):
                if not self.is_running:
                    break
                members.append(user)
                if len(members) % 50 == 0:
                    self.progress_signal.emit(f"📥 Получено участников: {len(members)}")
                    self.progress_value.emit(min(len(members), self.limit))

            parsed_data: List[Dict[str, Any]] = []
            for idx, user in enumerate(members):
                if not self.is_running:
                    break
                # Формируем last_online
                last_online_str = ''
                if isinstance(user.status, UserStatusOffline):
                    last_online_str = user.status.was_online.strftime("%Y-%m-%d %H:%M:%S")

                parsed_data.append({
                    'ID': user.id,
                    'Username': user.username or '',
                    'First Name': user.first_name or '',
                    'Last Name': user.last_name or '',
                    'Phone': user.phone or '',
                    'Status': get_user_status_text(user.status),
                    'Last Online': last_online_str or 'Скрыто',
                    'Is Bot': 'Да' if user.bot else 'Нет',
                    'Is Verified': 'Да' if user.verified else 'Нет',
                    'Is Scam': 'Да' if user.scam else 'Нет',
                    'Is Premium': 'Да' if user.premium else 'Нет',
                    'Is Admin': 'Да' if user.id in admin_ids else 'Нет',
                })
                if (idx + 1) % 50 == 0:
                    self.progress_signal.emit(f"🔄 Обработано: {idx + 1}/{len(members)}")
                    self.progress_value.emit(idx + 1)

            if self.is_running:
                self.finished_signal.emit(getattr(entity, 'title', ''), parsed_data)

        except Exception as e:
            if self.is_running:
                self.error_signal.emit(f"❌ Критическая ошибка: {e}")
        finally:
            sys.stdin = old_stdin
            await self.cleanup()

    def _clean_link(self, link: str) -> str:
        link = link.strip()
        link = link.replace("https://t.me/", "").replace("t.me/", "")
        if link.startswith("@"):
            link = link[1:]
        if "/" in link:
            link = link.split("/")[0]
        if "?" in link:
            link = link.split("?")[0]
        return link

    def run(self):
        asyncio.run(self.parse())


# ----------------------------------------------------------------------------
# Messages parser thread (recent chat history)
# ----------------------------------------------------------------------------

class MessagesParserThread(TelegramParserThread):
    def __init__(self, api_id, api_hash, chat_link, limit, session_name=None):
        super().__init__(api_id, api_hash, chat_link, limit, session_name)

    async def parse(self):
        old_stdin = sys.stdin
        try:
            sys.stdin = StringIO("")
            if not self.is_running:
                return
            self.progress_signal.emit("🔄 Инициализация клиента…")
            if not await self.ensure_auth():
                return
            chat_username = MembersParserThread._clean_link(self, self.link)
            entity = await self.client.get_entity(chat_username)
            self.progress_signal.emit(f"💬 Чат: {getattr(entity, 'title', chat_username)}")
            self.progress_signal.emit("📥 Получаю сообщения…")

            messages: List[types.Message] = []
            async for msg in self.client.iter_messages(entity, limit=self.limit):
                if not self.is_running:
                    break
                messages.append(msg)
                if len(messages) % 50 == 0:
                    self.progress_signal.emit(f"🔄 Получено сообщений: {len(messages)}")
                    self.progress_value.emit(len(messages))

            parsed: List[Dict[str, Any]] = []
            for m in messages:
                sender = await m.get_sender()
                parsed.append({
                    'Message ID': m.id,
                    'Author ID': sender.id if sender else '',
                    'Username': sender.username if sender else '',
                    'First Name': sender.first_name if sender else '',
                    'Last Name': sender.last_name if sender else '',
                    'Date': m.date.strftime("%Y-%m-%d %H:%M:%S"),
                    'Text': (m.text or m.message or '')[:4096],
                    'Media Type': type(m.media).__name__ if m.media else ''
                })
            if self.is_running:
                self.finished_signal.emit(f"Сообщения чата {getattr(entity, 'title', chat_username)}", parsed)
        except Exception as e:
            if self.is_running:
                self.error_signal.emit(f"❌ Критическая ошибка: {e}")
        finally:
            sys.stdin = old_stdin
            await self.cleanup()

    def run(self):
        asyncio.run(self.parse())


# ----------------------------------------------------------------------------
# Comments parser thread (replies to post)
# ----------------------------------------------------------------------------

class CommentsParserThread(TelegramParserThread):
    def __init__(self, api_id, api_hash, post_link, limit, session_name=None):
        super().__init__(api_id, api_hash, post_link, limit, session_name)

    async def parse(self):
        old_stdin = sys.stdin
        try:
            sys.stdin = StringIO("")
            if not self.is_running:
                return
            self.progress_signal.emit("🔄 Инициализация клиента…")
            if not await self.ensure_auth():
                return
            link = self.link.strip().replace("https://t.me/", "").replace("t.me/", "")
            parts = link.split("/")
            if len(parts) < 2:
                self.error_signal.emit("❌ Некорректная ссылка на пост")
                return
            channel_part, msg_id_str = parts[0], parts[1].split("?")[0]
            msg_id = int(msg_id_str)
            entity = await self.client.get_entity(channel_part)

            self.progress_signal.emit(f"📄 Канал: {getattr(entity, 'title', channel_part)} | Пост #{msg_id}")
            self.progress_signal.emit("💬 Получаю комментарии…")
            comments: List[types.Message] = []

            async for reply in self.client.iter_messages(entity, limit=self.limit, reply_to=msg_id):
                if not self.is_running:
                    break
                comments.append(reply)
                if len(comments) % 50 == 0:
                    self.progress_signal.emit(f"🔄 Получено комментариев: {len(comments)}")
                    self.progress_value.emit(min(len(comments), self.limit))

            parsed: List[Dict[str, Any]] = []
            for reply in comments:
                sender = await reply.get_sender()
                parsed.append({
                    'Comment ID': reply.id,
                    'Author ID': sender.id if sender else '',
                    'Username': sender.username if sender else '',
                    'First Name': sender.first_name if sender else '',
                    'Last Name': sender.last_name if sender else '',
                    'Text': reply.text or reply.message or '',
                    'Date': reply.date.strftime("%Y-%m-%d %H:%M:%S")
                })
            if self.is_running:
                self.finished_signal.emit(f"Комментарии к посту #{msg_id}", parsed)
        except Exception as e:
            if self.is_running:
                self.error_signal.emit(f"❌ Критическая ошибка: {e}")
        finally:
            sys.stdin = old_stdin
            await self.cleanup()

    def run(self):
        asyncio.run(self.parse())


# ----------------------------------------------------------------------------
# Reaction parser thread (simplified)
# ----------------------------------------------------------------------------

class ReactionsParserThread(TelegramParserThread):
    def __init__(self, api_id, api_hash, post_link, limit, session_name=None):
        super().__init__(api_id, api_hash, post_link, limit, session_name)

    async def parse(self):
        old_stdin = sys.stdin
        try:
            sys.stdin = StringIO("")
            if not self.is_running:
                return
            if not await self.ensure_auth():
                return
            link = self.link.strip().replace("https://t.me/", "").replace("t.me/", "")
            parts = link.split("/")
            if len(parts) < 2:
                self.error_signal.emit("❌ Неверная ссылка на пост")
                return
            channel_part, msg_id_str = parts[0], parts[1].split("?")[0]
            msg_id = int(msg_id_str)
            entity = await self.client.get_entity(channel_part)
            self.progress_signal.emit(f"📄 Канал/чат: {getattr(entity, 'title', channel_part)} | Пост #{msg_id}")

            # fetch reactions list
            from telethon.tl.functions.messages import GetMessageReactionsListRequest
            try:
                response = await self.client(GetMessageReactionsListRequest(
                    peer=entity,
                    id=msg_id,
                    limit=self.limit,
                    offset=0,
                    reaction=None
                ))
            except Exception as e:
                self.error_signal.emit(f"ℹ️ Ошибка получения реакций: {e}")
                return

            parsed: List[Dict[str, Any]] = []
            for user in response.users:
                reaction = next((r.reaction.emoticon for r in response.reactions if r.peer_id.user_id == user.id), '🧩')
                parsed.append({
                    'Emoji': reaction,
                    'User ID': user.id,
                    'Username': user.username or '',
                    'First Name': user.first_name or '',
                    'Last Name': user.last_name or ''
                })
                if len(parsed) >= self.limit:
                    break
            if self.is_running:
                self.finished_signal.emit(f"Реакции поста #{msg_id}", parsed)
        except Exception as e:
            if self.is_running:
                self.error_signal.emit(f"❌ Критическая ошибка: {e}")
        finally:
            sys.stdin = old_stdin
            await self.cleanup()

    def run(self):
        asyncio.run(self.parse())


# ----------------------------------------------------------------------------
# GUI class – mostly unchanged except threads mapping
# ----------------------------------------------------------------------------

class TelegramParserGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.parser_thread: TelegramParserThread | None = None
        self.parsed_data: List[Dict[str, Any]] = []
        self.session_name = "telegram_parser_persistent"
        self.init_ui()
        self.setup_logging()

    # --- UI creation methods copied from original, minimal changes -------------

    def init_ui(self):
        self.setWindowTitle("Telegram Group Parser v2.1 (Telethon)")
        self.setGeometry(100, 100, 1200, 800)
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        self.setup_settings_tab()
        self.setup_parser_tab()
        self.setup_results_tab()

    def setup_settings_tab(self):
        """Настройки API"""
        settings_widget = QWidget()
        self.tabs.addTab(settings_widget, "⚙️ Настройки")

        layout = QVBoxLayout(settings_widget)

        # Группа API настроек
        api_group = QGroupBox("🔑 Telegram API")
        api_layout = QFormLayout(api_group)

        self.api_id_input = QLineEdit()
        self.api_id_input.setPlaceholderText("Введите API ID")
        api_layout.addRow("API ID:", self.api_id_input)

        self.api_hash_input = QLineEdit()
        self.api_hash_input.setPlaceholderText("Введите API Hash")
        api_layout.addRow("API Hash:", self.api_hash_input)

        layout.addWidget(api_group)

        # Группа настроек парсинга
        parse_group = QGroupBox("📊 Настройки парсинга")
        parse_layout = QFormLayout(parse_group)

        self.max_members_input = QLineEdit("1000")
        parse_layout.addRow("Макс. элементов:", self.max_members_input)

        self.save_path_input = QLineEdit(str(Path.home()))
        parse_layout.addRow("Папка сохранения:", self.save_path_input)

        browse_btn = QPushButton("📁 Обзор")
        browse_btn.clicked.connect(self.browse_save_path)
        parse_layout.addRow("", browse_btn)

        layout.addWidget(parse_group)

        # ------ Управление сессией ------
        session_group = QGroupBox("🔐 Управление сессией")
        session_layout = QVBoxLayout(session_group)

        self.clear_session_btn = QPushButton("🗑️ Очистить сессию (повторная авторизация)")
        self.clear_session_btn.clicked.connect(self.clear_session)
        session_layout.addWidget(self.clear_session_btn)

        session_info = QLabel("💡 Сессия сохраняется между запусками. Очистите её, если нужно войти под другим аккаунтом.")
        session_info.setStyleSheet("color: #666; padding: 5px;")
        session_layout.addWidget(session_info)

        layout.addWidget(session_group)

        layout.addStretch()

    def setup_parser_tab(self):
        parser_widget = QWidget()
        self.tabs.addTab(parser_widget, "🚀 Парсинг")
        layout = QVBoxLayout(parser_widget)

        input_group = QGroupBox("🔗 Ссылка")
        input_layout = QVBoxLayout(input_group)
        self.chat_link_input = QLineEdit()
        self.chat_link_input.setPlaceholderText("https://t.me/... или @username")
        input_layout.addWidget(self.chat_link_input)

        mode_layout = QHBoxLayout()
        mode_label = QLabel("🛠️ Режим:")
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Участники", "Комментарии", "Сообщения", "Реакции"])
        mode_layout.addWidget(mode_label)
        mode_layout.addWidget(self.mode_combo)
        mode_layout.addStretch()
        input_layout.addLayout(mode_layout)

        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("🚀 Начать")
        self.start_btn.clicked.connect(self.start_parsing)
        self.stop_btn = QPushButton("⏹️ Остановить")
        self.stop_btn.clicked.connect(self.stop_parsing)
        self.stop_btn.setEnabled(False)
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)
        input_layout.addLayout(btn_layout)

        layout.addWidget(input_group)

        # Progress section
        progress_group = QGroupBox("📊 Прогресс")
        progress_layout = QVBoxLayout(progress_group)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        progress_layout.addWidget(self.progress_bar)
        self.status_text = QTextEdit()
        self.status_text.setReadOnly(True)
        self.status_text.setMaximumHeight(180)
        progress_layout.addWidget(self.status_text)
        layout.addWidget(progress_group)
        layout.addStretch()

    def setup_results_tab(self):
        results_widget = QWidget()
        self.tabs.addTab(results_widget, "📋 Результаты")
        layout = QVBoxLayout(results_widget)

        btn_layout = QHBoxLayout()
        self.save_csv_btn = QPushButton("💾 Сохранить CSV")
        self.save_csv_btn.clicked.connect(self.save_csv)
        self.save_csv_btn.setEnabled(False)
        self.clear_results_btn = QPushButton("🗑️ Очистить")
        self.clear_results_btn.clicked.connect(self.clear_results)
        btn_layout.addWidget(self.save_csv_btn)
        btn_layout.addWidget(self.clear_results_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self.results_table = QTableWidget()
        layout.addWidget(self.results_table)

    # --- Helper GUI methods -------------------------------------------------
    def browse_save_path(self):
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку")
        if folder:
            self.save_path_input.setText(folder)

    def setup_logging(self):
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    def reset_ui_before_start(self, max_value: int):
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(max_value)
        self.progress_bar.setValue(0)
        self.status_text.clear()
        self.tabs.setCurrentIndex(1)

    def reset_ui(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setVisible(False)
        self.progress_bar.setValue(0)

    def stop_parsing(self):
        if self.parser_thread and self.parser_thread.isRunning():
            self.parser_thread.stop()
            self.update_status("⏹️ Остановка…")
            if not self.parser_thread.wait(5000):
                self.parser_thread.terminate()
            self.update_status("✅ Остановлено")
        self.reset_ui()

    def update_status(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.status_text.append(f"[{timestamp}] {message}")
        cursor = self.status_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.status_text.setTextCursor(cursor)

    def parsing_finished(self, title: str, data: List[Dict[str, Any]]):
        self.parsed_data = data
        self.update_status(f"✅ Завершено! Получено {len(data)} записей")
        self.fill_results_table(data)
        self.tabs.setCurrentIndex(2)
        self.reset_ui()
        self.save_csv_btn.setEnabled(True)

    def parsing_error(self, message: str):
        self.update_status(message)
        QMessageBox.critical(self, "Ошибка", message)
        self.reset_ui()

    def fill_results_table(self, data: List[Dict[str, Any]]):
        if not data:
            return
        headers = list(data[0].keys())
        self.results_table.setColumnCount(len(headers))
        self.results_table.setRowCount(len(data))
        self.results_table.setHorizontalHeaderLabels(headers)
        for row, item in enumerate(data):
            for col, header in enumerate(headers):
                self.results_table.setItem(row, col, QTableWidgetItem(str(item.get(header, ''))))
        self.results_table.resizeColumnsToContents()

    def save_csv(self):
        if not self.parsed_data:
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"telegram_parsed_{timestamp}.csv"
        filename, _ = QFileDialog.getSaveFileName(self, "Сохранить CSV", os.path.join(self.save_path_input.text(), default_name), "CSV files (*.csv)")
        if filename:
            try:
                with open(filename, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=list(self.parsed_data[0].keys()))
                    writer.writeheader()
                    writer.writerows(self.parsed_data)
                QMessageBox.information(self, "Успех", f"Файл сохранен: {filename}")
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", str(e))

    def clear_results(self):
        self.parsed_data = []
        self.results_table.setRowCount(0)
        self.save_csv_btn.setEnabled(False)

    def handle_auth_code(self, message: str):
        code, ok = QInputDialog.getText(self, "Авторизация", message, QLineEdit.EchoMode.Normal)
        if ok and code:
            self.parser_thread.auth_code = code.strip()
        else:
            self.parser_thread.auth_code = ""

    def handle_auth_password(self):
        pwd, ok = QInputDialog.getText(self, "Пароль 2FA", "Введите пароль:", QLineEdit.EchoMode.Password)
        if ok and pwd:
            self.parser_thread.auth_password = pwd
        else:
            self.parser_thread.auth_password = ""

    def start_parsing(self):
        """Запуск парсинга в выбранном режиме"""
        if not all([self.api_id_input.text(), self.api_hash_input.text(), self.chat_link_input.text()]):
            QMessageBox.warning(self, "Ошибка", "Заполните все обязательные поля!")
            return

        try:
            max_items = int(self.max_members_input.text())
        except ValueError:
            QMessageBox.warning(self, "Ошибка", "Введите корректное число!")
            return

        # Останавливаем предыдущий поток, если он активен
        if self.parser_thread and self.parser_thread.isRunning():
            self.parser_thread.stop()
            self.parser_thread.wait(3000)

        # Настройка UI
        self.reset_ui_before_start(max_items)

        mode = self.mode_combo.currentText()
        link = self.chat_link_input.text().strip()
        is_post_link = re.search(r"/\d+(?:\?.*)?$", link) is not None

        if mode == "Комментарии":
            if not is_post_link:
                QMessageBox.warning(self, "Ошибка", "Ссылка не является ссылкой на пост.")
                self.reset_ui()
                return
            self.parser_thread = CommentsParserThread(self.api_id_input.text(), self.api_hash_input.text(), link, max_items, self.session_name)
        elif mode == "Сообщения":
            self.parser_thread = MessagesParserThread(self.api_id_input.text(), self.api_hash_input.text(), link, max_items, self.session_name)
        elif mode == "Реакции":
            if not is_post_link:
                QMessageBox.warning(self, "Ошибка", "Ссылка не является ссылкой на пост.")
                self.reset_ui()
                return
            self.parser_thread = ReactionsParserThread(self.api_id_input.text(), self.api_hash_input.text(), link, max_items, self.session_name)
        else:
            self.parser_thread = MembersParserThread(self.api_id_input.text(), self.api_hash_input.text(), link, max_items, self.session_name)

        # Подключаем сигналы
        self.parser_thread.progress_signal.connect(self.update_status)
        self.parser_thread.progress_value.connect(self.progress_bar.setValue)
        self.parser_thread.finished_signal.connect(self.parsing_finished)
        self.parser_thread.error_signal.connect(self.parsing_error)
        self.parser_thread.auth_code_needed.connect(self.handle_auth_code)
        self.parser_thread.auth_password_needed.connect(self.handle_auth_password)

        # Запуск
        self.parser_thread.start()

    def closeEvent(self, event):
        if self.parser_thread and self.parser_thread.isRunning():
            self.parser_thread.stop()
            self.parser_thread.wait(3000)
        event.accept()

    def clear_session(self):
        try:
            for file in Path.cwd().glob(f"{self.session_name}*.session*"):
                file.unlink()
            QMessageBox.information(self, "Успех", "Сессия очищена. При следующем парсинге потребуется повторная авторизация.")
        except Exception as e:
            QMessageBox.warning(self, "Ошибка", f"Не удалось очистить сессию: {e}")


# ----------------------------------------------------------------------------
# Application entry point
# ----------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    window = TelegramParserGUI()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()