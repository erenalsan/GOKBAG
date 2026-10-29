import time
import customtkinter
from utilities.user_management import verify_password, needs_rehash, make_record, update_user_data, generate_password, load_users

user_data_file = "user_data.json"
users = load_users(user_data_file)

MUTED, RED, GREEN = "gray60", "#d64545", "#2fa572"
MAX_ATTEMPTS = 5      # failed logins per username before a lockout
LOCKOUT_SECONDS = 30


class LoginApp:
    def __init__(self, root):
        self.root = root
        self.root.title("GÖKBAĞ Login")
        self.root.resizable(False, False)

        card = customtkinter.CTkFrame(root, corner_radius=12)
        card.pack(padx=24, pady=24)

        customtkinter.CTkLabel(card, text="GÖKBAĞ", font=customtkinter.CTkFont(size=28, weight="bold")).pack(padx=40, pady=(28, 0))
        customtkinter.CTkLabel(card, text="Drone ground control", text_color=MUTED).pack(pady=(0, 20))

        self.username_entry = customtkinter.CTkEntry(card, placeholder_text="Username", width=260, height=38)
        self.username_entry.pack(padx=32, pady=(0, 10))
        self.password_entry = customtkinter.CTkEntry(card, placeholder_text="Password", show='•', width=260, height=38)
        self.password_entry.pack(padx=32)

        self.error_label = customtkinter.CTkLabel(card, text="", text_color=RED)
        self.error_label.pack(pady=4)

        self.login_button = customtkinter.CTkButton(card, text="Log in", width=260, height=38, command=self.login)
        self.login_button.pack(padx=32, pady=(0, 28))

        self.logged_in = False
        # shortcut: lockout state is in memory, so restarting the app resets it; persist it if the threat model needs that
        self.failed_attempts = {}
        self.locked_until = {}
        # on release: acting on key-down and closing the window loses the key-up, leaving Enter stuck system-wide
        self.root.bind('<KeyRelease-Return>', lambda event: self.login())
        self.root.bind('<KeyRelease-KP_Enter>', lambda event: self.login())
        self.username_entry.focus_set()

    def login(self):
        username = self.username_entry.get()
        password = self.password_entry.get()

        remaining = self.locked_until.get(username, 0) - time.monotonic()
        if remaining > 0:
            self.error_label.configure(text=f"Too many attempts, try again in {int(remaining) + 1} s")
            return

        if username in users and verify_password(password, users[username]):
            self.failed_attempts.pop(username, None)
            if needs_rehash(users[username]):
                users[username] = make_record(password)
                update_user_data(users, user_data_file)
            self.error_label.configure(text="")
            if username == 'admin':
                self.admin_page()
            else:
                self.user_page()
            return

        # counted for unknown usernames too, so the message never reveals which names exist
        self.failed_attempts[username] = self.failed_attempts.get(username, 0) + 1
        if self.failed_attempts[username] >= MAX_ATTEMPTS:
            self.failed_attempts[username] = 0
            self.locked_until[username] = time.monotonic() + LOCKOUT_SECONDS
            self.error_label.configure(text=f"Too many attempts, locked for {LOCKOUT_SECONDS} s")
        else:
            self.error_label.configure(text="Invalid username or password")

    def admin_page(self):
        self.admin_window = customtkinter.CTkToplevel(self.root)
        self.admin_window.title("User Management")
        self.admin_window.resizable(False, False)
        self.admin_window.transient(self.root)

        card = customtkinter.CTkFrame(self.admin_window, corner_radius=12)
        card.pack(padx=20, pady=20)

        customtkinter.CTkLabel(card, text="User management", font=customtkinter.CTkFont(size=18, weight="bold")).pack(padx=28, pady=(20, 12))

        self.user_menu = customtkinter.CTkOptionMenu(card, values=list(users), width=260)
        self.user_menu.pack(padx=28, pady=(0, 10))

        self.new_password_entry = customtkinter.CTkEntry(card, placeholder_text="New password", width=260)
        self.new_password_entry.pack(padx=28, pady=(0, 10))

        customtkinter.CTkButton(card, text="Save new password", width=260, command=self.save_password).pack(padx=28, pady=(0, 8))
        customtkinter.CTkButton(card, text="Assign random password", width=260, fg_color="transparent", border_width=1,
                                border_color="gray40", command=self.assign_random_password).pack(padx=28)

        # selectable so the admin can copy a generated password
        self.admin_status = customtkinter.CTkEntry(card, width=260, fg_color="transparent", border_width=0, justify="center")
        self.admin_status.pack(padx=28, pady=(8, 16))

    def show_admin_status(self, text, color):
        self.admin_status.configure(state="normal", text_color=color)
        self.admin_status.delete(0, "end")
        self.admin_status.insert(0, text)
        self.admin_status.configure(state="readonly")

    def set_password(self, new_password):
        users[self.user_menu.get()] = make_record(new_password)
        update_user_data(users, user_data_file)

    def save_password(self):
        new_password = self.new_password_entry.get()
        if not new_password:
            self.show_admin_status("Enter a new password first", RED)
            return
        self.set_password(new_password)
        self.new_password_entry.delete(0, "end")
        self.show_admin_status(f"Password updated for {self.user_menu.get()}", GREEN)

    def assign_random_password(self):
        new_password = generate_password()
        self.set_password(new_password)
        self.show_admin_status(f"{self.user_menu.get()}: {new_password}", GREEN)

    def user_page(self):
        # main.py reuses this window for the controller once mainloop returns
        self.logged_in = True
        self.root.quit()
