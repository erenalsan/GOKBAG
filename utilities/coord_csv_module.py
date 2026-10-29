import csv
import io
import os
from datetime import datetime
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Create 'flight_logs' directory if it doesn't exist
log_folder = "flight_logs"
os.makedirs(log_folder, exist_ok=True)

# The key lives outside the repository so the source code alone cannot decrypt logs.
# Losing this file means losing access to every log encrypted with it.
key_file = os.environ.get("GOKBAG_LOG_KEY_FILE", os.path.expanduser("~/.config/gokbag/flight_log.key"))

# Logs written before 2026 used AES-CBC with this key, which was committed to the repo; read-only support.
LEGACY_KEY = b'Sixteen byte key'

PERSON = "person"  # 4th column marking a search & rescue sighting


def get_key():
    if not os.path.exists(key_file):
        os.makedirs(os.path.dirname(key_file), exist_ok=True)
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(Fernet.generate_key())
    with open(key_file, 'rb') as f:
        return f.read().strip()


def make_csv(coords, filename=None, detections=()):
    # Name is taken per call so each flight in a session gets its own log
    if filename is None:
        filename = os.path.join(log_folder, f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.csv")

    # Build the CSV in memory so the plaintext never touches the disk
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(coords)
    writer.writerows(tuple(point) + (PERSON,) for point in detections)

    with open(filename, 'wb') as f:
        f.write(Fernet(get_key()).encrypt(buffer.getvalue().encode('utf-8')))
    print(f'Encrypted flight log "{filename}" saved.')


def decrypt_file(filename):
    with open(filename, 'rb') as f:
        data = f.read()

    # Fernet tokens are base64 text starting with "gAAAAA"; anything else is a legacy AES-CBC log
    if data.startswith(b'gAAAAA'):
        return Fernet(get_key()).decrypt(data)

    iv, encrypted_data = data[:16], data[16:]
    decryptor = Cipher(algorithms.AES(LEGACY_KEY), modes.CBC(iv)).decryptor()
    decrypted_data = decryptor.update(encrypted_data) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    return unpadder.update(decrypted_data) + unpadder.finalize()


def read_flight_log(filename):
    """Returns (path, sightings), each a list of (x, y, z)."""
    path, sightings = [], []
    for row in csv.reader(decrypt_file(filename).decode('utf-8').splitlines()):
        if not row:
            continue
        point = (float(row[0]), float(row[1]), float(row[2]))
        (sightings if len(row) > 3 and row[3] == PERSON else path).append(point)
    return path, sightings
