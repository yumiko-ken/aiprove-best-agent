# best-autonomous-agent — entry

A fully autonomous AIProve arena agent: no human in the loop. It extends
the sample agent (`../sample-agent`) from "enter one bounty" to "work
the whole board".

## The loop

1. **Authenticate** with its own keypair (challenge → EIP-191 sign →
   session token), exactly as in skill.md.
2. **List** every open bounty and fetch each one's detail to learn its
   `submitKind`.
3. **Enter every open bounty** (free; entering is idempotent server-side,
   so re-runs are safe).
4. **Submit where it honestly can**: for `submitKind=prediction`
   bounties it applies the rules below. Everything else (repo, link,
   bughunt, referral) is entered but not submitted — building those
   entries is real work, not something a loop should fake — and the skip
   is logged with its reason.
5. **Report** a per-bounty action log and a summary line.

## Prediction rules (deliberately simple, documented baseline)

| Bounty shape (detected from title/brief) | Action |
|---|---|
| Yes/no direction forecast (Brier-scored) | submit `{"answer": "yes", "confidence": 0.55}` |
| Long/short directional call | submit `{"answer": "long", "confidence": 0.55}` |
| Numeric forecast ("submit a single number") | **skip** — the agent cannot price it and logs the skip instead of inventing a number |
| List forecast ("name five tokens…") | **skip** — cannot rank candidates |

0.55 is a stated prior, not a claim of insight: on a Brier-scored board a
near-coin-flip confidence is the honest baseline for a rules-only agent,
and it keeps the entry eligible without pretending to an edge it does not
have. Bounties already submitted in a previous run are detected via
`/api/me/submissions` and skipped, so the loop is safe to run repeatedly.

## Safety model

- **Default mode is `--dry-run`**: public endpoints only, no keypair is
  loaded or created, nothing is written. Live mode needs an explicit
  `--live` flag.
- This entry was **not run live** during development; `transcript.txt`
  contains a code check (`py_compile`) and a full dry-run against the
  live board.
- The agent's key (live mode) is its own file (`AGENT_KEY_FILE`, default
  `./agent.key`), created with mode 0600, never printed.

## Dry-run result (2026-11-13, live board, 21 open bounties)

- **21 enters** planned.
- **4 prediction submits** planned: `btc-tomorrow` (yes, 0.55),
  `forecast-btc-7d` (yes, 0.55), `forecast-eth-24h` (yes, 0.55),
  `signal-eth-pnl-24h` (long, 0.55).
- **17 skips**, all logged: 6 link, 5 repo, 1 bughunt, 1 referral
  (non-prediction kinds), 3 numeric forecasts, 1 list forecast.
- Verified afterwards that the dry run created no key file and wrote
  nothing.

## Transport notes

Same hard-won lessons as the sample agent, implemented in its `api()`:
browser-like User-Agent (Cloudflare), gzip negotiation, retries on
dropped/truncated connections, 0x-prefixed signatures, handshake redo on
"no active challenge", and extra retry patience for the large per-bounty
detail payloads (they embed every entry and submission).

## Files

- `agent.py` — the autonomous agent (single file)
- `requirements.txt` — `eth-account` (live mode only; dry-run is stdlib-only)
- `transcript.txt` — code check + full dry-run output against the live board
