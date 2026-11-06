"""Task 2 grader: registration, login, password hashing, JWT.

Owned files: app/security.py, app/services/auth_service.py, app/routers/auth.py
"""

from __future__ import annotations

import base64
import inspect
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from helpers import PASSWORD, assert_error_shape, register_and_login, unique_email

KNOWN_KDFS = ("argon2", "bcrypt", "$2a$", "$2b$", "$2y$", "scrypt", "pbkdf2")


def _token(secret, sub=None, exp_delta=timedelta(minutes=5), alg="HS256", with_exp=True, extra=None):
    now = datetime.now(timezone.utc)
    payload = {"sub": sub or str(uuid.uuid4()), "iat": now}
    if with_exp:
        payload["exp"] = now + exp_delta
    payload.update(extra or {})
    return jwt.encode(payload, secret, algorithm=alg)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ------------------------------- functional --------------------------------
@pytest.mark.functional
def test_register_returns_201_with_public_fields(client):
    email = unique_email()
    r = client.post("/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == email and body["role"] == "user"
    uuid.UUID(body["id"])
    assert "password" not in body and "password_hash" not in body


@pytest.mark.functional
def test_login_returns_bearer_token(client, params):
    u = register_and_login(client)
    r = client.post("/login", json={"email": u["email"], "password": u["password"]})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and body["access_token"].count(".") == 2
    assert body["expires_in"] == params["token_ttl_minutes"] * 60


@pytest.mark.functional
def test_token_authenticates_a_protected_route(client):
    u = register_and_login(client)
    assert client.post("/projects", json={"name": "P"}, headers=u["headers"]).status_code == 201


@pytest.mark.functional
def test_duplicate_email_returns_409(client):
    u = register_and_login(client)
    r = client.post("/register", json={"email": u["email"], "password": PASSWORD})
    assert_error_shape(r, 409)


@pytest.mark.functional
def test_email_is_case_insensitive(client):
    email = unique_email("MiXeD")
    assert client.post("/register", json={"email": email, "password": PASSWORD}).status_code == 201
    assert client.post("/login", json={"email": email.lower(), "password": PASSWORD}).status_code == 200
    assert client.post("/login", json={"email": email.upper(), "password": PASSWORD}).status_code == 200
    assert client.post("/register", json={"email": email.upper(), "password": PASSWORD}).status_code == 409


@pytest.mark.functional
@pytest.mark.parametrize("which", ["unknown_email", "wrong_password"])
def test_invalid_credentials_return_401(client, which):
    u = register_and_login(client)
    creds = ({"email": unique_email(), "password": PASSWORD} if which == "unknown_email"
             else {"email": u["email"], "password": PASSWORD + "x"})
    assert_error_shape(client.post("/login", json=creds), 401)


@pytest.mark.functional
@pytest.mark.hidden
def test_token_claims(client, jwt_secret, params):
    u = register_and_login(client)
    claims = jwt.decode(u["token"], jwt_secret, algorithms=["HS256"])
    assert claims["sub"] == u["id"]
    assert claims["exp"] - claims["iat"] == params["token_ttl_minutes"] * 60


@pytest.mark.functional
@pytest.mark.hidden
def test_two_logins_give_distinct_tokens_for_same_user(client):
    u = register_and_login(client)
    again = client.post("/login", json={"email": u["email"], "password": u["password"]}).json()
    assert again["access_token"] != u["token"]


@pytest.mark.functional
@pytest.mark.hidden
def test_token_of_deleted_user_is_rejected(client, db):
    u = register_and_login(client)
    db.execute("DELETE FROM users WHERE id = %s", (u["id"],))
    assert client.post("/projects", json={"name": "x"}, headers=u["headers"]).status_code == 401


# -------------------------------- database ---------------------------------
@pytest.mark.database
def test_registered_user_persisted_with_role_user(client, db):
    u = register_and_login(client)
    row = db.execute("SELECT email, role FROM users WHERE id = %s", (u["id"],)).fetchone()
    assert row == {"email": u["email"], "role": "user"}


@pytest.mark.database
def test_duplicate_registration_creates_exactly_one_row(client, db):
    email = unique_email()
    for _ in range(3):
        client.post("/register", json={"email": email, "password": PASSWORD})
    assert db.execute("SELECT count(*) AS n FROM users WHERE email = %s", (email,)).fetchone()["n"] == 1


@pytest.mark.database
@pytest.mark.hidden
def test_concurrent_duplicate_registration_yields_one_201(migrated_url):
    from fastapi.testclient import TestClient

    from app.main import create_app

    email, codes = unique_email(), []

    def go():
        codes.append(TestClient(create_app()).post(
            "/register", json={"email": email, "password": PASSWORD}).status_code)

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [201] + [409] * 5


@pytest.mark.database
def test_failed_login_does_not_change_user_rows(client, db):
    u = register_and_login(client)
    before = db.execute("SELECT * FROM users WHERE id = %s", (u["id"],)).fetchone()
    client.post("/login", json={"email": u["email"], "password": "wrong-password-123"})
    assert db.execute("SELECT * FROM users WHERE id = %s", (u["id"],)).fetchone() == before


# -------------------------------- security ---------------------------------
@pytest.mark.security
def test_password_is_stored_as_a_salted_kdf_hash(client, db):
    a, b = register_and_login(client), register_and_login(client)
    ha = db.execute("SELECT password_hash FROM users WHERE id = %s", (a["id"],)).fetchone()["password_hash"]
    hb = db.execute("SELECT password_hash FROM users WHERE id = %s", (b["id"],)).fetchone()["password_hash"]
    assert PASSWORD not in ha and ha != hb
    assert any(k in ha.lower() for k in KNOWN_KDFS)


@pytest.mark.security
def test_hash_and_verify_helpers():
    from app.security import hash_password, verify_password

    h = hash_password("s3cret-Passw0rd")
    assert h != "s3cret-Passw0rd" and verify_password("s3cret-Passw0rd", h)
    assert not verify_password("other", h)


@pytest.mark.security
@pytest.mark.hidden
def test_verify_password_never_raises_on_garbage_hash():
    from app.security import verify_password

    for garbage in ("", "not-a-hash", "$argon2id$broken", "x" * 500):
        assert verify_password("anything", garbage) is False


@pytest.mark.security
def test_login_does_not_reveal_whether_email_exists(client):
    u = register_and_login(client)
    a = client.post("/login", json={"email": u["email"], "password": "wrong-password-123"})
    b = client.post("/login", json={"email": unique_email(), "password": "wrong-password-123"})
    assert (a.status_code, a.json()) == (b.status_code, b.json())


@pytest.mark.security
@pytest.mark.parametrize("make", ["tampered", "wrong_secret", "expired", "no_exp", "alg_none",
                                  "hs512", "garbage", "unknown_user", "non_uuid_sub"])
def test_invalid_tokens_are_rejected(client, jwt_secret, make):
    u = register_and_login(client)
    if make == "tampered":
        head, body, sig = u["token"].split(".")
        flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
        token = ".".join([head, body, flipped])
    elif make == "wrong_secret":
        token = _token("another-secret-" + "z" * 40, sub=u["id"])
    elif make == "expired":
        token = _token(jwt_secret, sub=u["id"], exp_delta=timedelta(seconds=-30))
    elif make == "no_exp":
        token = _token(jwt_secret, sub=u["id"], with_exp=False)
    elif make == "alg_none":
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        token = f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc({'sub': u['id'], 'exp': 9999999999})}."
    elif make == "hs512":
        token = _token(jwt_secret, sub=u["id"], alg="HS512")
    elif make == "garbage":
        token = "abc.def.ghi"
    elif make == "unknown_user":
        token = _token(jwt_secret, sub=str(uuid.uuid4()))
    else:
        token = _token(jwt_secret, sub="not-a-uuid")
    r = client.post("/projects", json={"name": "x"}, headers=_bearer(token))
    assert_error_shape(r, 401)


