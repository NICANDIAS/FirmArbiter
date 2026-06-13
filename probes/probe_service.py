"""
probe_service.py
----------------
Checks whether a network service reported by a candidate tool is genuinely
the firmware's service, not something running on the host machine.

This distinction matters because tools like FirmAE report a service as
reachable by checking whether an HTTP connection succeeds — but if the
emulated firmware fails to start its web server, the connection may land
on the host's own HTTP server (Apache2, nginx, or similar) instead. From
the tool's perspective, the check passes. In reality, nothing from the
firmware was reached.

This probe catches that by fetching the response body and checking it
against a list of known host-default-page signatures. If any signature
matches, the service is flagged as a false positive regardless of whether
the TCP connection succeeded.

Usage (standalone, for testing):

    python probes/probe_service.py --ip 192.168.0.1 --port 80

Usage (called from run_veritas.py):

    from probes.probe_service import probe_service
    result = probe_service(reported_ip="192.168.0.1", reported_port=80)
"""

import hashlib
import socket
import time
import argparse
import json

# requests is the clearest way to make HTTP calls with fine-grained
# control over timeouts and error handling
import requests
from requests.exceptions import ConnectionError, Timeout, RequestException


# These are response body fragments that indicate the host machine's
# default HTTP server answered instead of the firmware. Each entry is
# a tuple of (signature_id, fragment_to_search_for). The fragment
# search is case-insensitive and looks at the first 4KB of the body,
# which is enough to catch any default page header without pulling
# large responses into memory.
#
# Add to this list whenever you encounter a new false positive during
# corpus testing. The signature_id goes into the result JSON so you
# know exactly which pattern triggered.
HOST_DEFAULT_PAGE_SIGNATURES = [
    ("apache2_ubuntu_default",   "Apache2 Ubuntu Default Page"),
    ("apache2_it_works",         "It works!"),
    ("nginx_welcome",            "Welcome to nginx"),
    ("nginx_default_fedora",     "Fedora"),
    ("lighttpd_placeholder",     "Placeholder page"),
    ("iis_default",              "Internet Information Services"),
    ("caddy_default",            "Caddy"),
]

# How long to wait for a TCP connection before giving up.
# 10 seconds is generous for a local emulated service.
TCP_CONNECT_TIMEOUT = 10

# How long to wait for the full HTTP response.
# 15 seconds covers slow-starting firmware web servers.
HTTP_RESPONSE_TIMEOUT = 15

# How many bytes of the response body to read for signature matching.
# 4096 is enough to catch any default page title or header.
BODY_SAMPLE_BYTES = 4096


