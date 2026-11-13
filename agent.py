#!/usr/bin/env python3
"""A fully autonomous agent for the AIProve bounty arena (aiprove.fun).

Extends the sample agent (see ../sample-agent) into a hands-off loop:

    authenticate -> list open bounties -> enter every open bounty ->
    for "prediction" bounties, submit a rules-based prediction ->
    report what it did.

No human is in the loop. The prediction rules are deliberately simple and
documented (this is a baseline, not an oracle):

  * yes/no direction forecasts (Brier-scored):  answer "yes", confidence 0.55
  * long/short directional calls:              answer "long", confidence 0.55
  * numeric forecasts ("submit a single number") and list forecasts
    ("name five tokens"):                       SKIP — the agent cannot
                                                 price them, and it logs
                                                 the skip instead of
                                                 guessing a number.
  * every other submitKind (repo / link / bughunt / referral): enter
    only; building those entries is out of scope for this loop and is
    logged as such.

Safety: the DEFAULT mode is --dry-run, which uses only public endpoints
(no keypair, no writes) and prints the actions it would take. Live mode
requires an explicit --live flag and the agent's own keypair file
(AGENT_KEY_FILE, default ./agent.key next to this script; created with
mode 0600 if missing). Entering is idempotent server-side (a repeat
enter is answered 409, treated as success), and prediction submits are
skipped for bounties the agent has already submitted to, so re-running
the loop is safe.

Usage:
    python3 agent.py              # dry-run plan (default)
    python3 agent.py --live       # actually enter + submit predictions

Environment: API_BASE_URL, AGENT_KEY_FILE, AGENT_HANDLE.
Dependencies: eth-account (live mode only; --dry-run is pure stdlib).
"""
import argparse
import gzip
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request

API_BASE = os.environ.get("API_BASE_URL", "https://aiprove.fun").rstrip("/")
KEY_FILE = os.environ.get(
    "AGENT_KEY_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.key"),
)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) aiprove-best-agent/1.0"
CONFIDENCE = 0.55  # baseline conviction for every rules-based prediction


# ---------------------------------------------------------------- HTTP ----
def api(method, path, body=None, token=None, attempts=3):
    """JSON call with retries on transient transport failures.

    Same transport lessons as the sample agent: browser-like UA (the API
    is behind Cloudflare), gzip negotiation (smaller wire payloads drop
    less often), retries on dropped/truncated connections and 502-504.
    """
    data = json.dumps(body).encode() if body is not None else None
    last_exc = None
    for attempt in range(attempts):
        req = urllib.request.Request(API_BASE + path, data=data, method=method)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept-Encoding", "gzip")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode())
        except urllib.error.HTTPError as e:
            if e.code in (502, 503, 504) and attempt < attempts - 1:
                last_exc = e
            else:
                raise RuntimeError(f"{method} {path} -> HTTP {e.code}: "
                                   f"{e.read().decode(errors='replace')[:200]}") from e
        except (urllib.error.URLError, http.client.HTTPException,
                ConnectionError, TimeoutError) as e:
            last_exc = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{method} {path} failed after {attempts} attempts: {last_exc}")


# -------------------------------------------------------------- identity ----
def load_or_create_account():
    from eth_account import Account
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            return Account.from_key(f.read().strip())
    acct = Account.create()
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(acct.key.hex())
    return acct


def authenticate(acct):
    from eth_account.messages import encode_defunct
    for handshake in range(2):
        challenge = api("GET", f"/api/auth/challenge?address={acct.address}")["challenge"]
        signature = acct.sign_message(encode_defunct(text=challenge)).signature.hex()
        if not signature.startswith("0x"):  # API wants the 0x-prefixed form
            signature = "0x" + signature
        try:
            return api("POST", "/api/auth/verify",
                       {"address": acct.address, "signature": signature})["token"]
        except RuntimeError as e:
            if "no active challenge" in str(e) and handshake == 0:
                continue
            raise


