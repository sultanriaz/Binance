"""Production webhook alerting for critical live trading events (Discord / Slack / Telegram)."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
import httpx

LOG = logging.getLogger(__name__)

COLOR_MAP = {
    'CRITICAL': 15158332,  # Red
    'WARNING': 15844367,   # Gold
    'INFO': 3447003,       # Blue
}

async def send_alert(title: str, message: str, level: str = 'INFO', metadata: dict | None = None) -> bool:
    """Dispatch an alert to configured webhook. Non-blocking; fails open so alerts never disrupt trading."""
    webhook_url = os.getenv('ALERT_WEBHOOK_URL') or os.getenv('DISCORD_WEBHOOK_URL') or os.getenv('SLACK_WEBHOOK_URL')
    LOG.info("[ALERT %s] %s: %s (meta: %s)", level, title, message, metadata or {})
    if not webhook_url:
        return False

    payload = {
        'content': f"🚨 **[{level}] {title}**\n{message}",
        'text': f"[{level}] {title}\n{message}\n{metadata or ''}",
        'embeds': [{
            'title': title,
            'description': message,
            'color': COLOR_MAP.get(level, 3447003),
            'fields': [{'name': str(k), 'value': str(v)[:1024], 'inline': True} for k, v in (metadata or {}).items()],
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }],
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4.0)) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code >= 400:
                LOG.warning("Webhook alert failed with HTTP %s: %s", resp.status_code, resp.text[:120])
                return False
            return True
    except Exception as exc:
        LOG.warning("Webhook alert delivery failed: %s", exc)
        return False

