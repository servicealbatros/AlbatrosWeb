"""Central security policy for Albatros AI Reception.

This module contains no provider credentials and exposes no generic process or
filesystem primitive to the agent.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, IntEnum
from hashlib import sha256, pbkdf2_hmac
from hmac import compare_digest, new as hmac_new
import base64
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from dashboard_devices import DashboardDevices
from typing import Any
from urllib.parse import urlparse


class AccessLevel(IntEnum):
    AUTO = 1
    CONFIRMATION_REQUIRED = 2
    HUMAN_ONLY = 3


class Action(str, Enum):
    READ_VIN = "read_vin"
    IDENTIFY_VEHICLE = "identify_vehicle"
    READ_HISTORY = "read_history"
    SEARCH_PARTS = "search_parts"
    READ_PART_CODE = "read_part_code"
    READ_PRICE = "read_price"
    READ_STOCK = "read_stock"
    SEARCH_LABOUR = "search_labour"
    PREPARE_OFFER = "prepare_offer"
    CREATE_REQUEST = "create_request"
    MODIFY_ESTIMATE = "modify_estimate"
    SEND_FINAL_OFFER = "send_final_offer"
    MODIFY_BOOKING = "modify_booking"
    MODIFY_CLIENT = "modify_client"
    DELETE_ESTIMATE = "delete_estimate"
    DELETE_CLIENT = "delete_client"
    DELETE_DATABASE = "delete_database"
    CHANGE_PASSWORD = "change_password"
    CREATE_USER = "create_user"
    MODIFY_PERMISSION = "modify_permission"
    INSTALL_SOFTWARE = "install_software"
    ARBITRARY_SHELL = "arbitrary_shell"


ACTION_LEVEL = {
    **{a: AccessLevel.AUTO for a in (
        Action.READ_VIN, Action.IDENTIFY_VEHICLE, Action.READ_HISTORY,
        Action.SEARCH_PARTS, Action.READ_PART_CODE, Action.READ_PRICE,
        Action.READ_STOCK, Action.SEARCH_LABOUR, Action.PREPARE_OFFER,
        Action.CREATE_REQUEST,
    )},
    **{a: AccessLevel.CONFIRMATION_REQUIRED for a in (
        Action.MODIFY_ESTIMATE, Action.SEND_FINAL_OFFER,
        Action.MODIFY_BOOKING, Action.MODIFY_CLIENT,
    )},
    **{a: AccessLevel.HUMAN_ONLY for a in (
        Action.DELETE_ESTIMATE, Action.DELETE_CLIENT, Action.DELETE_DATABASE, Action.CHANGE_PASSWORD,
        Action.CREATE_USER, Action.MODIFY_PERMISSION, Action.INSTALL_SOFTWARE,
        Action.ARBITRARY_SHELL,
    )},
}

DEFAULT_ALLOWED_DOMAINS = {
    "intercars": {"ro.e-cat.intercars.eu"},
    "autototal": {"www3.eoriginal.ro"},
    "dsm": {"master.dsmpro.eu", "apps.dsmpro.ro"},
    "meta": {"graph.facebook.com", "lookaside.fbsbx.com"},
    "openai": {"api.openai.com"},
}

INJECTION_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"execute\s+(this\s+)?command", r"reveal\s+(the\s+)?secrets?",
    r"send\s+(this\s+)?information", r"navigate\s+to\s+https?://",
    r"system\s*prompt", r"developer\s+message",
))
SENSITIVE_KEY = re.compile(r"(?i)(authorization|api.?key|token|secret|password|cookie)")
BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


class SecurityViolation(PermissionError):
    pass


class ConfirmationRequired(SecurityViolation):
    pass


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SENSITIVE_KEY.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    text = str(value) if value is not None else None
    if text is None:
        return None
    return OPENAI_KEY.sub("[REDACTED]", BEARER.sub("Bearer [REDACTED]", text))


class SecurityLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, *, action: str, result: str, success: bool,
              session_id: str | None = None, client_id: str | None = None,
              vin: str | None = None, user_agent: str = "agent",
              website: str | None = None, confirmation_required: bool = False,
              confirmation_user: str | None = None, error: str | None = None,
              details: dict | None = None) -> None:
        row = redact({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id, "client_id": client_id, "vin": vin,
            "user_agent": user_agent, "action": action, "website": website,
            "result": result, "success": success,
            "confirmation_required": confirmation_required,
            "confirmation_user": confirmation_user, "error": error,
            "details": details or {},
        })
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class Metrics:
    NAMES = (
        "requests_total", "requests_success", "requests_failed",
        "parts_searches", "labour_searches", "browser_errors",
        "human_confirmations", "blocked_actions",
    )
    def __init__(self):
        self._values = defaultdict(int)
        self._durations: list[float] = []
        self._lock = threading.Lock()

    def inc(self, name: str, amount: int = 1) -> None:
        if name not in self.NAMES:
            raise ValueError("Unknown metric")
        with self._lock:
            self._values[name] += amount

    def observe_response(self, seconds: float) -> None:
        with self._lock:
            self._durations.append(max(0.0, seconds))
            self._durations = self._durations[-1000:]

    def snapshot(self) -> dict:
        with self._lock:
            out = {name: self._values[name] for name in self.NAMES}
            out["average_response_time"] = round(sum(self._durations) / len(self._durations), 4) if self._durations else 0.0
            return out


class RateLimiter:
    def __init__(self):
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._events[key]
            while q and q[0] <= now - window_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


@dataclass
class Confirmation:
    token: str
    action: str
    resource_id: str
    user: str
    expires_at: float
    used: bool = False


class SecurityPolicy:
    def __init__(self, data_dir: Path | None = None):
        configured = data_dir or Path(os.getenv("ALBATROS_DATA_DIR", Path(__file__).with_name("data")))
        self.data_dir = configured.expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logger = SecurityLogger(self.data_dir / "security.log.jsonl")
        self.metrics = Metrics()
        self.rate_limiter = RateLimiter()
        self._confirmations: dict[str, Confirmation] = {}
        self._robot_override: bool | None = None
        self._robot_state_file = self.data_dir / "robot_enabled.state.json"
        self.devices = DashboardDevices(self.data_dir / 'dashboard_access.db')

    @property
    def beta_mode(self) -> bool:
        return env_bool("BETA_MODE", True)

    @property
    def robot_enabled(self) -> bool:
        if self._robot_override is not None:
            return self._robot_override
        try:
            state = json.loads(self._robot_state_file.read_text(encoding="utf-8"))
            if isinstance(state.get("enabled"), bool):
                return state["enabled"]
        except (OSError, ValueError, AttributeError):
            pass
        return env_bool("ROBOT_ENABLED", False)

    def set_robot_enabled(self, enabled: bool, user: str) -> None:
        self._robot_override = enabled
        temporary = self._robot_state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"enabled": enabled, "changed_by": user,
                                         "changed_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        temporary.replace(self._robot_state_file)
        self.logger.write(action="robot_enable" if enabled else "robot_disable", result="updated", success=True, user_agent=user)

    def require_admin(self, authorization: str | None) -> str:
        expected = os.getenv("ADMIN_API_TOKEN", "")
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not expected or not supplied or not compare_digest(expected, supplied):
            self.metrics.inc("blocked_actions")
            self.logger.write(action="admin_auth", result="blocked", success=False, error="invalid_or_missing_admin_token")
            raise SecurityViolation("Administrator authentication required")
        return os.getenv("ADMIN_USER", "administrator")

    def dashboard_password_spec(self, master):
        path = self.data_dir / 'dashboard_passwords.json'
        saved = json.loads(path.read_text()) if path.exists() else {}
        return saved.get('master' if master else 'reception') or os.getenv('MASTER_DASHBOARD_PASSWORD' if master else 'RECEPTION_DASHBOARD_PASSWORD', '')

    def dashboard_credentials_valid(self, username, password):
        master = username.strip() == os.getenv('MASTER_DASHBOARD_USER', 'master').strip()
        wanted = os.getenv('MASTER_DASHBOARD_USER', 'master') if master else os.getenv('RECEPTION_DASHBOARD_USER', 'consilier')
        spec = self.dashboard_password_spec(master)
        if not spec or not password or not compare_digest(username.strip(), wanted.strip()):
            return False
        if spec.startswith('pbkdf2$'):
            _, salt, digest = spec.split('$')
            return compare_digest(pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 300000).hex(), digest)
        return compare_digest(password, spec)

    def change_dashboard_password(self, master, password):
        if not 10 <= len(password) <= 128:
            raise SecurityViolation('Parola trebuie să aibă între 10 și 128 caractere.')
        path = self.data_dir / 'dashboard_passwords.json'
        saved = json.loads(path.read_text()) if path.exists() else {}
        salt = secrets.token_bytes(16)
        saved['master' if master else 'reception'] = 'pbkdf2$' + salt.hex() + '$' + pbkdf2_hmac('sha256', password.encode(), salt, 300000).hex()
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(saved), encoding='utf-8')
        temporary.replace(path)

    def create_dashboard_session(self, username: str, password: str, ttl_seconds: int | None = None, device_id: str | None = None) -> str:
        """Create a signed session for a reception workstation.

        Reception credentials are deliberately separate from ADMIN_API_TOKEN,
        so the workstation never receives robot or administrator credentials.
        """
        master = username.strip() == os.getenv('MASTER_DASHBOARD_USER', 'master').strip()
        expected_user = os.getenv('MASTER_DASHBOARD_USER', 'master').strip() if master else os.getenv("RECEPTION_DASHBOARD_USER", "consilier").strip()
        expected_password = self.dashboard_password_spec(master)
        secret = os.getenv("DASHBOARD_SESSION_SECRET", "")
        if (not expected_password or not secret or not username or not password
                or not compare_digest(username.strip(), expected_user)
                or not self.dashboard_credentials_valid(username, password)):
            self.metrics.inc("blocked_actions")
            self.logger.write(action="dashboard_login", result="blocked", success=False,
                              client_id=username or None, error="invalid_credentials_or_configuration")
            raise SecurityViolation("Date de autentificare invalide")
        now = int(time.time())
        ttl = int(ttl_seconds or os.getenv("DASHBOARD_SESSION_TTL_SECONDS", "28800"))
        try:
            sid = self.devices.issue(device_id, now + max(300, min(ttl, 86400))) if env_bool('DASHBOARD_DEVICE_APPROVAL', True) and not master else None
        except PermissionError as exc:
            raise SecurityViolation(str(exc)) from exc
        payload = json.dumps({"sub": expected_user, "role": "master" if master else "reception", "iat": now,
                              "credential_version": hmac_new(secret.encode(), expected_password.encode(), sha256).hexdigest(),
                              "sid": sid,
                              "exp": now + max(300, min(ttl, 86400))},
                             separators=(",", ":"), sort_keys=True).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hmac_new(secret.encode("utf-8"), encoded.encode("ascii"), sha256).hexdigest()
        self.logger.write(action="dashboard_login", result="authenticated", success=True,
                          client_id=expected_user, user_agent="reception_dashboard")
        return f"{encoded}.{signature}"

    def verify_dashboard_session(self, token: str | None) -> str | None:
        secret = os.getenv("DASHBOARD_SESSION_SECRET", "")
        if not secret or not token or "." not in token:
            return None
        encoded, signature = token.rsplit(".", 1)
        expected = hmac_new(secret.encode("utf-8"), encoded.encode("ascii"), sha256).hexdigest()
        if not compare_digest(expected, signature):
            return None
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
            if payload.get("role") not in {'reception', 'master'} or int(payload.get("exp") or 0) < int(time.time()):
                return None
            master = payload.get('role') == 'master'
            password = self.dashboard_password_spec(master)
            version = payload.get('credential_version')
            if not version and password.startswith('pbkdf2$'):
                return None
            if version and not compare_digest(version, hmac_new(secret.encode(), password.encode(), sha256).hexdigest()):
                return None
            if master and (not version or payload.get('sub') != os.getenv('MASTER_DASHBOARD_USER', 'master')):
                return None
            if env_bool('DASHBOARD_DEVICE_APPROVAL', True) and not master and not self.devices.valid(payload.get('sid')):
                return None
            username = str(payload.get("sub") or "").strip()
            return username or None
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def is_master_session(self, token: str | None) -> bool:
        if not self.verify_dashboard_session(token):
            return False
        encoded = token.rsplit('.', 1)[0]
        return json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))).get('role') == 'master'

    def create_confirmation(self, action: Action, resource_id: str, user: str, ttl_seconds: int = 600) -> str:
        if ACTION_LEVEL[action] != AccessLevel.CONFIRMATION_REQUIRED:
            raise SecurityViolation("Confirmation is valid only for Level 2 actions")
        token = secrets.token_urlsafe(32)
        self._confirmations[token] = Confirmation(token, action.value, str(resource_id), user, time.time() + ttl_seconds)
        self.metrics.inc("human_confirmations")
        self.logger.write(action=action.value, result="confirmed", success=True, confirmation_required=True, confirmation_user=user)
        return token

    def authorize(self, action: Action, *, resource_id: str = "", confirmation_token: str | None = None,
                  session_id: str | None = None, vin: str | None = None) -> None:
        level = ACTION_LEVEL[action]
        if level == AccessLevel.HUMAN_ONLY:
            self.metrics.inc("blocked_actions")
            self.logger.write(action=action.value, result="blocked", success=False, session_id=session_id, vin=vin, error="human_only_or_beta_block")
            raise SecurityViolation("Action is human-only and blocked")
        if level == AccessLevel.CONFIRMATION_REQUIRED:
            item = self._confirmations.get(confirmation_token or "")
            valid = bool(item and not item.used and item.expires_at >= time.time() and item.action == action.value and item.resource_id == str(resource_id))
            if not valid:
                self.metrics.inc("blocked_actions")
                self.logger.write(action=action.value, result="confirmation_required", success=False, session_id=session_id, vin=vin, confirmation_required=True)
                raise ConfirmationRequired("Valid human confirmation required")
            item.used = True

    def require_robot(self, action: Action, **context: Any) -> None:
        if not self.robot_enabled:
            self.metrics.inc("blocked_actions")
            self.logger.write(action=action.value, result="blocked", success=False, error="robot_disabled", **context)
            raise SecurityViolation("Robot is disabled")
        self.authorize(action, **context)

    def validate_url(self, url: str, service: str | None = None, **context: Any) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        allowed = DEFAULT_ALLOWED_DOMAINS.get(service, set()) if service else set().union(*DEFAULT_ALLOWED_DOMAINS.values())
        if parsed.scheme != "https" or host not in allowed or parsed.username or parsed.password:
            self.metrics.inc("blocked_actions")
            self.metrics.inc("browser_errors")
            self.logger.write(action="navigate", website=host or url, result="blocked", success=False, error="domain_not_allowlisted", **context)
            raise SecurityViolation("Domain is not allowlisted")
        return url

    def external_data(self, content: str, source: str, **context: Any) -> str:
        text = str(content or "")[:20000]
        detected = [p.pattern for p in INJECTION_PATTERNS if p.search(text)]
        if detected:
            self.logger.write(action="prompt_injection", website=source, result="ignored", success=True, details={"patterns": detected}, **context)
        return "<EXTERNAL_PAGE_CONTENT source=" + json.dumps(source) + ">\n" + text + "\n</EXTERNAL_PAGE_CONTENT>"

    def safe_path(self, relative: str | Path) -> Path:
        candidate = (self.data_dir / relative).resolve()
        if candidate != self.data_dir and self.data_dir not in candidate.parents:
            raise SecurityViolation("Path escapes ALBATROS_DATA_DIR")
        return candidate

    @staticmethod
    def verify_meta_signature(body: bytes, signature: str | None, secret: str | None = None) -> bool:
        key = secret or os.getenv("WHATSAPP_APP_SECRET", "")
        if not key or not signature or not signature.startswith("sha256="):
            return False
        expected = hmac_new(key.encode(), body, "sha256").hexdigest()
        return compare_digest(expected, signature[7:])


POLICY = SecurityPolicy()
