import hashlib
import json
import logging
import re
import secrets
from http.cookies import SimpleCookie
from typing import Literal
from urllib.parse import unquote, urlsplit

from authlib.integrations.starlette_client import OAuth
from authlib.integrations.starlette_client.apps import StarletteOAuth2App
from authlib.oidc.core import CodeIDToken
from fastapi import HTTPException, Request
from redis.asyncio import Redis
from starlette.responses import JSONResponse, RedirectResponse

from app.config import Settings
from app.errors import ConfigurationError
from app.models import User
from app.observability import error_fields, log_event, traced
from app.profile import linkedin_profile


@traced("auth.hr.password")
def password_hr_user(settings: Settings, username: str, password: str) -> User:
    if not settings.hr_username or not settings.hr_password:
        log_event("auth.hr.password", "rejected", reason="credentials_not_configured")
        raise ConfigurationError("HR login is not configured. Set HR_USERNAME and HR_PASSWORD in backend/.env.")
    # Fixed-size comparisons support Unicode and avoid credential-length timing.
    username_matches = secrets.compare_digest(hashlib.sha256(username.encode("utf-8", errors="surrogatepass")).digest(),
                                              hashlib.sha256(settings.hr_username.encode("utf-8", errors="surrogatepass")).digest())
    password_matches = secrets.compare_digest(hashlib.sha256(password.encode("utf-8", errors="surrogatepass")).digest(),
                                              hashlib.sha256(settings.hr_password.encode("utf-8", errors="surrogatepass")).digest())
    if not (username_matches & password_matches):
        log_event("auth.hr.password", "rejected", reason="invalid_credentials", status_code=401)
        raise HTTPException(401, "Invalid username or password")
    log_event("auth.hr.password", "validated", authenticated=True, role="hr")
    return User(sub="environment-hr-admin", issuer="urn:talentmatch:hr:environment", role="hr",
                name="HR Administrator", email=settings.hr_username)


@traced("auth.oidc.return_validation")
def oauth_return_path(value: str | None, provider: str) -> str:
    """Only portal routes for the signing-in role can be OAuth destinations."""
    default = "/candidate/applications" if provider == "linkedin" else "/hr"
    if value is None:
        return default
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise HTTPException(400, "Invalid sign-in return destination")
    decoded = value
    for _ in range(6):
        if "\\" in decoded or not decoded.isprintable():
            raise HTTPException(400, "Invalid sign-in return destination")
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    else:
        raise HTTPException(400, "Invalid sign-in return destination")
    try:
        parts = urlsplit(value)
        decoded_path = unquote(parts.path)
        for _ in range(6):
            expanded = unquote(decoded_path)
            if expanded == decoded_path:
                break
            decoded_path = expanded
        if (
            parts.scheme
            or parts.netloc
            or not parts.path.startswith("/")
            or decoded_path.startswith("//")
            or "\\" in decoded_path
            or (any(part in {".", "..", ""} for part in decoded_path[1:].split("/")) and decoded_path != "/")
        ):
            raise ValueError("Unsafe relative path")
        identifier = r"[^/?#]+"
        pattern = (
            rf"/(?:jobs(?:/{identifier}(?:/apply)?)?|candidate/applications(?:/{identifier})?)?"
            if provider == "linkedin"
            else rf"/hr(?:/jobs(?:/{identifier}(?:/candidates(?:/{identifier})?)?)?|/applications/{identifier})?"
        )
        if not re.fullmatch(pattern, decoded_path):
            raise ValueError("Destination is outside this role's portal")
    except ValueError:
        raise HTTPException(400, "Invalid sign-in return destination") from None
    return value


def _return_route_template(value: str) -> str:
    """Describe a validated destination without job IDs, search text or fragments."""
    parts = unquote(urlsplit(value).path).strip("/").split("/")
    if parts[0] == "jobs" and len(parts) > 1:
        parts[1] = "{requirement_id}"
    elif parts[:2] == ["candidate", "applications"] and len(parts) > 2:
        parts[2] = "{application_id}"
    elif parts[:2] == ["hr", "jobs"] and len(parts) > 2:
        parts[2] = "{requirement_id}"
        if len(parts) > 4:
            parts[4] = "{application_id}"
    elif parts[:2] == ["hr", "applications"] and len(parts) > 2:
        parts[2] = "{application_id}"
    return "/" + "/".join(parts)


