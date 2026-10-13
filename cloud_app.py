"""Render entry point: deny access to every application route by default.

Public visitors can only reach the login form, minimal health check and Meta
webhook. The existing signed sessions and device approvals remain in force.
"""
import os
from pathlib import Path

os.environ["ALBATROS_CLOUD_MODE"] = "true"
os.environ.setdefault("ALLOW_REMOTE_DASHBOARD", "true")
os.environ.setdefault("ALLOWED_RECEPTION_IPS", "")
os.environ.setdefault("DASHBOARD_COOKIE_SECURE", "true")
os.environ.setdefault("ROBOT_ENABLED", "false")

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from app import app as backend
from security import POLICY, SecurityViolation

BASE = Path(__file__).resolve().parent


def is_authenticated(request):
    if POLICY.verify_dashboard_session(request.cookies.get("albatros_reception")):
        return True
    try:
        POLICY.require_admin(request.headers.get("authorization"))
        return True
    except SecurityViolation:
        return False


class CloudGateway:
    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.application(scope, receive, send)
        if scope["type"] != "http":
            return await send({"type": "websocket.close", "code": 1008})
        request = Request(scope, receive=receive)
        path, method = request.url.path, request.method
        response = None
        if path == "/health" and method in {"GET", "HEAD"}:
            response = JSONResponse({"ok": True})
        elif path == "/robots.txt" and method == "GET":
            response = PlainTextResponse("User-agent: *\nDisallow: /\n")
        elif path in {"/", "/login"} and method == "GET":
            response = RedirectResponse("/receptie/login", status_code=303)
        elif path == "/receptie/login" and method in {"GET", "HEAD"}:
            response = FileResponse(BASE / "static" / "cloud_login.html")
        elif path == "/receptie/login" and method == "POST":
            pass
        elif path == "/webhook/whatsapp" and method in {"GET", "POST"}:
            # GET verifies the Meta token; POST verifies X-Hub-Signature-256.
            pass
        elif not is_authenticated(request):
            response = (RedirectResponse("/receptie/login", status_code=303)
                        if method == "GET" and "text/html" in request.headers.get("accept", "")
                        else JSONResponse({"detail": "Autentificare necesară."}, status_code=401))
        elif path.endswith(("/offer-start", "/technician-start", "/labour-only-start",
                            "/vehicle-data", "/dsm/import-week", "/dsm-history/start")):
            # These routes currently start laptop/browser processes. Do not
            # pretend they work in a Linux container without a connected worker.
            response = JSONResponse({"detail": "Workerul de catalog nu este conectat la server."}, status_code=503)
        if response is not None:
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            return await response(scope, receive, send)
        return await self.application(scope, receive, send)


app = CloudGateway(backend)
