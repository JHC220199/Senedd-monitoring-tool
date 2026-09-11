"""Getting a Microsoft Graph token that survives being left alone.

READ THIS BEFORE CHANGING HOW THE MAILBOX ROUTE AUTHENTICATES
-------------------------------------------------------------
The first version of the Welsh Government mailbox route asked the operator to
paste a Graph **access token** into a repository secret called
`MONITOR_GRAPH_TOKEN`. Following those instructions exactly produced:

    run 1 (that morning)   200 OK, items collected, everything looks fine
    run 2 (next morning)   401, in a log nobody reads
    every run after        401, in a log nobody reads

Graph access tokens last about an hour. The instructions were therefore a bug
that shipped as documentation: they could only ever work once, and the failure
was silent — the Welsh Government section simply went quiet, which looks exactly
like a quiet fortnight in Cardiff Bay.

The fix is OAuth 2.0 client credentials. The app registration holds a client
secret, and the tool exchanges it for a fresh hour-long token at the start of
every run. A client secret lasts months or years rather than an hour, which is
the property an unattended weekday-morning job actually needs.

    POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
         client_id, client_secret,
         scope=https://graph.microsoft.com/.default,
         grant_type=client_credentials

Three design choices worth keeping:

* **Errors are returned, not raised.** The person who reads the output is a
  policy officer, not a developer. `resolve_token` hands back a plain-English
  sentence that says what to do next, and the caller prints it.
* **Nothing configured is not an error.** A deployment inside the NRLA network
  reaches gov.wales directly and needs no mailbox at all. Only a *partial*
  configuration is worth complaining about, because that is someone halfway
  through setting it up who needs to know which piece is missing.
* **The secret never goes in a URL.** Form-encoded POST body only. Query
  strings end up in proxy logs, browser history and error reports. There is a
  test asserting this, because it is the kind of thing a well-meaning
  refactor quietly breaks.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping


log = logging.getLogger(__name__)

LOGIN_ROOT = "https://login.microsoftonline.com"
SCOPE = "https://graph.microsoft.com/.default"

# How long a client secret can live before Azure forces a new one. Microsoft's
# maximum is 24 months; many tenants set 6 or 12.
MAX_SECRET_MONTHS = 24


# The Microsoft error codes that actually occur on a first setup, each with the
# thing the reader should do about it. Raw AADSTS codes are useless to anyone
# who has not seen one before, and searching for them lands on forum threads
# from 2017 about a different product.
AAD_EXPLANATIONS: dict[str, str] = {
    "AADSTS7000215": (
        "the client secret is wrong — check you pasted the secret VALUE, not "
        "the Secret ID. Azure shows both on the same screen and only the Value "
        "is a password. It is shown once, when the secret is created; if it has "
        "been lost, IT must generate a new secret rather than retrieve it."
    ),
    "AADSTS700016": (
        "the application ID is not registered in this tenant — check "
        "MONITOR_GRAPH_CLIENT_ID and MONITOR_GRAPH_TENANT belong to each other. "
        "The usual cause is a client ID from one Azure directory and a tenant "
        "ID from another."
    ),
    "AADSTS900023": (
        "the tenant is not recognised — check MONITOR_GRAPH_TENANT for a stray "
        "space or a truncated paste. It should be a GUID, or the NRLA domain "
        "name."
    ),
    "AADSTS7000222": (
        "the client secret has expired. This is the failure that arrives "
        "months later with no warning; ask IT for a new secret, update "
        "MONITOR_GRAPH_CLIENT_SECRET, and diarise the next expiry."
    ),
}


def describe_expiry_risk() -> str:
    """One sentence for the log and the setup guide about the renewal trap.

    Said out loud on every configured run, because the failure it describes is
    invisible: an expired secret does not announce itself, it just stops the
    Welsh Government half of the page without emptying it.
    """
    return (
        f"Client secrets expire (Microsoft's maximum is {MAX_SECRET_MONTHS} "
        "months). Put a reminder in the calendar a month before the expiry "
        "date IT gave you — when it lapses, this source goes quiet rather than "
        "red, which is the harder failure to notice."
    )


def _explain(payload: Mapping[str, Any], status: int) -> str:
    """Turn a Microsoft token-endpoint error into something actionable."""
    description = str(payload.get("error_description") or "").strip()
    for code, explanation in AAD_EXPLANATIONS.items():
        if code in description:
            return f"Microsoft rejected the sign-in ({code}): {explanation}"

    short = str(payload.get("error") or "").strip()
    first_line = description.splitlines()[0] if description else ""
    detail = " — ".join(p for p in (short, first_line) if p)
    return (
        f"Microsoft rejected the sign-in (HTTP {status})"
        + (f": {detail}" if detail else ".")
        + " Check the four MONITOR_GRAPH_* values against what IT supplied."
    )


def resolve_token(env: Mapping[str, str], session: Any) -> tuple[str, str]:
    """Return ``(token, error)`` for the mailbox route.

    Exactly one of the two is ever non-empty, except in the "nothing
    configured" case where both are empty — which is a legitimate, supported
    deployment (Route B, running from inside the NRLA network) and must not be
    reported as a problem.

    Precedence:

    1. A pasted ``MONITOR_GRAPH_TOKEN``. Kept because it is genuinely useful
       for one manual test from a laptop, and because deleting support for it
       silently would break anyone mid-setup. It logs a warning, because the
       thing it is most likely to be is a leftover from the old instructions,
       and it takes precedence over a correctly configured client secret.
    2. Client credentials — tenant, client ID and client secret.
    3. Nothing. Not an error.
    """
    pasted = (env.get("MONITOR_GRAPH_TOKEN") or "").strip()
    tenant = (env.get("MONITOR_GRAPH_TENANT") or "").strip()
    client_id = (env.get("MONITOR_GRAPH_CLIENT_ID") or "").strip()
    secret = (env.get("MONITOR_GRAPH_CLIENT_SECRET") or "").strip()

    if pasted:
        log.warning(
            "Using the pasted MONITOR_GRAPH_TOKEN. Access tokens last about an "
            "hour, so this works for one manual test and then fails every "
            "morning. For an unattended run set MONITOR_GRAPH_TENANT, "
            "MONITOR_GRAPH_CLIENT_ID and MONITOR_GRAPH_CLIENT_SECRET instead, "
            "and delete this secret — it overrides them.")
        return pasted, ""

    supplied = [bool(tenant), bool(client_id), bool(secret)]
    if not any(supplied):
        # Route B, or simply not set up yet. Silence is correct.
        return "", ""

    if not all(supplied):
        missing = ", ".join(name for name, present in (
            ("MONITOR_GRAPH_TENANT", tenant),
            ("MONITOR_GRAPH_CLIENT_ID", client_id),
            ("MONITOR_GRAPH_CLIENT_SECRET", secret),
        ) if not present)
        return "", (
            "The Welsh Government mailbox route is half configured — "
            f"missing: {missing}. Add the missing repository secret(s), or "
            "remove the others to turn the route off entirely. See "
            "WELSH-GOVERNMENT-SETUP.md stage 3.")

    log.info("%s", describe_expiry_risk())

    url = f"{LOGIN_ROOT}/{tenant}/oauth2/v2.0/token"
    # Form-encoded BODY, never a query string: a client secret in a URL ends up
    # in proxy logs and error reports. `data=` is what keeps it in the body.
    data = {
        "client_id": client_id,
        "client_secret": secret,
        "scope": SCOPE,
        "grant_type": "client_credentials",
    }

    try:
        resp = session.post(url, data=data, timeout=60)
    except Exception as exc:  # noqa: BLE001 - network variety
        return "", (f"Could not reach Microsoft to sign in ({type(exc).__name__}: "
                    f"{exc}). This is a network problem, not a configuration one.")

    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 - a proxy error page, usually
        payload = {}

    if resp.status_code != 200:
        return "", _explain(payload, resp.status_code)

    token = str(payload.get("access_token") or "").strip()
    if not token:
        return "", ("Microsoft accepted the sign-in but returned no access "
                    "token. Check the app registration has the application "
                    "permission Mail.Read with admin consent granted.")
    return token, ""