class LinkedInCodeIDToken(CodeIDToken):
    """LinkedIn may omit nonce in its authorization-code ID tokens."""

    def validate_nonce(self):
        if "nonce" in self:
            super().validate_nonce()


class LinkedInOAuthApp(StarletteOAuth2App):
    async def parse_id_token(self, token, nonce, claims_options=None, claims_cls=None, leeway=120):
        def linkedin_claims(claims, header, options, params):
            # Authlib does not inject access_token when a custom claims class is
            # supplied. Preserve at_hash verification as well as all JWT checks.
            return LinkedInCodeIDToken(claims, header, options, {**params, "access_token": token.get("access_token")})

        return await super().parse_id_token(
            token, nonce, claims_options=claims_options, claims_cls=linkedin_claims, leeway=leeway
        )


class SessionStore:
    """Opaque random session identifiers. No identity or tokens go in cookies."""

    def __init__(self, settings: Settings, redis: Redis | None = None):
        self.settings = settings
        self.redis = redis
        self.memory: dict[str, dict] = {}

    def key(self, sid):
        return f"{self.settings.redis_prefix}:session:{sid}"

    @traced("auth.session.load")
    async def get(self, sid):
        if self.redis:
            value = await self.redis.get(self.key(sid))
            log_event("auth.session.lookup", "completed", storage="redis", found=bool(value))
            return json.loads(value) if value else None
        import time

        item = self.memory.get(sid)
        if item and item["expires"] > time.time():
            log_event("auth.session.lookup", "completed", storage="memory", found=True)
            return item["data"].copy()
        self.memory.pop(sid, None)
        log_event("auth.session.lookup", "completed", storage="memory", found=False)
        return None

    @traced("auth.session.save")
    async def put(self, sid, data, old_sid=None):
        """Atomically update/rotate only a still-valid original session."""
        if self.redis:
            if old_sid:
                script = """
                if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
                if KEYS[1] ~= KEYS[2] then redis.call('DEL', KEYS[1]) end
                redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
                return 1
                """
                saved = bool(
                    await self.redis.eval(
                        script, 2, self.key(old_sid), self.key(sid), json.dumps(data), self.settings.session_ttl_seconds
                    )
                )
                log_event("auth.session.rotation", "completed" if saved else "failed", storage="redis", validated=saved)
                return saved
            await self.redis.set(self.key(sid), json.dumps(data), ex=self.settings.session_ttl_seconds)
            log_event("auth.session.persist", "completed", storage="redis")
            return True
        else:
            import time

            if old_sid and (old_sid not in self.memory or self.memory[old_sid]["expires"] <= time.time()):
                log_event("auth.session.rotation", "failed", storage="memory", reason="original_session_expired")
                return False
            if old_sid and old_sid != sid:
                self.memory.pop(old_sid, None)
            self.memory[sid] = {"data": data.copy(), "expires": time.time() + self.settings.session_ttl_seconds}
            log_event("auth.session.persist", "completed", storage="memory", rotated=bool(old_sid and old_sid != sid))
            return True

    @traced("auth.session.delete")
    async def delete(self, sid):
        if self.redis:
            await self.redis.delete(self.key(sid))
        else:
            self.memory.pop(sid, None)


