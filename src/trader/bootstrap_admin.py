"""One-time interactive operator enrollment. Invoke from a secure terminal."""
from __future__ import annotations

import asyncio
import getpass
from urllib.parse import quote

from trader.config import Settings
from trader.security import new_totp_secret, password_hash
from trader.storage import Storage


async def main():
    settings = Settings.load()
    user = input("Operator username: ").strip()
    if not (3 <= len(user) <= 100) or not user.replace("_", "").isalnum():
        raise ValueError("Use an alphanumeric username (underscores permitted), length 3–100")
    first = getpass.getpass("Password (14+ characters): ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise ValueError("passwords do not match")
    hashed = password_hash(first)
    secret = new_totp_secret()
    db = Storage(settings.database_url)
    await db.open()
    try:
        assert db.pool
        async with db.pool.connection() as conn:
            await conn.execute("INSERT INTO users(username,password_hash,role,totp_secret) VALUES (%s,%s,%s,%s)",
                               (user, hashed, "ADMIN", secret))
        await db.audit(user, "auth.enroll", "accepted", {"role": "ADMIN"})
    finally:
        await db.close()
    print("\nMFA enrollment: add this URI to your authenticator app (KEEP PRIVATE):")
    print(f"otpauth://totp/{quote('AegisTrade:'+user)}?secret={secret}&issuer=AegisTrade&algorithm=SHA1&digits=6&period=30")
    print("Enrollment complete. TOTP and password required for every login.\n")


if __name__ == "__main__":
    asyncio.run(main())