def _tcp_connect(ip: str, port: int) -> bool:
    """
    Try a raw TCP connection to ip:port.

    We do this separately from the HTTP request so we can distinguish
    between 'nothing is listening' (TCP refused) and 'something answered
    but it was wrong' (HTTP false positive). That distinction is useful
    when writing up results.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(TCP_CONNECT_TIMEOUT)
    try:
        sock.connect((ip, port))
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False
    finally:
        sock.close()


def _fetch_http(ip: str, port: int) -> dict:
    """
    Make an HTTP GET to http://ip:port/ and return what we found.

    Returns a dict with keys:
        status_code     int or None
        body_sample     str (first BODY_SAMPLE_BYTES, decoded loosely)
        body_size       int (full Content-Length if available, else len of body)
        body_sha256     str (SHA256 of the complete response body)
        error           str or None
    """
    url = f"http://{ip}:{port}/"
    result = {
        "status_code": None,
        "body_sample": "",
        "body_size": 0,
        "body_sha256": None,
        "error": None,
    }

    try:
        # stream=True lets us read the body in chunks, which means we
        # can hash the full body without loading it all into memory at once
        response = requests.get(
            url,
            timeout=HTTP_RESPONSE_TIMEOUT,
            stream=True,
            # Some firmware web servers use self-signed certs on port 443;
            # for port 80 this does nothing, but it keeps the call safe
            # if someone points it at 443
            verify=False,
            # A generic browser-like user agent avoids some firmware
            # servers that return 403 for unknown agents
            headers={"User-Agent": "Mozilla/5.0 (compatible; VERITAS/1.0)"},
            # Do not follow redirects automatically — we want to see the
            # raw response from the reported address, not where it points
            allow_redirects=False,
        )
        result["status_code"] = response.status_code

        # Read and hash the body in chunks
        body_chunks = []
        hasher = hashlib.sha256()
        total_bytes = 0
        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                hasher.update(chunk)
                total_bytes += len(chunk)
                # Only keep the first BODY_SAMPLE_BYTES for signature checking
                if total_bytes <= BODY_SAMPLE_BYTES:
                    body_chunks.append(chunk)

        result["body_size"] = total_bytes
        result["body_sha256"] = hasher.hexdigest()
        # Decode loosely — firmware pages may not be clean UTF-8
        result["body_sample"] = b"".join(body_chunks).decode("utf-8", errors="replace")

    except Timeout:
        result["error"] = "http_timeout"
    except ConnectionError:
        result["error"] = "http_connection_error"
    except RequestException as exc:
        result["error"] = f"http_request_error: {exc}"

    return result


def _check_authenticity(body_sample: str) -> tuple:
    """
    Check the response body sample against known host default page signatures.

    Returns (is_authentic: bool, false_positive_reason: str or None).
    is_authentic is True if no signature matched (the service looks real).
    """
    lower_body = body_sample.lower()
    for signature_id, fragment in HOST_DEFAULT_PAGE_SIGNATURES:
        if fragment.lower() in lower_body:
            return False, signature_id
    return True, None


def probe_service(reported_ip: str, reported_port: int = 80) -> dict:
    """
    Main entry point. Probe the service at reported_ip:reported_port and
    return a result dict that matches the 'service' block in result_schema.json.

    This function is called by run_veritas.py after the candidate tool has
    declared that a service is reachable. It does not trust that declaration —
    it verifies independently.

    Parameters
    ----------
    reported_ip : str
        The IP address the candidate tool printed as the firmware address.
    reported_port : int
        The port to probe. Defaults to 80.

    Returns
    -------
    dict
        A dict matching the service block of result_schema.json.
    """
    result = {
        "reported_ip": reported_ip,
        "reported_port": reported_port,
        "tcp_connect": None,
        "http_status": None,
        "body_size_bytes": None,
        "body_sha256": None,
        "service_reachable": False,
        "service_authentic": False,
        "false_positive_reason": None,
    }

    if not reported_ip:
        return result

    # Step 1 — raw TCP check
    tcp_ok = _tcp_connect(reported_ip, reported_port)
    result["tcp_connect"] = tcp_ok

    if not tcp_ok:
        # Nothing is listening. No point making an HTTP request.
        return result

    # Step 2 — HTTP fetch
    http = _fetch_http(reported_ip, reported_port)

    result["http_status"] = http["status_code"]
    result["body_size_bytes"] = http["body_size"]
    result["body_sha256"] = http["body_sha256"]

    if http["status_code"] is None:
        # TCP connected but HTTP failed — count as not reachable
        return result

    # Any HTTP response (even 4xx or 5xx) counts as reachable because
    # it means something answered. A firmware web server returning 401
    # is still the firmware.
    result["service_reachable"] = True

    # Step 3 — authenticity check
    is_authentic, fp_reason = _check_authenticity(http["body_sample"])
    result["service_authentic"] = is_authentic
    result["false_positive_reason"] = fp_reason

    return result


# ── Standalone test usage ──────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Probe a single IP:port for service reachability and authenticity"
    )
    parser.add_argument("--ip",   required=True,      help="IP address to probe")
    parser.add_argument("--port", type=int, default=80, help="Port to probe (default 80)")
    args = parser.parse_args()

    print(f"\nProbing {args.ip}:{args.port} ...")
    result = probe_service(reported_ip=args.ip, reported_port=args.port)
    print(json.dumps(result, indent=2))

    # Give a plain-English summary so it is easy to read at the terminal
    print()
    if result["service_reachable"] and result["service_authentic"]:
        print("RESULT: Service is reachable and appears to be from the firmware.")
    elif result["service_reachable"] and not result["service_authentic"]:
        print(f"RESULT: Service responded but matched a host default page "
              f"({result['false_positive_reason']}). This is likely a false positive.")
    elif result["tcp_connect"]:
        print("RESULT: TCP connection succeeded but HTTP request failed.")
    else:
        print("RESULT: Nothing is listening at this address.")
