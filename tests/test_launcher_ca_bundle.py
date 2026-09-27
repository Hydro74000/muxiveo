"""Tests du choix du bundle CA en build figé (launcher._find_ca_bundle)."""

from __future__ import annotations

import builtins
import sys

import launcher


def test_falls_back_to_system_bundle_without_certifi(monkeypatch):
    real_import = builtins.__import__

    def _no_certifi(name, *args, **kwargs):
        if name == "certifi":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "certifi", raising=False)
    monkeypatch.setattr(builtins, "__import__", _no_certifi)
    monkeypatch.setattr(launcher.os.path, "isfile", lambda p: p == "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem")
    assert launcher._find_ca_bundle() == "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"


def test_ensure_sets_ssl_cert_file_when_frozen(monkeypatch):
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.setattr(launcher, "_find_ca_bundle", lambda: "/etc/ssl/cert.pem")
    launcher._ensure_ssl_ca_bundle()
    assert launcher.os.environ["SSL_CERT_FILE"] == "/etc/ssl/cert.pem"
