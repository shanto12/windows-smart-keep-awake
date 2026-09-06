#!/usr/bin/env python3
"""Keep Windows awake continuously and publish genuine Teams availability.

Python 3.9+. Run this script instead of keep_awake.py. This file is independent
of the original program and never imports it. Use only on an authorized device
and account, consistently with applicable organization policies.

QUICK START (PowerShell on Windows)
    py keep_awake_teams.py --dry-run --once
    py keep_awake_teams.py --dry-run --once --at 2026-09-08T09:00
    py keep_awake_teams.py --teams off
    py -m pip install "msal>=1.31,<2"
    py keep_awake_teams.py --tenant-id TENANT_GUID --client-id APP_GUID
    py keep_awake_teams.py --holiday 2026-12-25 --holidays-file holidays.txt
    py keep_awake_teams.py --self-test
    py keep_awake_teams.py --docs

AUTHENTICATION AND PERMISSIONS
    Register a single-tenant public client app in Microsoft Entra ID. Under
    Authentication, enable Allow public client flows for device-code sign-in.
    Add Microsoft Graph DELEGATED Presence.ReadWrite. Tenant consent policy
    may require an administrator even though this permission does not normally
    require admin consent. Do not add application-wide Presence.ReadWrite.All.
    No client secret is used. --tenant-id and --client-id can instead come from
    TEAMS_TENANT_ID and TEAMS_CLIENT_ID. These IDs are not passwords.

    Sign in to your own Teams-enabled work/school account at the Microsoft URL
    printed by MSAL. Personal Microsoft accounts are unsupported by these APIs,
    even when the purpose is personal availability. Conditional Access may
    prohibit device flow; use an administrator-approved flow instead of bypassing
    that policy. This script supports the Microsoft GLOBAL cloud only.

    MSAL adds the standard OpenID/profile/offline_access scopes and handles the
    device-code expiry and token refresh. The target user comes from MSAL's
    authenticated ID-token oid claim; User.Read is unnecessary. Tokens stay in
    memory, are never logged or saved, and are lost on exit. Sign-in is needed
    on every launch. Silent refresh can fail after revocation, consent changes,
    or session expiry. The script then disables presence updates with an error,
    keeps the Windows awake request active, and requires a restart/sign-in.
    Initial sign-in/configuration failures exit with a nonzero code.

SCHEDULE AND HOLIDAYS
    Default availability is Monday-Friday, 08:00 inclusive to 18:00 exclusive,
    using the Windows machine's LOCAL clock/timezone. Configure Windows to your
    intended timezone (for Shanto, America/Chicago / Central Time). Wall time
    is sampled again each loop, including after clock, timezone, or DST changes;
    monotonic time controls retry and renewal intervals. The normal polling
    interval is at most 15 seconds, shortened at schedule/date boundaries.

    --hours also accepts overnight windows; equal endpoints mean all day.
    Weekend/holiday exclusion always uses the CURRENT local date: for 22:00-
    06:00, Monday 01:00 may be Available but Saturday 01:00 is always Away.
    Holidays are explicit local YYYY-MM-DD dates, with no assumed country or
    automatic public-holiday lookup. Include observed/substitute dates yourself.
    --holidays-file is UTF-8, one date per line; blank lines and # comments are
    allowed. It is read once at startup; restart after changing it and maintain
    future years. --holiday may be repeated and is combined with the file.

WINDOWS AND TEAMS BEHAVIOR
    Windows: SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) stays
    active during BOTH Available and Away until this process stops. It requests
    prevention of ordinary idle system sleep; it does not keep the display on,
    inject input, change power/registry settings, unlock the device, or override
    managed locks, screensavers, lid closure, explicit sleep, or shutdown.
    Policies and hardware may reject/override it. It cannot operate while the
    machine is asleep or off. No service, startup task, or policy is installed.

    Teams defaults to --presence-mode preferred: setPresence creates an app
    session, then setUserPreferredPresence requests Available/Available or
    Away/Away. Both requests explicitly expire after five minutes (PT5M) and
    normally renew every 120 seconds. The sessionId is the registered app ID.
    Available sessions otherwise become inactive after five minutes. Session
    presence is aggregated DND > Busy > Available > Away, so --presence-mode
    session alone cannot reliably show Away when another client is Available.

    Preferred mode intentionally replaces the user's preferred/manual status
    and takes precedence over session-derived meeting/call status while this
    schedule runs. Stop the script before choosing a different manual status.
    Preferred presence needs a live session, supplied by setPresence. Teams
    propagation can take several minutes. HTTP acceptance is not proof of the
    displayed badge; there is NO guarantee of permanent green or immediate Away.

    Requests have finite timeouts. Transient failures use exponential backoff
    and honor Retry-After; the current schedule is recomputed before retrying.
    Network/token outages can let leases expire, leaving another status or
    Offline. The two Graph writes are not atomic. In-flight requests, clock
    changes, and Teams propagation can delay transitions across a boundary.

    Ctrl+C releases the Windows request first, then best-effort clears only
    this app's presence session. The user-wide preferred setting is left to
    expire within five minutes of its last accepted write, plus propagation:
    clearing it could erase a newer manual setting. Previous preferences cannot
    reliably be restored from aggregate presence. If this was the only session,
    clearing/expiry can make the account Offline. Run only ONE instance for the
    same app/account, including on other computers; they share the session ID.

    --dry-run works on any OS with no Windows calls, authentication, network,
    or token writes, and logs the intended decision. --at is a local naive
    timestamp usable only with --dry-run --once. --once performs one iteration
    and cleanup; it does NOT keep Windows awake after exit. --teams off keeps
    Windows awake without MSAL or Graph. --self-test uses only stdlib fakes and
    never contacts Windows or Graph. Existing repository CI does not invoke
    these embedded tests; run this file's --self-test explicitly.

OFFICIAL REFERENCES
    https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
    https://learn.microsoft.com/en-us/graph/api/presence-setpresence
    https://learn.microsoft.com/en-us/graph/api/presence-setuserpreferredpresence
    https://learn.microsoft.com/en-us/graph/api/presence-clearpresence
    https://learn.microsoft.com/en-us/graph/cloud-communications-manage-presence-state
    https://learn.microsoft.com/en-us/graph/permissions-reference#presencereadwrite
    https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration
    https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference
    https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import email.utils
import http.client
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import FrozenSet, Optional, Tuple


LOGGER = logging.getLogger("keep_awake_teams")
DEFAULT_HOURS = "08:00-18:00"
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
SCOPES = ["Presence.ReadWrite"]
LEASE = "PT5M"


def parse_clock(value: str) -> dt.time:
    if not re.fullmatch(r"\d{2}:\d{2}", value.strip()):
        raise ValueError("time must use HH:MM, for example 08:00")
    hour, minute = map(int, value.strip().split(":"))
    return dt.time(hour, minute)


def parse_hours(value: str) -> Tuple[dt.time, dt.time]:
    try:
        start, end = value.split("-")
        return parse_clock(start), parse_clock(end)
    except (ValueError, AttributeError):
        raise ValueError("hours must use valid HH:MM-HH:MM times") from None


def parse_date(value: str) -> dt.date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("holiday must use YYYY-MM-DD")
    return dt.date.fromisoformat(value)


def load_holidays(values, filename: Optional[str]) -> FrozenSet[dt.date]:
    dates = {parse_date(value) for value in values}
    if filename:
        for number, line in enumerate(
            Path(filename).read_text(encoding="utf-8-sig").splitlines(), 1
        ):
            value = line.partition("#")[0].strip()
            if value:
                try:
                    dates.add(parse_date(value))
                except ValueError:
                    raise ValueError(
                        "invalid holiday at %s:%d; use YYYY-MM-DD" % (filename, number)
                    ) from None
    return frozenset(dates)


def parse_guid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("tenant, application, and user IDs must be GUIDs") from None


def positive_seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("seconds must be a number") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("seconds must be finite and greater than zero")
    return number


@dataclass(frozen=True)
class Schedule:
    start: dt.time = dt.time(8)
    end: dt.time = dt.time(18)
    holidays: FrozenSet[dt.date] = frozenset()

    def decision(self, now: dt.datetime) -> Tuple[str, str]:
        if now.date() in self.holidays:
            return "Away", "configured holiday"
        if now.weekday() >= 5:
            return "Away", "weekend"
        clock = now.time()
        active = (
            self.start == self.end
            or (self.start < self.end and self.start <= clock < self.end)
            or (self.start > self.end and (clock >= self.start or clock < self.end))
        )
        return ("Available", "work window") if active else ("Away", "outside work window")

    def seconds_to_boundary(self, now: dt.datetime) -> float:
        candidates = [
            dt.datetime.combine(now.date() + dt.timedelta(days=offset), clock)
            for offset in (0, 1)
            for clock in (dt.time(), self.start, self.end)
        ]
        return min((value - now).total_seconds() for value in candidates if value > now)


class WindowsPlatform:
    """Thread-scoped sleep prevention only; no input or lock-policy APIs."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("live mode requires Windows; use --dry-run or --self-test here")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
        self.kernel32.SetThreadExecutionState.restype = ctypes.c_uint32

    def set_keep_awake(self, enabled: bool) -> None:
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
        if not self.kernel32.SetThreadExecutionState(flags):
            raise OSError("SetThreadExecutionState failed; Windows rejected the power request")


