import os
import hmac
import secrets
import string
import hashlib
import json
import bcrypt


def generate_password(length=12):
    characters = string.ascii_letters + string.digits + string.punctuation
    return ''.join(secrets.choice(characters) for _ in range(length))


def hash_password(password):
    # bcrypt keeps its salt inside the hash and is deliberately slow against brute force
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password, record):
    if 'salt' in record:  # legacy salted SHA-256 record, upgraded on next successful login
        legacy = hashlib.sha256(bytes.fromhex(record['salt']) + password.encode()).hexdigest()
        return hmac.compare_digest(legacy, record['password'])
    return bcrypt.checkpw(password.encode(), record['password'].encode())


def needs_rehash(record):
    return 'salt' in record


def make_record(password):
    return {'password': hash_password(password)}


def update_user_data(users, user_data_file):
    with open(user_data_file, 'w') as f:
        json.dump(users, f)


def load_users(user_data_file):
    if not os.path.exists(user_data_file):
        users = {name: make_record(f'{name}pass') for name in ('user1', 'user2', 'user3', 'user4', 'user5', 'admin')}
        update_user_data(users, user_data_file)
    else:
        with open(user_data_file, 'r') as f:
            users = json.load(f)

        for username, user_data in users.items():
            if isinstance(user_data, str):
                users[username] = make_record(user_data)
        update_user_data(users, user_data_file)

    return users
