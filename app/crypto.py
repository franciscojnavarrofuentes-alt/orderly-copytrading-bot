from cryptography.fernet import Fernet


def build_fernet(master_key: str) -> Fernet:
    return Fernet(master_key.encode("utf-8"))


def encrypt_secret(fernet: Fernet, secret: str) -> str:
    token = fernet.encrypt(secret.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_secret(fernet: Fernet, token: str) -> str:
    raw = fernet.decrypt(token.encode("utf-8"))
    return raw.decode("utf-8")
