import time
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QComboBox
from utilities.user_management import verify_password, needs_rehash, make_record, update_user_data, generate_password, load_users

user_data_file = "user_data.json"
users = load_users(user_data_file)

RED, GREEN = "#e07a7a", "#7fc79b"
MAX_ATTEMPTS = 5      # failed logins per username before a lockout
LOCKOUT_SECONDS = 30


class LoginDialog(QDialog):
    """Accepts when a pilot logs in; the admin account opens user management instead."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("GÖKBAĞ")
        self.setFixedWidth(420)
        # shortcut: lockout state is in memory, so restarting the app resets it; persist it if the threat model needs that
        self.failed_attempts = {}
        self.locked_until = {}

        title = QLabel("GÖKBAĞ")
        title.setObjectName("Title")
        title.setStyleSheet("font-size: 20px;")
        title.setAlignment(Qt.AlignCenter)
        subtitle = QLabel("Drone ground control")
        subtitle.setObjectName("Muted")
        subtitle.setAlignment(Qt.AlignCenter)
        self.username = QLineEdit(placeholderText="Username")
        self.password = QLineEdit(placeholderText="Password", echoMode=QLineEdit.Password)
        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {RED};")
        self.error.setAlignment(Qt.AlignCenter)
        login_button = QPushButton("Log in")
        login_button.setDefault(True)  # Enter logs in
        login_button.setMinimumHeight(48)
        login_button.setStyleSheet("background: #4d8dff; border: none; color: white; font-size: 15px; font-weight: 600;")
        login_button.clicked.connect(self.login)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 36, 36, 36)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(14)
        layout.addWidget(self.username)
        layout.addWidget(self.password)
        layout.addWidget(self.error)
        layout.addWidget(login_button)

    def login(self):
        username = self.username.text()
        password = self.password.text()

        remaining = self.locked_until.get(username, 0) - time.monotonic()
        if remaining > 0:
            self.error.setText(f"Too many attempts, try again in {int(remaining) + 1} s")
            return

        if username in users and verify_password(password, users[username]):
            self.failed_attempts.pop(username, None)
            if needs_rehash(users[username]):
                users[username] = make_record(password)
                update_user_data(users, user_data_file)
            self.error.setText("")
            if username == 'admin':
                self.password.clear()
                AdminDialog(self).exec()
            else:
                self.accept()
            return

        # counted for unknown usernames too, so the message never reveals which names exist
        self.failed_attempts[username] = self.failed_attempts.get(username, 0) + 1
        if self.failed_attempts[username] >= MAX_ATTEMPTS:
            self.failed_attempts[username] = 0
            self.locked_until[username] = time.monotonic() + LOCKOUT_SECONDS
            self.error.setText(f"Too many attempts, locked for {LOCKOUT_SECONDS} s")
        else:
            self.error.setText("Wrong username or password")


class AdminDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("User management")
        self.setFixedWidth(420)
        title = QLabel("User management")
        title.setStyleSheet("font-size: 17px; font-weight: 600;")
        self.user_menu = QComboBox()
        self.user_menu.addItems(list(users))
        self.new_password = QLineEdit(placeholderText="New password")
        save = QPushButton("Save new password")
        save.clicked.connect(self.save_password)
        random_button = QPushButton("Make a random password")
        random_button.clicked.connect(self.assign_random_password)
        # read-only line edit so the admin can select and copy a generated password
        self.status = QLineEdit(readOnly=True, alignment=Qt.AlignCenter)
        self.status.setStyleSheet("background: transparent; border: none;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(self.user_menu)
        layout.addWidget(self.new_password)
        buttons = QHBoxLayout()
        buttons.addWidget(save)
        buttons.addWidget(random_button)
        layout.addLayout(buttons)
        layout.addWidget(self.status)

    def show_status(self, text, color):
        self.status.setText(text)
        self.status.setStyleSheet(f"background: transparent; border: none; color: {color};")

    def set_password(self, new_password):
        users[self.user_menu.currentText()] = make_record(new_password)
        update_user_data(users, user_data_file)

    def save_password(self):
        new_password = self.new_password.text()
        if not new_password:
            self.show_status("Type a new password first", RED)
            return
        self.set_password(new_password)
        self.new_password.clear()
        self.show_status(f"Password updated for {self.user_menu.currentText()}", GREEN)

    def assign_random_password(self):
        new_password = generate_password()
        self.set_password(new_password)
        self.show_status(f"{self.user_menu.currentText()}: {new_password}", GREEN)