class OpaqueSessionMiddleware:
    def __init__(self, app, settings: Settings, store: SessionStore):
        self.app, self.settings, self.store = app, settings, store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        cookie = SimpleCookie()
        try:
            cookie.load(dict(scope["headers"]).get(b"cookie", b"").decode())
            morsel = cookie.get(self.settings.session_cookie_name)
            sid = morsel.value if morsel else ""
            if len(sid) != 43:
                sid = ""
            data = await self.store.get(sid) if sid else None
        except Exception as exc:
            log_event("auth.session.load", "failed", level=logging.ERROR, **{**error_fields(exc), "status_code": 503})
            response = JSONResponse({"detail": "Session service unavailable"}, status_code=503)
            return await response(scope, receive, send)
        log_event("auth.session.ready", "completed", authenticated=isinstance(data, dict) and bool(data.get("user")), existing=bool(data))
        scope["session"] = data or {"csrf_token": secrets.token_urlsafe(32)}
        scope.setdefault("state", {})
        current_sid = sid if data else secrets.token_urlsafe(32)
        original = json.dumps(scope["session"], sort_keys=True)

        async def send_with_cookie(message):
            if message["type"] == "http.response.start":
                nonlocal current_sid
                modified = (
                    not data
                    or original != json.dumps(scope["session"], sort_keys=True)
                    or scope["state"].get("rotate_session")
                )
                try:
                    if scope["state"].get("rotate_session"):
                        log_event("auth.session.rotation", "start")
                        current_sid = secrets.token_urlsafe(32)
                    if modified and not await self.store.put(
                        current_sid, scope["session"], old_sid=sid if data else None
                    ):
                        raise RuntimeError("Session was revoked")
                except Exception as exc:
                    log_event("auth.session.persist", "failed", level=logging.ERROR, **{**error_fields(exc), "status_code": 503})
                    message = {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [(b"content-type", b"application/json")],
                    }
                    # Session persistence is a prerequisite for authentication.
                    scope["state"]["session_failed"] = True
                else:
                    response = JSONResponse({})
                    response.set_cookie(
                        self.settings.session_cookie_name,
                        current_sid,
                        max_age=self.settings.session_ttl_seconds,
                        httponly=True,
                        secure=self.settings.app_env == "production",
                        samesite="lax",
                        path="/",
                    )
                    if modified:
                        message["headers"].extend([(b"set-cookie", response.headers["set-cookie"].encode())])
                    message["headers"].extend(
                        [
                            (b"cache-control", b"no-store"),
                            (b"x-content-type-options", b"nosniff"),
                            (b"referrer-policy", b"same-origin"),
                        ]
                    )
            elif message["type"] == "http.response.body" and scope["state"].get("session_failed"):
                message = {
                    "type": "http.response.body",
                    "body": b'{"detail":"Session service unavailable"}',
                    "more_body": False,
                }
            await send(message)

        await self.app(scope, receive, send_with_cookie)


@traced("auth.identity.current")
def current_user(request: Request) -> User:
    value = request.session.get("user")
    if not value:
        log_event("auth.identity.current", "rejected", level=logging.WARNING, reason="sign_in_required", status_code=401)
        raise HTTPException(401, "Sign in to continue")
    return User.model_validate(value)


@traced("auth.access.candidate")
def candidate_user(request: Request) -> User:
    user = current_user(request)
    if user.role != "candidate":
        log_event("auth.access.candidate", "rejected", level=logging.WARNING, reason="candidate_role_required", status_code=403)
        raise HTTPException(403, "Candidate access required")
    return user


@traced("auth.access.hr")
def hr_user(request: Request) -> User:
    user = current_user(request)
    if user.role != "hr":
        log_event("auth.access.hr", "rejected", level=logging.WARNING, reason="hr_role_required", status_code=403)
        raise HTTPException(403, "HR access required")
    return user


@traced("auth.csrf")
def require_csrf(request: Request):
    expected = request.session.get("csrf_token", "")
    actual = request.headers.get("x-csrf-token", "")
    if not actual or not expected or not secrets.compare_digest(actual, expected):
        log_event("auth.csrf.token", "rejected", level=logging.WARNING, reason="csrf_token_invalid", status_code=403)
        raise HTTPException(403, "Invalid CSRF token")
    log_event("auth.csrf.token", "completed", validated=True)
    origin = request.headers.get("origin")
    allowed = request.app.state.settings.cors_origins + [
        request.app.state.settings.frontend_url,
        request.app.state.settings.backend_url,
    ]
    if origin and origin not in allowed:
        log_event("auth.csrf.origin", "rejected", level=logging.WARNING, reason="origin_not_allowed", status_code=403)
        raise HTTPException(403, "Request origin is not allowed")
    log_event("auth.csrf.origin", "completed", validated=True)


