# =============================================================================
# security.py — Password Hashing and JWT Handling
#
# Password hashing: PBKDF2-HMAC-SHA256 via Python's built-in hashlib.
#   - No native compiled dependencies (avoids the metadata-generation-failed
#     pip error that bcrypt/argon2 can cause on some machines).
#   - 100,000 iterations — meets NIST SP 800-132 recommendation.
#   - Legally unrestricted in the USA and India (one-way hash, not encryption).
#   - The same algorithm and iteration count MUST be used in the Databricks
#     seed script (02_seed_users.py), or logins will fail.
#
# JWT: python-jose signs tokens with HS256.
#   - Tokens are long-lived (~1 year) to implement persistent login.
#   - The JWT carries: user_id, username, role, department, and all permission
#     flags so the API never needs a DB lookup on every request.
# =============================================================================

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from config import settings


# =============================================================================
# PASSWORD HASHING
# =============================================================================

PBKDF2_ITERATIONS = 100_000     # Must match 02_seed_users.py exactly


def hash_password(password: str) -> tuple[str, str]:
    """
    Hash a plain-text password.

    Returns
    -------
    (hash_hex, salt_hex)
        Both as lowercase hex strings, ready to store in the users table.
    """
    salt = secrets.token_hex(16)        # 16 random bytes = 32 hex chars
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        PBKDF2_ITERATIONS,
    )
    return dk.hex(), salt


def verify_password(plain_password: str, stored_hash: str, stored_salt: str) -> bool:
    """
    Verify a plain-text password against the stored hash and salt.
    Returns True if the password matches, False otherwise.
    """
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        plain_password.encode("utf-8"),
        bytes.fromhex(stored_salt),
        PBKDF2_ITERATIONS,
    )
    return dk.hex() == stored_hash


# =============================================================================
# JWT TOKENS
# =============================================================================

def create_access_token(data: dict) -> str:
    """
    Create a signed JWT token containing the given payload.
    The token expires after settings.jwt_expire_minutes (default ~1 year).
    """
    payload = data.copy()
    expire  = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload.update({"exp": expire})
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Optional[dict]:
    """
    Decode and validate a JWT token.
    Returns the payload dict if valid, None if expired or tampered.
    """
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None