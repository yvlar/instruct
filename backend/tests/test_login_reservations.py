"""Successful logins release their own slots without erasing other attempts."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import security
from app.config import Settings
from app.security import SecurityStore, token_hash
from test_security import secured  # noqa: F401

PASSWORD = "Synthetic-password-123"


@pytest.fixture
def store(tmp_path):
    config = Settings(_env_file=None, state_path=str(tmp_path), login_attempts=5)
    result = SecurityStore(config)
    result.create_user("alice", PASSWORD, "admin", bootstrap=True)
    return result


def counts(store):
    with store.connect() as db:
        return {
            row["key"]: row["count"]
            for row in db.execute("SELECT key, count FROM attempts")
        }


def rejected(store, name="alice", password="wrong", ip="127.0.0.1", status=401):
    with pytest.raises(HTTPException) as exc:
        store.login(name, password, ip)
    assert exc.value.status_code == status


def test_repeated_successful_http_logins_release_both_counters(secured):  # noqa: F811
    for _ in range(secured.config.login_attempts + 1):
        client = secured.login("alice")
        assert client.get("/api/documents").status_code == 200
    assert all(value == 0 for value in counts(secured.store).values())


@pytest.mark.parametrize("scope", ["name", "ip"])
def test_success_preserves_failures_and_each_limit(store, scope):
    limit = store.config.login_attempts * (10 if scope == "ip" else 1)
    for index in range(limit - 1):
        rejected(store, name=f"unknown{index}" if scope == "ip" else "alice")
    token = store.login("alice", PASSWORD, "127.0.0.1")
    assert store.authenticate(token)["username"] == "alice"
    key = token_hash("ip:127.0.0.1" if scope == "ip" else "name:alice")
    assert counts(store)[key] == limit - 1
    rejected(store)
    rejected(store, password=PASSWORD, status=429)


def gated_verification(monkeypatch, password=PASSWORD):
    entered, release = Event(), Event()
    original = security.PASSWORDS.verify

    def verify(hashed, supplied):
        if supplied == password:
            entered.set()
            assert release.wait(10), "verification was never released"
        return original(hashed, supplied)

    monkeypatch.setattr(
        security,
        "PASSWORDS",
        SimpleNamespace(
            verify=verify,
            check_needs_rehash=security.PASSWORDS.check_needs_rehash,
            hash=security.PASSWORDS.hash,
        ),
    )
    return entered, release


def test_success_does_not_erase_a_concurrent_failure(store, monkeypatch):
    entered, release = gated_verification(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.login, "alice", PASSWORD, "127.0.0.1")
        try:
            assert entered.wait(10)
            rejected(store)
            assert set(counts(store).values()) == {2}
        finally:
            release.set()
        assert store.authenticate(future.result(timeout=10))["username"] == "alice"
    assert set(counts(store).values()) == {1}
    for _ in range(store.config.login_attempts - 1):
        rejected(store)
    rejected(store, password=PASSWORD, status=429)


def test_concurrent_reservations_block_before_argon2(store, monkeypatch):
    store.config.login_attempts = 1
    entered, release = gated_verification(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.login, "alice", PASSWORD, "127.0.0.1")
        try:
            assert entered.wait(10)
            # Even a valid password cannot enter Argon2 while the slot is occupied.
            rejected(store, password=PASSWORD, status=429)
        finally:
            release.set()
        assert store.authenticate(future.result(timeout=10))["username"] == "alice"
    # The rejected attempt still counts; the successful one alone is released.
    assert set(counts(store).values()) == {1}


def test_old_success_cannot_release_a_replacement_window(store, monkeypatch):
    clock = [10000.0]
    monkeypatch.setattr(security, "time", SimpleNamespace(time=lambda: clock[0]))
    entered, release = gated_verification(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.login, "alice", PASSWORD, "127.0.0.1")
        try:
            assert entered.wait(10)
            clock[0] += 901
            rejected(store)
            assert set(counts(store).values()) == {1}
        finally:
            release.set()
        assert store.authenticate(future.result(timeout=10))["username"] == "alice"
    assert set(counts(store).values()) == {1}
