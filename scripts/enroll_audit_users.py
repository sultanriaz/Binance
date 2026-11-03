"""Enroll two audit test operators with MFA for E2E testing."""
import asyncio
from trader.config import Settings
from trader.security import new_totp_secret, password_hash
from trader.storage import Storage

async def enroll(user: str, password: str):
    settings = Settings.load()
    hashed = password_hash(password)
    secret = new_totp_secret()
    db = Storage(settings.database_url)
    await db.open()
    try:
        assert db.pool
        async with db.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO users(username, password_hash, role, totp_secret, enabled)
                   VALUES (%s, %s, %s, %s, true)
                   ON CONFLICT (username) DO UPDATE SET password_hash=%s, totp_secret=%s, enabled=true""",
                (user, hashed, 'ADMIN', secret, hashed, secret)
            )
        await db.audit(user, 'auth.enroll', 'accepted', {'role': 'ADMIN', 'audit': True})
    finally:
        await db.close()
    print(f"ENROLLED:{user}:{secret}")

async def main():
    await enroll("audit_lead_admin", "AuditorLeadPasswd2026!#")
    await enroll("audit_peer_admin", "AuditorPeerPasswd2026!#")

if __name__ == "__main__":
    asyncio.run(main())

