"""Web hardening: Secure cookie, security headers, no-store API answers, /healthz, listen options."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shelfsaver.__main__ import server_options
from shelfsaver.config import Config
from shelfsaver.web.app import CONTENT_SECURITY_POLICY, SESSION_COOKIE, create_app
from tests.test_web import PASSWORD, FakeScanner, FakeWorker, make_config
from shelfsaver.db import Database


@pytest.fixture
def parts(tmp_path):
    db = Database(tmp_path / "data" / "shelfsaver.db")
    worker = FakeWorker()
    worker.db = db
    return db, worker, FakeScanner(), make_config(tmp_path)


def make_client(parts, base_url):
    db, worker, scanner, config = parts
    return TestClient(create_app(db, worker, scanner, config), base_url=base_url)


def cookie_header(response):
    return response.headers["set-cookie"].lower()


def test_cookie_is_secure_over_https(parts):
    with make_client(parts, "https://testserver") as c:
        r = c.post("/api/auth/setup", json={"password": PASSWORD})
    assert "secure" in cookie_header(r).split("; ")


def test_cookie_is_not_secure_over_plain_http(parts):
    """A Secure cookie on a plain-HTTP home network would never be stored: nobody could sign in."""
    with make_client(parts, "http://testserver") as c:
        r = c.post("/api/auth/setup", json={"password": PASSWORD})
        assert SESSION_COOKIE in cookie_header(r)
        assert "secure" not in cookie_header(r).split("; ")
        assert c.get("/api/status").status_code == 200  # and the session works


def test_login_cookie_is_secure_over_https_too(parts):
    with make_client(parts, "https://testserver") as c:
        c.post("/api/auth/setup", json={"password": PASSWORD})
        r = c.post("/api/auth/login", json={"password": PASSWORD})
    assert "secure" in cookie_header(r).split("; ")


@pytest.mark.parametrize("path", ["/", "/login", "/static/app.js", "/healthz", "/api/auth/state"])
def test_every_kind_of_response_carries_the_security_headers(parts, path):
    with make_client(parts, "http://testserver") as c:
        r = c.get(path)
    assert r.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_errors_and_the_mcp_endpoint_carry_them_too(parts):
    with make_client(parts, "http://testserver") as c:
        for r in (c.get("/api/status"), c.get("/mcp"), c.get("/no-such-page")):
            assert r.headers["x-content-type-options"] == "nosniff", r.request.url


def test_policy_blocks_scripts_and_framing_but_allows_what_the_pages_use():
    policy = dict(part.strip().split(" ", 1) for part in CONTENT_SECURITY_POLICY.split(";"))
    assert policy["script-src"] == "'self'"            # no inline script, no outside script
    assert policy["default-src"] == "'none'"           # anything not listed is refused
    assert policy["frame-ancestors"] == "'none'"       # nobody may embed the page
    assert policy["connect-src"] == "'self'"           # the page only talks to its own API
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY


def test_api_answers_are_not_cached_but_pages_still_can_be(parts):
    """/api/settings holds the MCP token, so no browser or proxy may keep a copy."""
    with make_client(parts, "http://testserver") as c:
        c.post("/api/auth/setup", json={"password": PASSWORD})
        assert c.get("/api/settings").headers["cache-control"] == "no-store"
        assert c.get("/api/auth/state").headers["cache-control"] == "no-store"
        assert c.get("/").headers["cache-control"] == "no-cache"   # the pages' own rule is kept


def test_healthz_needs_no_sign_in_and_reveals_nothing(parts):
    with make_client(parts, "http://testserver") as c:
        r = c.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_healthz_reports_503_when_the_database_cannot_be_read(parts):
    db = parts[0]

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    db.get_setting = broken
    with make_client(parts, "http://testserver") as c:
        r = c.get("/healthz")
    assert r.status_code == 503 and r.json() == {"ok": False}
    assert "locked" not in r.text   # the reason goes to the log, not to an anonymous caller


def test_server_listens_on_every_network_and_believes_no_forwarded_headers_by_default(tmp_path):
    options = server_options(Config(data_dir=tmp_path))
    assert options["host"] == "0.0.0.0"
    assert options["proxy_headers"] is False


def test_server_options_pass_the_chosen_host_and_trusted_proxies(tmp_path):
    options = server_options(Config(data_dir=tmp_path, host="127.0.0.1", port=9000,
                                    trusted_proxies="172.17.0.1,10.0.0.0/8"))
    assert options["host"] == "127.0.0.1" and options["port"] == 9000
    assert options["proxy_headers"] is True
    assert options["forwarded_allow_ips"] == "172.17.0.1,10.0.0.0/8"


# ---- the proxy claim, against a real server (TestClient skips uvicorn's header handling)

def _serve(parts, tmp_path, **config_changes):
    """Run the real uvicorn on a free local port; returns (server, thread, base_url)."""
    import socket
    import threading
    import time

    import uvicorn

    db, worker, scanner, config = parts
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config.host, config.port = "127.0.0.1", port
    for name, value in config_changes.items():
        setattr(config, name, value)
    server = uvicorn.Server(uvicorn.Config(create_app(db, worker, scanner, config),
                                           **server_options(config) | {"log_level": "error"}))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    return server, thread, f"http://127.0.0.1:{port}"


def _setup_with_forwarded_https(base):
    import httpx

    return httpx.post(f"{base}/api/auth/setup", json={"password": PASSWORD},
                      headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"})


def test_forwarded_https_is_ignored_from_an_untrusted_sender(parts, tmp_path):
    """Anyone can send the header; believing it blindly would also let a visitor pick the
    address the login rate limit sees."""
    server, thread, base = _serve(parts, tmp_path)
    try:
        r = _setup_with_forwarded_https(base)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert r.status_code == 200
    assert "secure" not in r.headers["set-cookie"].lower().split("; ")


def test_forwarded_https_is_believed_from_a_listed_proxy(parts, tmp_path):
    server, thread, base = _serve(parts, tmp_path, trusted_proxies="127.0.0.1")
    try:
        r = _setup_with_forwarded_https(base)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert r.status_code == 200
    assert "secure" in r.headers["set-cookie"].lower().split("; ")


# ---- requests that a browser says came from another page

def _signed_in(parts):
    c = make_client(parts, "http://testserver")
    assert c.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
    return c


@pytest.mark.parametrize("path", ["/api/pause", "/api/resume", "/api/auth/logout"])
def test_a_bodyless_change_from_another_site_is_refused(parts, path):
    """The pause/resume/logout endpoints read no body, so nothing else stops a forged form post
    from another port on the same machine (which the browser treats as the same site)."""
    with _signed_in(parts) as c:
        r = c.post(path, headers={"Sec-Fetch-Site": "same-site"})
        assert r.status_code == 403
        assert c.get("/api/status").status_code == 200       # and it did not sign us out
    assert r.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_pause_really_did_not_run_when_refused(parts):
    with _signed_in(parts) as c:
        c.post("/api/pause", headers={"Sec-Fetch-Site": "cross-site"})
        assert c.get("/api/status").json()["worker"]["state"] != "paused"


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_our_own_pages_still_work(parts, site):
    with _signed_in(parts) as c:
        assert c.post("/api/pause", headers={"Sec-Fetch-Site": site}).status_code == 200


def test_an_older_browser_is_judged_by_its_origin_header(parts):
    with _signed_in(parts) as c:
        same = c.post("/api/pause", headers={"Origin": "http://testserver"})
        other = c.post("/api/resume", headers={"Origin": "http://testserver:9999"})
        hidden = c.post("/api/resume", headers={"Origin": "null"})
    assert same.status_code == 200
    assert other.status_code == 403
    assert hidden.status_code == 403


def test_a_script_with_no_browser_headers_is_not_affected(parts):
    with _signed_in(parts) as c:
        assert c.post("/api/pause").status_code == 200


def test_reading_is_never_refused(parts):
    with _signed_in(parts) as c:
        assert c.get("/api/status", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
