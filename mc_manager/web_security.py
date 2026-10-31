"""Private controller boundary and browser request protections."""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from urllib.parse import urlsplit

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


def origin(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        return parsed.scheme, parsed.hostname.casefold(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


class LoginThrottle:
    """Bounded, process-local limits. The controller runs one worker."""
    def __init__(self, clock=time.monotonic):
        self.clock, self.buckets = clock, OrderedDict()

    def check(self, address, username):
        now = self.clock()
        limits = [("global", 60, 60), ("ip:" + address, 15, 60), ("user:" + username.casefold(), 6, 300)]
        for key, count, window in limits:
            queue = self.buckets.setdefault(key, deque())
            while queue and queue[0] <= now - window:
                queue.popleft()
            self.buckets.move_to_end(key)
            if len(queue) >= count:
                raise HTTPException(429, "Too many sign-in attempts. Try again later.", headers={"Retry-After": str(max(1, int(queue[0] + window - now) + 1))})
        for key, _, _ in limits:
            self.buckets[key].append(now)
        while len(self.buckets) > 4096:
            self.buckets.popitem(last=False)


class ControllerSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, panel, secure_cookies):
        super().__init__(app)
        self.panel, self.secure_cookies = panel, secure_cookies

    async def dispatch(self, request, call_next):
        try:
            if request.url.path.startswith("/api/"):
                if request.url.path not in {"/api/login", "/api/session", "/api/logout"}:
                    self.panel.require(request)
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
                        raise HTTPException(403, "Cross-origin requests are not allowed")
                    source = request.headers.get("origin")
                    if source is None:
                        source = request.headers.get("referer")
                    target = origin(str(request.url))
                    if self.secure_cookies and target:
                        target = ("https", target[1], 443 if target[2] == 80 else target[2])
                    if source is not None:
                        if origin(source) is None or origin(source) != target:
                            raise HTTPException(403, "Cross-origin requests are not allowed")
                    elif request.headers.get("x-monkeycraft-request") != "1":
                        # Custom headers require a browser preflight; this private
                        # controller never enables cross-origin CORS access.
                        raise HTTPException(403, "A same-origin request is required")
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        if self.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response