class OIDC:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.oauth = OAuth()
        if settings.linkedin_client_id and settings.linkedin_client_secret:
            self.oauth.register(
                "linkedin",
                client_id=settings.linkedin_client_id,
                client_secret=settings.linkedin_client_secret,
                server_metadata_url=settings.linkedin_discovery_url,
                client_cls=LinkedInOAuthApp,
                client_kwargs={"scope": "openid profile email", "token_endpoint_auth_method": "client_secret_post"},
            )
        if settings.entra_tenant_id and settings.entra_client_id and settings.entra_client_secret:
            self.oauth.register(
                "hr",
                client_id=settings.entra_client_id,
                client_secret=settings.entra_client_secret,
                server_metadata_url=f"https://login.microsoftonline.com/{settings.entra_tenant_id}/v2.0/.well-known/openid-configuration",
                client_kwargs={"scope": "openid profile email"},
            )

    def client(self, provider: Literal["linkedin", "hr"]):
        client = self.oauth.create_client(provider)
        if client is None:
            log_event("auth.oidc.configuration", "failed", level=logging.ERROR, provider=provider, reason="provider_not_configured")
            raise ConfigurationError(f"{provider.title()} authentication is not configured")
        log_event("auth.oidc.configuration", "completed", provider=provider)
        return client

    @traced("auth.oidc.login")
    async def login(self, request: Request, provider: Literal["linkedin", "hr"]):
        return_to = oauth_return_path(request.query_params.get("return_to"), provider)
        log_event("auth.oidc.return_destination", "completed", provider=provider, route=_return_route_template(return_to), validated=True)
        client = self.client(provider)
        uri = self.settings.linkedin_redirect_uri if provider == "linkedin" else self.settings.entra_redirect_uri
        # Authlib generates unpredictable state and nonce. The destination is
        # bound to this specific authorization attempt, never a global value.
        log_event("auth.oidc.authorization", "start", provider=provider)
        authorization = await client.create_authorization_url(uri, nonce=secrets.token_urlsafe(32))
        log_event("auth.oidc.authorization", "completed", provider=provider)
        log_event("auth.oidc.state_save", "start", provider=provider)
        await client.save_authorize_data(request, redirect_uri=uri, return_to=return_to, **authorization)
        log_event("auth.oidc.state_save", "completed", provider=provider)
        return RedirectResponse(authorization["url"], status_code=302)

    @traced("auth.oidc.identity")
    def identity(self, claims: dict, provider: str) -> User:
        issuer = (
            self.settings.linkedin_issuer
            if provider == "linkedin"
            else f"https://login.microsoftonline.com/{self.settings.entra_tenant_id}/v2.0"
        )
        if claims.get("iss") != issuer or not claims.get("sub"):
            log_event("auth.oidc.identity_claims", "rejected", level=logging.WARNING, provider=provider, reason="issuer_or_subject_invalid", status_code=401)
            raise HTTPException(401, "Invalid identity claims")
        log_event("auth.oidc.identity_claims", "completed", provider=provider, validated=True)
        if provider == "hr":
            allowed = {*self.settings.hr_allowed_roles, self.settings.entra_hr_role}
            if claims.get("tid") != self.settings.entra_tenant_id or not allowed.intersection(claims.get("roles", [])):
                log_event("auth.oidc.hr_role", "rejected", level=logging.WARNING, provider=provider, reason="tenant_or_hr_role_invalid", status_code=403)
                raise HTTPException(403, "The configured HR application role is required")
            log_event("auth.oidc.hr_role", "completed", provider=provider, validated=True)
        name = claims.get("name")
        email = claims.get("email") or (claims.get("preferred_username") if provider == "hr" else None)
        profile = None
        if provider == "linkedin":
            # LinkedIn profile/contact claims are optional. Authentication is
            # bound to the validated issuer/subject; the application form
            # collects required contact details rather than inventing them.
            profile = linkedin_profile(claims, issuer)
            name = profile.name or ""
            if not name:
                name = " ".join(value for value in (profile.given_name, profile.family_name) if value)
            email = profile.email or ""
        elif not name or not email:
            log_event("auth.oidc.profile", "rejected", level=logging.WARNING, provider=provider, reason="required_contact_missing", status_code=401)
            raise HTTPException(401, "Identity is missing required profile information")
        log_event("auth.oidc.profile", "completed", provider=provider, name_available=bool(name), email_available=bool(email))
        return User(
            sub=claims["sub"],
            name=name,
            email=email,
            issuer=issuer,
            role="candidate" if provider == "linkedin" else "hr",
            linkedin_profile=profile,
        )

    @traced("auth.oidc.callback")
    async def callback(self, request: Request, provider: Literal["linkedin", "hr"]):
        client = self.client(provider)
        issuer = (
            self.settings.linkedin_issuer
            if provider == "linkedin"
            else f"https://login.microsoftonline.com/{self.settings.entra_tenant_id}/v2.0"
        )
        callback_stage = "auth.oidc.state_load"
        try:
            log_event(callback_stage, "start", provider=provider)
            state_data = await client.framework.get_state_data(request.session, request.query_params.get("state"))
            if not state_data or not state_data.get("nonce"):
                raise HTTPException(401, "Authentication state is missing or expired")
            log_event(callback_stage, "completed", provider=provider, validated=True)
            callback_stage = "auth.oidc.return_destination"
            return_to = oauth_return_path(state_data.get("return_to"), provider)
            log_event(callback_stage, "completed", provider=provider, route=_return_route_template(return_to), validated=True)
            callback_stage = "auth.oidc.token_validation"
            log_event(callback_stage, "start", provider=provider)
            token = await client.authorize_access_token(
                request,
                claims_options={
                    "iss": {"essential": True, "value": issuer},
                    "aud": {"essential": True, "value": client.client_id},
                },
            )
            if "id_token" not in token or not token.get("userinfo"):
                raise HTTPException(401, "OIDC identity token is required")
            log_event(callback_stage, "completed", provider=provider, validated=True)
            claims = dict(token["userinfo"])
            callback_stage = "auth.oidc.nonce_validation"
            # LinkedIn omits nonce in some code-flow tokens. When present it
            # must still match, even if a token claims nonce_supported=false.
            if (provider != "linkedin" or "nonce" in claims) and not secrets.compare_digest(
                str(claims.get("nonce", "")), state_data["nonce"]
            ):
                raise HTTPException(401, "Invalid identity nonce")
            log_event(callback_stage, "completed", provider=provider, validated=True, nonce_present="nonce" in claims)
            if provider == "linkedin":
                callback_stage = "auth.oidc.userinfo"
                log_event(callback_stage, "start", provider=provider)
                profile = await client.userinfo(token=token)
                if not isinstance(profile.get("sub"), str) or not profile["sub"] or profile["sub"] != claims.get("sub"):
                    raise HTTPException(401, "LinkedIn profile does not match the identity token")
                log_event(callback_stage, "completed", provider=provider, validated=True)
                if profile.get("email") is not None and profile["email"] != claims.get("email"):
                    # Verification refers to a particular mailbox. A token's
                    # flag cannot verify a different userinfo contact address.
                    claims.pop("email_verified", None)
                # The profile response cannot replace validated token claims.
                claims.update(
                    {
                        field: profile[field]
                        for field in ("name", "given_name", "family_name", "picture", "locale", "email", "email_verified")
                        if profile.get(field) is not None
                    }
                )
            callback_stage = "auth.oidc.identity"
            user = self.identity(claims, provider)
        except HTTPException as exc:
            log_event(callback_stage, "failed", level=logging.WARNING, provider=provider, **error_fields(exc))
            raise
        except Exception as exc:
            oauth_error = getattr(exc, "error", "")
            if not isinstance(oauth_error, str) or oauth_error not in {
                "invalid_client", "invalid_grant", "invalid_scope", "unauthorized_client",
                "access_denied", "invalid_request",
            }:
                oauth_error = "unclassified"
            claim = getattr(exc, "claim_name", getattr(exc, "claim", ""))
            if not isinstance(claim, str) or claim not in {
                "iss", "aud", "exp", "iat", "nbf", "nonce", "azp", "at_hash", "sub",
            }:
                claim = "-"
            log_event(callback_stage, "failed", level=logging.WARNING, provider=provider, oauth_error=oauth_error, claim=claim, **error_fields(exc))
            logging.getLogger("uvicorn.error").warning(
                "OIDC callback failure: provider=%s type=%s error=%s claim=%s",
                provider, type(exc).__name__, oauth_error, claim,
            )
            raise HTTPException(401, "Authentication callback could not be validated") from None
        request.session.clear()
        request.session.update(user=user.model_dump(), csrf_token=secrets.token_urlsafe(32))
        request.state.rotate_session = True
        request.state.oauth_return_to = return_to
        log_event("auth.oidc.session_rotation", "requested", provider=provider)
        log_event("auth.oidc.return_destination", "ready", provider=provider, route=_return_route_template(return_to))
        return user