class AuthRequired(RuntimeError):
    """A new explicit user sign-in or configuration change is required."""


class ServiceError(RuntimeError):
    def __init__(self, message, retryable=True, retry_after=0.0, status=None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.status = status


class MsalAuth:
    """Public-client device login; in-memory cache and silent renewal only."""

    def __init__(self, tenant_id: str, client_id: str, timeout: float):
        # Some MSAL error paths log raw token responses even with PII disabled.
        # Keep only this script's sanitized diagnostics in this dedicated process.
        msal_logger = logging.getLogger("msal")
        msal_logger.handlers = [logging.NullHandler()]
        msal_logger.propagate = False
        try:
            import msal
            import requests
        except ImportError:
            raise AuthRequired('install MSAL first: py -m pip install "msal>=1.31,<2"') from None
        self.network_errors = (requests.exceptions.RequestException, OSError)
        self.tenant_id = parse_guid(tenant_id)
        self.client_id = parse_guid(client_id)
        self.account = None
        self.user_id = None
        try:
            self.app = msal.PublicClientApplication(
                self.client_id,
                authority="https://login.microsoftonline.com/" + self.tenant_id,
                token_cache=msal.TokenCache(),
                timeout=timeout,
                enable_pii_log=False,
            )
        except self.network_errors:
            raise AuthRequired(
                "cannot reach Microsoft sign-in; check connectivity and restart"
            ) from None
        except ValueError:
            raise AuthRequired(
                "invalid Microsoft authority/app configuration; check tenant ID"
            ) from None

    def login(self) -> None:
        try:
            flow = self.app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise AuthRequired(
                    "device sign-in unavailable; check public client flows, app and tenant policy"
                )
            # MSAL's sign-in instruction includes the short-lived code; not a token.
            print(flow["message"], flush=True)
            result = self.app.acquire_token_by_device_flow(flow)
        except self.network_errors:
            raise AuthRequired(
                "Microsoft sign-in failed due to network error; restart to sign in"
            ) from None
        except ValueError:
            raise AuthRequired(
                "Microsoft sign-in returned an invalid response; restart to sign in"
            ) from None
        if not result or "access_token" not in result:
            raise AuthRequired(
                "sign-in expired, was denied, or needs consent; check Entra policy and restart"
            )
        claims = result.get("id_token_claims", {})
        try:
            self.user_id = parse_guid(claims.get("oid"))
            tenant = parse_guid(claims.get("tid"))
        except ValueError:
            raise AuthRequired("authenticated account has no usable tenant/user ID") from None
        if tenant != self.tenant_id:
            raise AuthRequired("authenticated account belongs to a different tenant")
        accounts = [
            account
            for account in self.app.get_accounts()
            if account.get("local_account_id", "").lower() == self.user_id
            and account.get("realm", "").lower() == self.tenant_id
        ]
        if len(accounts) != 1:
            raise AuthRequired("could not select the signed-in account for silent token renewal")
        self.account = accounts[0]

    def token(self, force_refresh=False) -> str:
        if self.account is None:
            raise AuthRequired("sign-in is required; restart the script")
        try:
            result = self.app.acquire_token_silent_with_error(
                SCOPES, account=self.account, force_refresh=force_refresh
            )
        except self.network_errors:
            raise ServiceError("Microsoft token renewal network failure") from None
        except ValueError:
            raise ServiceError("Microsoft token renewal returned an invalid response") from None
        if not result or "access_token" not in result:
            error = (result or {}).get("error")
            if error in ("temporarily_unavailable", "server_error"):
                raise ServiceError("Microsoft token service is temporarily unavailable")
            raise AuthRequired("silent sign-in expired or was rejected; restart and sign in again")
        return result["access_token"]


def retry_after_seconds(value: Optional[str], now=None) -> float:
    if not value:
        return 0.0
    try:
        result = float(value)
    except ValueError:
        try:
            target = email.utils.parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=dt.timezone.utc)
            result = (target - (now or dt.datetime.now(dt.timezone.utc))).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return max(0.0, result) if math.isfinite(result) else 0.0


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a bearer token to a redirected endpoint.


