"""HTTP GET for research. Connect to the checked address and keep TLS verification for the host."""
from __future__ import annotations

import http.client
import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit


def parts(url: str):
    u = urlsplit(url)
    if (u.scheme not in ("http", "https") or not u.hostname or u.username is not None
            or any(c in url for c in '\\<>"') or any(c.isspace() or ord(c) < 32 for c in url)):
        raise ValueError("only public http(s) URLs without credentials")
    if len(url) > 4096:
        raise ValueError("URL is too long")
    return u


def origin(u) -> tuple:
    return u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80)


def _addresses(host: str, port: int, local: bool) -> list[str]:
    try:
        addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except socket.gaierror as e:
        raise ValueError(f"could not resolve {host}") from e
    for value in addresses:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ((local and ip.is_loopback) or (ip.is_global and not ip.is_multicast and not ip.is_reserved)):
            raise ValueError(f"{host} is a local/private address; research tools only reach the public web")
    if not addresses:
        raise ValueError(f"no address for {host}")
    return addresses


def remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("research time limit reached")
    return left


def get(url: str, *, check, deadline: float, limit: int, trusted_search: bool = False,
        public_origin: tuple | None = None) -> tuple[bytes, str, str, int]:
    for hop in range(5):
        u = parts(url)
        scheme, host, port = origin(u)
        if public_origin is not None and origin(u) != public_origin:
            raise ValueError("browser resources must stay on the original origin")
        check(host)
        local = trusted_search and hop == 0
        if not local and port not in (80, 443):
            raise ValueError("only public web ports 80 and 443 are allowed")
        addresses = _addresses(host, port, local)
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        conn = cls(host, port, timeout=remaining(deadline))
        # The connection must never resolve the original hostname a second time.
        def connect(_address, timeout, source_address):
            error = None
            for ip in addresses:
                try:
                    return socket.create_connection((ip, port), min(timeout, remaining(deadline)), source_address)
                except OSError as e:
                    error = e
            raise error
        conn._create_connection = connect
        try:
            conn.request("GET", urlunsplit(("", "", u.path or "/", u.query, "")), headers={
                "User-Agent": "Mozilla/5.0 (compatible; SHAMS-research/2.2)",
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            sock = conn.sock
            sock.settimeout(remaining(deadline))
            # Keep ownership of the socket until the bounded read finishes. getresponse()
            # closes the connection socket early when the server sends Connection: close.
            with http.client.HTTPResponse(sock, method="GET") as response:
                response.begin()
                location = response.getheader("Location")
                if response.status in (301, 302, 303, 307, 308) and location:
                    url = urljoin(url, location)
                    continue
                if response.status >= 400:
                    raise OSError(f"HTTP {response.status} from {host}")
                if response.getheader("Content-Encoding", "identity").lower() not in ("identity", ""):
                    raise ValueError("unsupported HTTP content encoding")
                data = bytearray()
                while len(data) < limit:
                    sock.settimeout(remaining(deadline))
                    chunk = response.read1(min(16384, limit - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                return bytes(data), response.getheader("Content-Type", ""), url, response.status
        finally:
            conn.close()
    raise ValueError("too many redirects")
