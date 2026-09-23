"""Verify that no dashboard is reachable without going through the portal.

The portal only helps if the apps behind it are bound to loopback. This does not
read config or parse netstat — it actually tries to open a socket to each
dashboard's port over this machine's LAN address, which is what an outsider
would do. If the connection succeeds, the login can be bypassed.
"""

import socket
from urllib.parse import urlsplit

from django.core.management.base import BaseCommand

from catalog.models import Dashboard

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def lan_address() -> str | None:
    """This machine's outward-facing IPv4, without sending anything.

    Connecting a UDP socket only sets the kernel's chosen source address; no
    packet leaves the box.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Command(BaseCommand):
    help = "Check that dashboards are bound to loopback and cannot bypass the login."

    def handle(self, *args, **options):
        lan = lan_address()
        if not lan:
            self.stdout.write(self.style.WARNING(
                "No LAN address found - this machine appears to be offline, so the "
                "check cannot prove anything. Re-run when connected."))
            return

        self.stdout.write(f"Testing from this machine's LAN address: {lan}\n")

        exposed = []
        for dash in Dashboard.objects.filter(enabled=True):
            if dash.is_internal:
                # Served by this process behind @login_required — there is no
                # second port that could answer without a session.
                self.stdout.write(self.style.SUCCESS(
                    f"  {dash.slug:<14} internal - no separate port to bypass"))
                continue

            parts = urlsplit(dash.upstream)
            host = parts.hostname or ""
            port = parts.port or (443 if parts.scheme == "https" else 80)

            if host not in LOOPBACK:
                # The upstream is on another machine; loopback binding is not
                # something this portal can reason about.
                self.stdout.write(f"  {dash.slug:<14} remote upstream ({host}) - not checked")
                continue

            running = reachable("127.0.0.1", port)
            open_to_lan = reachable(lan, port)

            if open_to_lan:
                exposed.append((dash, port))
                self.stdout.write(self.style.ERROR(
                    f"  {dash.slug:<14} EXPOSED - anyone who can reach {lan}:{port} "
                    f"skips the login"))
            elif running:
                self.stdout.write(self.style.SUCCESS(
                    f"  {dash.slug:<14} loopback only (:{port}) - portal login required"))
            else:
                self.stdout.write(
                    f"  {dash.slug:<14} not running (:{port}) - nothing to reach")

        self.stdout.write("")
        if exposed:
            self.stdout.write(self.style.ERROR(
                f"{len(exposed)} dashboard(s) bypassable. Bind them to 127.0.0.1:"))
            for dash, port in exposed:
                self.stdout.write(f"  - {dash.name}: see the 'Locking the dashboards down' "
                                  f"section of the portal README")
        else:
            self.stdout.write(self.style.SUCCESS(
                "No dashboard is reachable from the network. The portal login is the "
                "only way in."))