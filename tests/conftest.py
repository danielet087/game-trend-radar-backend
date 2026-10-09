"""Offline contracts require injected sources instead of live network access."""

import ipaddress
import socket
from urllib.parse import urlsplit

import pytest
import requests


@pytest.fixture(autouse=True)
def require_offline_sources(monkeypatch):
    attempts = []

    def local(host):
        if host in (None, "", "localhost"):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def deny():
        attempts.append(True)
        raise AssertionError("Offline contracts must inject their external source")

    original_request = requests.sessions.Session.request
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def request(session, method, url, *args, **kwargs):
        if not local(urlsplit(url).hostname):
            deny()
        return original_request(session, method, url, *args, **kwargs)

    def connect(sock, address):
        if isinstance(address, tuple) and not local(address[0]):
            deny()
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if isinstance(address, tuple) and not local(address[0]):
            deny()
        return original_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if not local(host):
            deny()
        return original_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(requests.sessions.Session, "request", request)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
    # A transport may catch the initial error. That must still fail its test.
    assert not attempts, "An offline contract attempted an external request"