# --------------------------------------------------------------- planner ----
def plan_prediction(bounty, detail):
    """Decide the prediction action for one bounty.

    Returns ("submit", payload) or ("skip", reason).
    """
    if detail.get("submitKind") != "prediction":
        return ("skip", f"submitKind={detail.get('submitKind')}: not a prediction bounty")
    text = f"{bounty.get('title', '')} {bounty.get('brief', '')}".lower()
    if "single number" in text or "one number" in text:
        return ("skip", "numeric forecast: agent cannot price it")
    if "five" in text and ("name" in text or "top" in text):
        return ("skip", "list forecast: agent cannot rank candidates")
    if "long or short" in text:
        return ("submit", {"answer": "long", "confidence": CONFIDENCE})
    if "yes or no" in text or "probability" in text or "brier" in text:
        return ("submit", {"answer": "yes", "confidence": CONFIDENCE})
    return ("skip", "unrecognised prediction shape")


def plan_actions():
    """Fetch the board (public endpoints) and plan every action."""
    bounties = api("GET", "/api/bounties?status=open")["bounties"]
    plans = []
    for b in bounties:
        try:
            # Detail payloads embed every entry and submission (can exceed
            # 100 KB), so they need more retry patience than small calls.
            detail = api("GET", f"/api/bounties/{b['slug']}", attempts=6)
        except RuntimeError as e:
            plans.append((b, ("skip", f"detail fetch failed: {e}"), None))
            continue
        action, payload_or_reason = plan_prediction(b, detail)
        plans.append((b, (action, payload_or_reason),
                      detail.get("submitKind")))
    return plans


# ----------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser(description="Autonomous AIProve agent")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="print intended actions without submitting (default)")
    mode.add_argument("--live", action="store_true",
                      help="actually enter bounties and submit predictions")
    ap.add_argument("--handle", default=os.environ.get("AGENT_HANDLE"))
    args = ap.parse_args()
    live = bool(args.live)

    print(f"mode: {'LIVE' if live else 'DRY-RUN (no writes)'}")
    plans = plan_actions()
    print(f"open bounties: {len(plans)}")

    token = None
    already_submitted = set()
    if live:
        acct = load_or_create_account()
        print(f"agent address: {acct.address}")
        token = authenticate(acct)
        print("authenticated")
        mine = api("GET", "/api/me/submissions", token=token)
        already_submitted = {s["bountySlug"] for s in mine.get("submissions", mine)
                              if isinstance(s, dict)}

    n_enter = n_submit = n_skip = 0
    for b, (action, payload), kind in plans:
        slug = b["slug"]
        print(f"\n[{slug}] reward={b['amt']} {b['unit']} lane={b['lane']} submitKind={kind}")
        if live:
            try:
                api("POST", f"/api/bounties/{slug}/enter",
                    {"handle": args.handle} if args.handle else {}, token=token)
                print("  ENTER  done")
            except RuntimeError as e:
                if "already entered" in str(e):
                    print("  ENTER  already entered (ok)")
                else:
                    print(f"  ENTER  FAILED: {e}")
                    continue
        else:
            print("  ENTER  would POST /api/bounties/%s/enter" % slug)
        n_enter += 1

        if action == "submit" and slug in already_submitted:
            print(f"  SKIP   already submitted in a previous run")
            n_skip += 1
        elif action == "submit":
            if live:
                r = api("POST", f"/api/bounties/{slug}/submit",
                        {"prediction": payload, "handle": args.handle}, token=token)
                print(f"  SUBMIT prediction={payload} -> id={r.get('id')}")
            else:
                print(f"  SUBMIT would POST prediction={payload}")
            n_submit += 1
        else:
            print(f"  SKIP   submit: {payload}")
            n_skip += 1

    print(f"\nsummary: enter={n_enter} submit={n_submit} skip={n_skip} "
          f"({'live' if live else 'dry-run — nothing was written'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
