"""Authentication module with hardcoded secret."""

SECRET_KEY = "super-duper-secret-key-that-should-not-be-here-1234567890"


def check_token(token: str) -> bool:
    """Verify a token against the hardcoded secret key."""
    return token == SECRET_KEY


def hash_password(password: str) -> str:
    import hashlib
    return hashlib.sha256(password.encode()).hexdigest()
