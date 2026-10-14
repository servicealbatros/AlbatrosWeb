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
from reception_bridge import bridge_url, dashboard_route, forward, verify_request

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
        if path == '/provider-auth' or path.startswith('/api/provider-auth/'):
            # A one-use administrator grant unlocks login-only supplier controls.
            # The scoped cookie grants no dashboard or robot permissions.
            return await self.application(scope, receive, send)
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
        elif path == '/api/admin/worker-backup' and method == 'POST':
            try:
                POLICY.require_admin(request.headers.get('authorization'))
            except SecurityViolation:
                response = JSONResponse({'detail': 'Administrator access required'}, status_code=403)
            else:
                from cloud_backup import receive_backup
                response = await receive_backup(request)
        elif path == "/webhook/whatsapp" and method in {"GET", "POST"}:
            # GET verifies the Meta token; POST verifies X-Hub-Signature-256.
            if method == 'POST' and bridge_url():
                import hashlib
                import hmac
                secret = os.getenv('WHATSAPP_APP_SECRET', '')
                body = await request.body()
                expected = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
                if not secret or not hmac.compare_digest(expected, request.headers.get('x-hub-signature-256', '')):
                    response = JSONResponse({'detail': 'Invalid webhook signature'}, status_code=401)
                else:
                    response = await forward(request, 'meta-webhook')
        elif (os.getenv('ALBATROS_INTERNAL_BRIDGE') == 'true' and dashboard_route(path)
              and request.headers.get('x-albatros-signature')):
            # An authenticated reception gateway can delegate to the Linux worker.
            # Re-sign internally with a fresh nonce; never consume one twice.
            try:
                target = path + ('?' + request.url.query if request.url.query else '')
                actor = verify_request(request.headers, method, target, await request.body())
            except (ValueError, TypeError):
                response = JSONResponse({'detail': 'Invalid bridge signature'}, status_code=403)
            else:
                response = await forward(request, actor)
        elif not is_authenticated(request):
            response = (RedirectResponse("/receptie/login", status_code=303)
                        if method == "GET" and "text/html" in request.headers.get("accept", "")
                        else JSONResponse({"detail": "Autentificare necesară."}, status_code=401))
        elif bridge_url() and dashboard_route(path):
            user = POLICY.verify_dashboard_session(request.cookies.get('albatros_reception'))
            if not user:
                POLICY.require_admin(request.headers.get('authorization'))
                user = 'master:cloud-administrator' if path == '/master' or path.startswith('/api/dashboard/master/') else 'cloud-administrator'
            elif POLICY.is_master_session(request.cookies.get('albatros_reception')):
                user = 'master:' + user
            if method not in {'GET', 'HEAD', 'OPTIONS'} and user not in {'cloud-administrator', 'master:cloud-administrator'}:
                from app import _same_origin_request
                if not _same_origin_request(request):
                    response = JSONResponse({'detail': 'Cerere respinsă: origine invalidă'}, status_code=403)
            if response is None:
                response = await forward(request, user)
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
