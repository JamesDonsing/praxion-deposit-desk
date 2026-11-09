"""Tiny in-memory sliding-window rate limiter (cost and abuse control on a public demo)."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

LIMITS = {  # bucket: (max requests, window seconds)
    "draft": (20, 60),
    "send": (10, 60),
    "webhook": (120, 60),
    "reset": (5, 60),
}


class RateLimiter:
    def __init__(self, enabled: bool = True, limits: dict | None = None):
        self.enabled = enabled
        self.limits = limits or LIMITS
        self.hits: dict[tuple[str, str], deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, bucket: str, key: str) -> bool:
        if not self.enabled:
            return True
        limit, window = self.limits[bucket]
        now = time.monotonic()
        with self.lock:
            q = self.hits[(bucket, key)]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True
