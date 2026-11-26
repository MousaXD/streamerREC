"""Offline integration checks against the actual Squid egress image.

Run inside a network-isolated egress container with private-check.cubetecn.com
mapped to 127.0.0.1 and public-check.cubetecn.com mapped to 8.8.8.8.
The public fixture has no route; a non-403 upstream failure is expected.
"""

import socket
import subprocess
import time
import unittest


def response_status(host, port, method="CONNECT"):
    authority = f"{host}:{port}"
    target = authority if method == "CONNECT" else f"http://{authority}/"
    with socket.create_connection(("127.0.0.1", 3128), timeout=8) as conn:
        conn.sendall(
            f"{method} {target} HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n\r\n".encode()
        )
        with conn.makefile("rb") as response:
            return int(response.readline().split()[1])


class EgressPolicyChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.squid = subprocess.Popen(
            ["squid", "-N", "-f", "/etc/squid/squid.conf"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.addClassCleanup(cls.stop_squid)
        for _ in range(50):
            if cls.squid.poll() is not None:
                raise RuntimeError("Squid failed to start")
            try:
                with socket.create_connection(("127.0.0.1", 3128), timeout=1):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("Squid did not become ready")

    @classmethod
    def stop_squid(cls):
        if cls.squid.poll() is None:
            cls.squid.terminate()
            try:
                cls.squid.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.squid.kill()
                cls.squid.wait()

    def test_cdn_ports_pass_policy(self):
        for port in (1450, 1454, 1457, 1458, 1462, 1469):
            with self.subTest(port=port):
                # No external network: Squid may accept the destination but
                # cannot establish the upstream connection.
                self.assertIn(response_status("public-check.cubetecn.com", port), (502, 503, 504))

    def test_nonstandard_port_restrictions(self):
        for host, port, method in (
            ("example.com", 1454, "CONNECT"),
            ("cubetecn.com.example.com", 1454, "CONNECT"),
            ("public-check.cubetecn.com", 1449, "CONNECT"),
            ("public-check.cubetecn.com", 1470, "CONNECT"),
            ("public-check.cubetecn.com", 1454, "GET"),
            ("8.8.8.8", 1454, "CONNECT"),
            ("example.com", 80, "CONNECT"),
        ):
            with self.subTest(host=host, port=port, method=method):
                self.assertEqual(response_status(host, port, method), 403)

    def test_private_destinations_still_blocked(self):
        for host, port in (
            ("private-check.cubetecn.com", 1454),
            ("private-check.cubetecn.com", 443),
            ("127.0.0.1", 1454),
            ("192.168.1.1", 443),
            ("169.254.169.254", 443),
        ):
            with self.subTest(host=host, port=port):
                self.assertEqual(response_status(host, port), 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