class GraphClient:
    def __init__(self, auth, mode="preferred", timeout=20.0, opener=None, state_provider=None):
        self.auth = auth
        self.mode = mode
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(NoRedirects())
        self.state_provider = state_provider
        self.may_have_session = False

    def post(self, action: str, payload, force_refresh=False) -> Optional[str]:
        if action not in ("setPresence", "setUserPreferredPresence", "clearPresence"):
            raise ValueError("unsupported Graph operation")
        token = self.auth.token(force_refresh=force_refresh)
        # Evaluate AFTER token renewal, including on a bounded 401 retry.
        body = payload() if callable(payload) else payload
        user_id = parse_guid(self.auth.user_id)
        request = urllib.request.Request(
            GRAPH_ROOT + "/users/" + user_id + "/presence/" + action,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise ServiceError("unexpected Graph response", retryable=False)
        except urllib.error.HTTPError as error:
            status = error.code
            delay = retry_after_seconds((error.headers or {}).get("Retry-After"))
            error.close()
            if status == 401:
                if not force_refresh:
                    return self.post(action, payload, force_refresh=True)
                raise AuthRequired(
                    "Graph rejected refreshed credentials; restart and sign in"
                ) from None
            if status == 404 and action == "clearPresence":
                return
            hints = {
                400: "request rejected; check supported account and presence configuration",
                403: "access denied; verify delegated Presence.ReadWrite consent and tenant policy",
                404: "user/presence unavailable; verify Teams support for the signed-in account",
                429: "rate limited; honoring Retry-After",
            }
            message = hints.get(
                status, "service unavailable" if status >= 500 else "request rejected"
            )
            raise ServiceError(
                "Graph HTTP %d: %s" % (status, message),
                retryable=status in (408, 429) or 500 <= status < 600,
                retry_after=delay,
                status=status,
            ) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise ServiceError("Graph network/TLS/timeout failure; check connectivity") from None
        return body.get("availability")

    def publish(self, state: str) -> str:
        if state not in ("Available", "Away"):
            raise ValueError("unsupported presence state")

        def body(session=False):
            current = self.state_provider() if self.state_provider else state
            if current not in ("Available", "Away"):
                raise ValueError("unsupported presence state")
            payload = {"availability": current, "activity": current, "expirationDuration": LEASE}
            if session:
                payload["sessionId"] = self.auth.client_id
            return payload

        # Also clear after an ambiguous timeout: the server may have accepted it.
        self.may_have_session = True
        accepted = self.post("setPresence", lambda: body(session=True))
        if self.mode == "preferred":
            accepted = self.post("setUserPreferredPresence", body)
        return accepted

    def clear(self) -> None:
        if self.may_have_session:
            self.post("clearPresence", {"sessionId": self.auth.client_id})
            self.may_have_session = False


class PresenceController:
    """No queued state: every attempt uses the caller's current schedule."""

    def __init__(self, graph, refresh=120.0, monotonic=None):
        self.graph = graph
        self.refresh = refresh
        self.monotonic = monotonic or time.monotonic
        self.last_state = None
        self.renew_at = 0.0
        self.retry_at = 0.0
        self.failures = 0
        self.disabled = False
        self.healthy = True

    def tick(self, desired: str) -> None:
        current = self.monotonic()
        if self.disabled or current < self.retry_at:
            return
        if self.healthy and desired == self.last_state and current < self.renew_at:
            return
        try:
            accepted = self.graph.publish(desired)
        except AuthRequired as error:
            self.disabled = True
            self.healthy = False
            LOGGER.error("Teams updates disabled; Windows awake request continues: %s", error)
        except ServiceError as error:
            self.healthy = False
            if not error.retryable:
                self.disabled = True
                LOGGER.error("Teams updates disabled; Windows awake request continues: %s", error)
                return
            self.failures += 1
            delay = max(error.retry_after, min(300.0, 5.0 * 2 ** min(self.failures - 1, 6)))
            self.retry_at = self.monotonic() + delay
            LOGGER.warning("%s; retry in %.0fs; Teams status unconfirmed", error, delay)
        else:
            self.healthy = True
            self.failures = 0
            self.retry_at = 0.0
            self.last_state = accepted if accepted in ("Available", "Away") else desired
            # A slow preferred-presence call must not extend the earlier lease.
            self.renew_at = current + self.refresh
            LOGGER.info(
                "Graph accepted %s (%s, five-minute expiry); Teams display unverified",
                self.last_state,
                self.graph.mode,
            )

    def close(self) -> None:
        if self.disabled or self.monotonic() < self.retry_at:
            LOGGER.warning(
                "Skipping Graph cleanup after rejection/backoff; presence leases will expire"
            )
            return
        try:
            self.graph.clear()
        except (AuthRequired, ServiceError) as error:
            LOGGER.warning("Could not clear app session; it will expire: %s", error)


class Runner:
    """One thread owns the Windows request; network failures do not release it."""

    def __init__(
        self,
        schedule,
        platform=None,
        graph_factory=None,
        dry_run=False,
        teams=True,
        poll=15.0,
        refresh=120.0,
        now=None,
        monotonic=None,
        sleep=None,
    ):
        self.schedule = schedule
        self.platform = platform
        self.graph_factory = graph_factory
        self.dry_run = dry_run
        self.teams = teams
        self.poll = poll
        self.refresh = refresh
        self.now = now or dt.datetime.now
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.controller = None

    def run(self, once=False) -> int:
        awake = False
        last_decision = None
        try:
            if not self.dry_run:
                self.platform.set_keep_awake(True)
                awake = True
                LOGGER.info("Windows continuous system-awake request active")
                if self.teams:
                    self.controller = PresenceController(
                        self.graph_factory(), self.refresh, self.monotonic
                    )
            while True:
                current = self.now()  # After initial interactive authentication.
                decision = self.schedule.decision(current)
                if decision != last_decision or self.dry_run:
                    LOGGER.info(
                        "%s; system awake continuously; Teams %s (%s)%s",
                        current.isoformat(sep=" ", timespec="seconds"),
                        decision[0] if self.teams else "updates off",
                        decision[1],
                        " [dry-run; no side effects]" if self.dry_run else "",
                    )
                    last_decision = decision
                if self.controller:
                    self.controller.tick(decision[0])
                if once:
                    return int(self.controller is not None and not self.controller.healthy)
                current = self.now()
                # Re-evaluate immediately if a request crossed a schedule boundary.
                if self.schedule.decision(current) != decision:
                    continue
                delay = min(self.poll, self.schedule.seconds_to_boundary(current))
                if self.controller and not self.controller.disabled:
                    deadline = self.controller.retry_at or self.controller.renew_at
                    if deadline > self.monotonic():
                        delay = min(delay, deadline - self.monotonic())
                self.sleep(max(0.05, delay))
        finally:
            # Power cleanup must happen before any slow or failing network call.
            try:
                if awake:
                    self.platform.set_keep_awake(False)
                    LOGGER.info("Windows awake request released")
            finally:
                if self.controller:
                    self.controller.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Keep Windows awake continuously; schedule genuine Teams availability.",
        epilog="Use --docs for setup, permissions, holiday format, and presence limitations.",
    )
    parser.add_argument(
        "--hours",
        default=DEFAULT_HOURS,
        metavar="START-END",
        help="local work window (default: %(default)s); weekends/holidays stay Away",
    )
    parser.add_argument(
        "--holiday",
        action="append",
        default=[],
        metavar="YYYY-MM-DD",
        help="exclude this date (repeatable; include observed dates)",
    )
    parser.add_argument(
        "--holidays-file", metavar="PATH", help="UTF-8 dates file, one per line; # comments allowed"
    )
    parser.add_argument(
        "--teams",
        choices=("graph", "off"),
        default="graph",
        help="publish presence or just prevent sleep (default: %(default)s)",
    )
    parser.add_argument(
        "--presence-mode",
        choices=("preferred", "session"),
        default="preferred",
        help="preferred overrides manual/meeting status; session may not show Away",
    )
    parser.add_argument(
        "--tenant-id",
        default=os.environ.get("TEAMS_TENANT_ID"),
        metavar="GUID",
        help="Entra tenant GUID (or TEAMS_TENANT_ID)",
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("TEAMS_CLIENT_ID"),
        metavar="GUID",
        help="public-client app GUID (or TEAMS_CLIENT_ID)",
    )
    parser.add_argument(
        "--poll",
        type=positive_seconds,
        default=15.0,
        metavar="SECONDS",
        help="schedule polling, 1-60 seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--refresh",
        type=positive_seconds,
        default=120.0,
        metavar="SECONDS",
        help="presence renewal, 30-240 seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_seconds,
        default=20.0,
        metavar="SECONDS",
        help="per HTTP request timeout, 1-30 seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview on any OS without Windows calls, authentication or network",
    )
    parser.add_argument(
        "--at", metavar="LOCAL_DATETIME", help="fixed local timestamp; requires --dry-run --once"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="one iteration then release power request and clear app session",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="show each renewal and schedule transition"
    )
    parser.add_argument(
        "--docs", action="store_true", help="print self-contained setup and limitations"
    )
    parser.add_argument(
        "--self-test", action="store_true", help="run embedded offline unit tests on any OS"
    )
    parser.add_argument("--version", action="version", version="1.0.0")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.docs:
        print(__doc__)
        return 0
    if args.self_test:
        return run_self_tests()
    logging.basicConfig(
        level=logging.INFO if args.verbose or args.dry_run else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fixed_time = None
    try:
        start, end = parse_hours(args.hours)
        schedule = Schedule(start, end, load_holidays(args.holiday, args.holidays_file))
        for name, value, low, high in (
            ("poll", args.poll, 1, 60),
            ("refresh", args.refresh, 30, 240),
            ("timeout", args.timeout, 1, 30),
        ):
            if not low <= value <= high:
                raise ValueError("--%s must be between %d and %d seconds" % (name, low, high))
        if args.at:
            if not args.dry_run or not args.once:
                raise ValueError("--at requires --dry-run --once")
            fixed_time = dt.datetime.fromisoformat(args.at)
            if fixed_time.tzinfo is not None:
                raise ValueError("--at must use a local timestamp without a UTC offset")
        if args.teams == "graph" and not args.dry_run:
            if not args.tenant_id or not args.client_id:
                raise ValueError(
                    "Teams needs --tenant-id and --client-id; see --docs or use --teams off"
                )
            args.tenant_id = parse_guid(args.tenant_id)
            args.client_id = parse_guid(args.client_id)
    except (ValueError, OSError, UnicodeError) as error:
        parser.error(str(error))

    def graph_factory():
        auth = MsalAuth(args.tenant_id, args.client_id, args.timeout)
        auth.login()
        return GraphClient(
            auth,
            args.presence_mode,
            args.timeout,
            state_provider=lambda: schedule.decision(dt.datetime.now())[0],
        )

    def stop(signum, frame):
        raise KeyboardInterrupt

    try:
        platform = None if args.dry_run else WindowsPlatform()
        if not args.dry_run:
            signal.signal(signal.SIGTERM, stop)
        runner = Runner(
            schedule,
            platform,
            graph_factory,
            args.dry_run,
            args.teams == "graph",
            args.poll,
            args.refresh,
            now=(lambda: fixed_time) if fixed_time else None,
        )
        return runner.run(once=args.once)
    except KeyboardInterrupt:
        LOGGER.info("Stopped; any remaining preferred presence expires after its short lease")
        return 130
    except (AuthRequired, ServiceError, OSError) as error:
        LOGGER.error("%s", error)
        return 1


def run_self_tests() -> int:
    """Embedded tests keep the repository change confined to this one file."""
    import io
    import unittest
    from unittest.mock import MagicMock, Mock, patch

    app_id = "11111111-1111-4111-8111-111111111111"
    user_id = "22222222-2222-4222-8222-222222222222"

    class ScheduleTests(unittest.TestCase):
        def test_default_boundaries(self):
            schedule = Schedule()
            for clock, expected in (
                ("07:59:59", "Away"),
                ("08:00:00", "Available"),
                ("17:59:59", "Available"),
                ("18:00:00", "Away"),
            ):
                with self.subTest(clock=clock):
                    self.assertEqual(
                        schedule.decision(dt.datetime.fromisoformat("2026-09-08T" + clock))[0],
                        expected,
                    )

        def test_weekends_holidays_and_year_boundary(self):
            schedule = Schedule(holidays=frozenset({dt.date(2027, 1, 1)}))
            for day in (
                dt.datetime(2026, 9, 5, 10),
                dt.datetime(2026, 9, 6, 10),
                dt.datetime(2027, 1, 1, 10),
            ):
                self.assertEqual(schedule.decision(day)[0], "Away")
            self.assertEqual(schedule.decision(dt.datetime(2026, 12, 31, 10))[0], "Available")

        def test_overnight_uses_current_calendar_date(self):
            schedule = Schedule(*parse_hours("22:00-06:00"))
            for value, expected in (
                ("2026-09-07T01:00", "Available"),
                ("2026-09-11T23:00", "Available"),
                ("2026-09-12T01:00", "Away"),
                ("2026-09-08T06:00", "Away"),
            ):
                self.assertEqual(schedule.decision(dt.datetime.fromisoformat(value))[0], expected)

        def test_all_day_still_excludes_weekend(self):
            schedule = Schedule(dt.time(), dt.time())
            self.assertEqual(schedule.decision(dt.datetime(2026, 9, 7))[0], "Available")
            self.assertEqual(schedule.decision(dt.datetime(2026, 9, 6))[0], "Away")

        def test_next_boundary_includes_midnight(self):
            self.assertEqual(Schedule().seconds_to_boundary(dt.datetime(2026, 9, 8, 7, 59, 59)), 1)
            self.assertEqual(
                Schedule().seconds_to_boundary(dt.datetime(2026, 9, 11, 23, 59, 59)), 1
            )

        def test_invalid_inputs(self):
            for value in ("25:00-18:00", "08:00", "08:60-18:00", "8-18", "08:00-18:00-19:00"):
                with self.assertRaises(ValueError):
                    parse_hours(value)
            for value in ("nan", "inf", "-1", "0"):
                with self.assertRaises(argparse.ArgumentTypeError):
                    positive_seconds(value)
            with self.assertRaises(ValueError):
                parse_date("2026-02-30")

        def test_holiday_file_comments_and_validation(self):
            with patch.object(
                Path, "read_text", return_value="# observed\n2026-12-25 # holiday\n\n"
            ):
                self.assertEqual(
                    load_holidays(["2027-01-01"], "dates.txt"),
                    frozenset({dt.date(2026, 12, 25), dt.date(2027, 1, 1)}),
                )
            with patch.object(Path, "read_text", return_value="invalid"):
                with self.assertRaisesRegex(ValueError, "dates.txt:1"):
                    load_holidays([], "dates.txt")

    class GraphTests(unittest.TestCase):
        def setUp(self):
            self.auth = Mock(client_id=app_id, user_id=user_id)
            self.auth.token.return_value = "not-a-real-token"
            self.opener = MagicMock()
            self.opener.open.return_value.__enter__.return_value.status = 200
            self.graph = GraphClient(self.auth, opener=self.opener)

        def http_error(self, status, headers=None):
            return urllib.error.HTTPError(
                "https://graph.microsoft.com",
                status,
                "test",
                headers or {},
                io.BytesIO(b"private server body"),
            )

        def test_preferred_payloads_and_owned_cleanup(self):
            self.graph.publish("Away")
            self.graph.clear()
            calls = self.opener.open.call_args_list
            self.assertEqual(len(calls), 3)
            bodies = [json.loads(call.args[0].data) for call in calls]
            self.assertEqual(
                bodies[0],
                {
                    "sessionId": app_id,
                    "availability": "Away",
                    "activity": "Away",
                    "expirationDuration": "PT5M",
                },
            )
            self.assertNotIn("sessionId", bodies[1])
            self.assertEqual(bodies[2], {"sessionId": app_id})
            self.assertTrue(
                calls[0].args[0].full_url.endswith("/users/" + user_id + "/presence/setPresence")
            )
            self.assertTrue(calls[2].args[0].full_url.endswith("/clearPresence"))

        def test_session_mode_only_uses_set_presence(self):
            self.graph.mode = "session"
            self.graph.publish("Available")
            self.assertEqual(self.opener.open.call_count, 1)

        def test_throttle_and_body_redaction(self):
            self.opener.open.side_effect = self.http_error(429, {"Retry-After": "90"})
            with self.assertRaises(ServiceError) as caught:
                self.graph.publish("Available")
            self.assertEqual(caught.exception.retry_after, 90)
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn("private server body", str(caught.exception))

        def test_401_refreshes_only_once(self):
            self.opener.open.side_effect = [self.http_error(401), self.http_error(401)]
            with self.assertRaises(AuthRequired):
                self.graph.publish("Available")
            self.assertEqual(self.opener.open.call_count, 2)
            self.assertTrue(self.auth.token.call_args.kwargs["force_refresh"])

        def test_forbidden_disables_retry(self):
            self.opener.open.side_effect = self.http_error(403)
            with self.assertRaises(ServiceError) as caught:
                self.graph.publish("Available")
            self.assertFalse(caught.exception.retryable)

        def test_partial_publish_still_clears_session(self):
            ok = self.opener.open.return_value
            self.opener.open.side_effect = [ok, self.http_error(503), ok]
            with self.assertRaises(ServiceError):
                self.graph.publish("Available")
            self.graph.clear()
            self.assertTrue(self.opener.open.call_args.args[0].full_url.endswith("/clearPresence"))

        def test_network_failure_and_absent_cleanup(self):
            self.opener.open.side_effect = urllib.error.URLError("do not log sensitive details")
            with self.assertRaises(ServiceError):
                self.graph.publish("Away")
            self.opener.open.side_effect = self.http_error(404)
            self.graph.clear()
            self.assertFalse(self.graph.may_have_session)

        def test_retry_after_dates_and_invalid_values(self):
            now = dt.datetime(2026, 9, 8, tzinfo=dt.timezone.utc)
            self.assertEqual(retry_after_seconds("Tue, 08 Sep 2026 00:01:00 GMT", now), 60)
            for value in (None, "bad", "nan", "inf", "-10"):
                self.assertEqual(retry_after_seconds(value, now), 0)

        def test_redirects_are_rejected(self):
            self.assertIsNone(
                NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.example")
            )

        def test_schedule_recomputed_after_401_and_silent_refresh(self):
            wall = [dt.datetime(2026, 9, 8, 17, 59, 59)]

            def token(force_refresh=False):
                if force_refresh:
                    wall[0] = dt.datetime(2026, 9, 8, 18, 0, 10)
                return "not-a-real-token"

            self.auth.token.side_effect = token
            self.graph.state_provider = lambda: Schedule().decision(wall[0])[0]
            ok = self.opener.open.return_value
            self.opener.open.side_effect = [self.http_error(401), ok, ok]
            self.assertEqual(self.graph.publish("Available"), "Away")
            states = [
                json.loads(call.args[0].data)["availability"]
                for call in self.opener.open.call_args_list
            ]
            self.assertEqual(states, ["Available", "Away", "Away"])

        def test_schedule_recomputed_before_each_write(self):
            self.graph.state_provider = Mock(side_effect=["Available", "Away"])
            self.assertEqual(self.graph.publish("Available"), "Away")
            self.assertEqual(
                json.loads(self.opener.open.call_args.args[0].data)["activity"], "Away"
            )

        def test_malformed_http_is_retryable(self):
            self.opener.open.side_effect = http.client.BadStatusLine("private data")
            with self.assertRaises(ServiceError) as caught:
                self.graph.publish("Away")
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn("private data", str(caught.exception))

    class AuthTests(unittest.TestCase):
        def setUp(self):
            self.auth = MsalAuth.__new__(MsalAuth)
            self.auth.network_errors = (OSError,)
            self.auth.tenant_id = app_id
            self.auth.app = Mock()
            self.auth.account = None
            self.auth.app.initiate_device_flow.return_value = {
                "user_code": "test",
                "message": "test sign-in instruction",
            }
            self.auth.app.acquire_token_by_device_flow.return_value = {
                "access_token": "test",
                "id_token_claims": {"oid": user_id, "tid": app_id},
            }
            self.auth.app.get_accounts.return_value = [
                {"local_account_id": user_id, "realm": app_id}
            ]

        def test_login_targets_own_oid_with_least_privilege(self):
            with patch("builtins.print"):
                self.auth.login()
            self.assertEqual(self.auth.user_id, user_id)
            self.auth.app.initiate_device_flow.assert_called_once_with(
                scopes=["Presence.ReadWrite"]
            )
            self.assertIsNotNone(self.auth.account)

        def test_wrong_tenant_is_rejected(self):
            self.auth.app.acquire_token_by_device_flow.return_value["id_token_claims"]["tid"] = (
                user_id
            )
            with patch("builtins.print"), self.assertRaises(AuthRequired):
                self.auth.login()

        def test_malformed_device_response_is_redacted(self):
            self.auth.app.initiate_device_flow.side_effect = ValueError("private response")
            with self.assertRaises(AuthRequired) as caught:
                self.auth.login()
            self.assertNotIn("private response", str(caught.exception))

        def test_silent_refresh_does_not_start_interactive_login(self):
            self.auth.account = {"local_account_id": user_id}
            self.auth.app.acquire_token_silent_with_error.return_value = {"access_token": "test"}
            self.assertEqual(self.auth.token(force_refresh=True), "test")
            self.auth.app.initiate_device_flow.assert_not_called()
            self.assertTrue(
                self.auth.app.acquire_token_silent_with_error.call_args.kwargs["force_refresh"]
            )

        def test_expired_and_transient_refresh_results(self):
            self.auth.account = {"local_account_id": user_id}
            self.auth.app.acquire_token_silent_with_error.return_value = {"error": "invalid_grant"}
            with self.assertRaises(AuthRequired):
                self.auth.token()
            self.auth.app.acquire_token_silent_with_error.return_value = {
                "error": "temporarily_unavailable"
            }
            with self.assertRaises(ServiceError):
                self.auth.token()

        def test_malformed_refresh_response_is_retryable(self):
            self.auth.account = {"local_account_id": user_id}
            self.auth.app.acquire_token_silent_with_error.side_effect = ValueError(
                "private response"
            )
            with self.assertRaises(ServiceError) as caught:
                self.auth.token()
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn("private response", str(caught.exception))

    class RunnerTests(unittest.TestCase):
        def setUp(self):
            self.clock = 0.0
            self.wall = dt.datetime(2026, 9, 8, 17, 59)
            self.graph = Mock(mode="preferred")
            self.platform = Mock()

        def controller(self):
            return PresenceController(self.graph, monotonic=lambda: self.clock)

        def test_renewal_and_boundary_change(self):
            controller = self.controller()
            controller.tick("Available")
            self.clock = 119
            controller.tick("Available")
            self.assertEqual(self.graph.publish.call_count, 1)
            self.clock = 120
            controller.tick("Available")
            controller.tick("Away")
            self.assertEqual(
                [call.args[0] for call in self.graph.publish.call_args_list],
                ["Available", "Available", "Away"],
            )

        def test_backoff_honored_across_schedule_change(self):
            self.graph.publish.side_effect = [ServiceError("rate limit", retry_after=90), None]
            controller = self.controller()
            controller.tick("Available")
            self.clock = 20
            controller.tick("Away")
            self.assertEqual(self.graph.publish.call_count, 1)
            self.clock = 90
            controller.tick("Away")
            self.assertEqual(self.graph.publish.call_args.args, ("Away",))
            self.assertTrue(controller.healthy)

        def test_auth_failure_disables_presence(self):
            self.graph.publish.side_effect = AuthRequired("sign in again")
            controller = self.controller()
            controller.tick("Available")
            self.clock = 1000
            controller.tick("Away")
            self.assertTrue(controller.disabled)
            self.assertEqual(self.graph.publish.call_count, 1)

        def test_dry_run_never_touches_platform_or_factory(self):
            factory = Mock(side_effect=AssertionError("must not authenticate"))
            runner = Runner(Schedule(), self.platform, factory, dry_run=True, now=lambda: self.wall)
            self.assertEqual(runner.run(once=True), 0)
            self.platform.set_keep_awake.assert_not_called()
            factory.assert_not_called()

        def test_awake_outside_hours_and_cleanup_order(self):
            events = []
            self.platform.set_keep_awake.side_effect = lambda value: events.append(value)
            self.graph.clear.side_effect = lambda: events.append("clear")
            runner = Runner(
                Schedule(),
                self.platform,
                lambda: self.graph,
                now=lambda: dt.datetime(2026, 9, 6, 23),
            )
            self.assertEqual(runner.run(once=True), 0)
            self.graph.publish.assert_called_once_with("Away")
            self.assertEqual(events, [True, False, "clear"])

        def test_initial_login_crossing_boundary_uses_fresh_schedule(self):
            def factory():
                self.wall = dt.datetime(2026, 9, 8, 18, 1)
                return self.graph

            Runner(Schedule(), self.platform, factory, now=lambda: self.wall).run(once=True)
            self.graph.publish.assert_called_once_with("Away")

        def test_power_released_on_auth_startup_error(self):
            factory = Mock(side_effect=AuthRequired("cancelled"))
            with self.assertRaises(AuthRequired):
                Runner(Schedule(), self.platform, factory).run(once=True)
            self.assertEqual(
                [call.args[0] for call in self.platform.set_keep_awake.call_args_list],
                [True, False],
            )

        def test_teams_off_has_no_graph_calls(self):
            factory = Mock()
            Runner(Schedule(), self.platform, factory, teams=False).run(once=True)
            factory.assert_not_called()
            self.assertEqual(self.platform.set_keep_awake.call_count, 2)

        def test_runtime_graph_error_does_not_drop_awake_request(self):
            self.graph.publish.side_effect = AuthRequired("consent revoked")

            def sleep(delay):
                self.assertEqual(
                    [call.args[0] for call in self.platform.set_keep_awake.call_args_list], [True]
                )
                raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                Runner(Schedule(), self.platform, lambda: self.graph, sleep=sleep).run()
            self.assertEqual(
                [call.args[0] for call in self.platform.set_keep_awake.call_args_list],
                [True, False],
            )

        def test_cleanup_error_does_not_mask_power_release(self):
            self.graph.clear.side_effect = ServiceError("offline")
            Runner(Schedule(), self.platform, lambda: self.graph).run(once=True)
            self.platform.set_keep_awake.assert_called_with(False)

        def test_slow_second_write_does_not_delay_renewal(self):
            def publish(state):
                self.clock += 70
                return state

            self.graph.publish.side_effect = publish
            controller = PresenceController(self.graph, refresh=240, monotonic=lambda: self.clock)
            controller.tick("Available")
            self.assertEqual(controller.renew_at, 240)
            self.clock = 240
            controller.tick("Available")
            self.assertEqual(self.graph.publish.call_count, 2)

        def test_controller_records_state_used_after_token_refresh(self):
            self.graph.publish.return_value = "Away"
            controller = self.controller()
            controller.tick("Available")
            self.assertEqual(controller.last_state, "Away")

        def test_partial_failure_does_not_suppress_corrective_state(self):
            self.graph.publish.side_effect = ["Away", ServiceError("partial write"), "Away"]
            controller = self.controller()
            controller.tick("Away")
            self.clock = 10
            controller.tick("Available")
            self.clock = 75
            controller.tick("Away")
            self.assertEqual(
                [call.args[0] for call in self.graph.publish.call_args_list],
                ["Away", "Available", "Away"],
            )

    suite = unittest.TestSuite()
    for case in (ScheduleTests, GraphTests, AuthTests, RunnerTests):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    with patch.object(LOGGER, "disabled", True):
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