@pytest.mark.security
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic dXNlcjpwYXNz"},
                                     {"Authorization": "Bearer"}, {"Authorization": "Bearer "}])
def test_missing_or_wrong_scheme_is_401(client, headers):
    assert client.post("/projects", json={"name": "x"}, headers=headers).status_code == 401


@pytest.mark.security
@pytest.mark.hidden
def test_decode_access_token_rejects_bad_tokens_with_401(jwt_secret):
    from app.errors import AppError
    from app.security import decode_access_token

    with pytest.raises(AppError) as exc:
        decode_access_token(_token(jwt_secret, exp_delta=timedelta(seconds=-5)))
    assert exc.value.status_code == 401


# --------------------------------- quality ---------------------------------
@pytest.mark.quality
def test_auth_errors_use_error_shape_and_challenge_header(client):
    r = client.post("/login", json={"email": unique_email(), "password": PASSWORD})
    assert_error_shape(r, 401)
    assert r.headers.get("www-authenticate", "").lower().startswith("bearer")
    assert_error_shape(client.post("/projects", json={"name": "x"}), 401)


@pytest.mark.quality
def test_openapi_declares_auth_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/register"] and "post" in paths["/login"]


@pytest.mark.quality
def test_security_module_contract():
    from app import security

    for name, params in {"hash_password": ["plain"], "verify_password": ["plain", "hashed"],
                         "create_access_token": ["user_id"], "decode_access_token": ["token"]}.items():
        sig = list(inspect.signature(getattr(security, name)).parameters)
        assert sig[: len(params)] == params, name


@pytest.mark.quality
def test_login_response_shape_is_exact(client):
    u = register_and_login(client)
    body = client.post("/login", json={"email": u["email"], "password": u["password"]}).json()
    assert set(body) == {"access_token", "token_type", "expires_in"}


@pytest.mark.quality
def test_service_module_contract():
    from app.services import auth_service

    assert list(inspect.signature(auth_service.register_user).parameters)[:3] == ["conn", "email", "password"]
    assert list(inspect.signature(auth_service.authenticate).parameters)[:3] == ["conn", "email", "password"]
