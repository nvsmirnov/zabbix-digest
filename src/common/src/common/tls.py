"""TLS settings shared by all outgoing connections (Zabbix API, SMTP, Core <-> web).

Trusted CA certificates, in this order:
  - ca_file of the connection settings (zabbix.ca_file, smtp.ca_file);
  - SSL_CERT_FILE and/or SSL_CERT_DIR from the environment;
  - the certifi bundle.
"""

import os
import ssl

import certifi


def trust_source(ca_file: str | None = None) -> str:
    """Description for the startup log."""
    if ca_file:
        return f"ca_file {ca_file}"
    env = [f"{name}={os.environ[name]}" for name in ("SSL_CERT_FILE", "SSL_CERT_DIR") if os.environ.get(name)]
    return ", ".join(env) if env else "certifi bundle"


def ssl_context(verify: bool = True, ca_file: str | None = None) -> ssl.SSLContext:
    """Client SSL context. Raises ValueError when a configured CA file or directory is missing."""
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if ca_file:
        cafile, capath, source = ca_file, None, "ca_file"
    else:
        cafile = os.environ.get("SSL_CERT_FILE") or None
        capath = os.environ.get("SSL_CERT_DIR") or None
        source = "SSL_CERT_FILE/SSL_CERT_DIR"
        if not cafile and not capath:
            cafile, source = certifi.where(), "certifi"
    if cafile and not os.path.isfile(cafile):
        raise ValueError(f"{source}: CA file not found: {cafile}")
    if capath and not os.path.isdir(capath):
        raise ValueError(f"{source}: CA directory not found: {capath}")
    try:
        return ssl.create_default_context(cafile=cafile, capath=capath)
    except ssl.SSLError as exc:
        raise ValueError(f"{source}: cannot load CA certificates from {cafile or capath}: {exc}") from None
