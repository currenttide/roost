---
name: roost-onboard
description: >-
  Guided onboarding to add a machine to the Roost fleet — mint a join token,
  install Claude Code on the worker if missing, and set up authentication, with
  the auth method presented as an explicit user CHOICE (v1 default: copy the
  host's Claude credentials). Use when the user wants to add / join / enroll /
  onboard a new node or worker to the fleet, set up a Pi/box as a Roost worker,
  or fix a worker that can't run agent jobs because claude isn't authenticated.
---

# Roost onboarding

Bring a new machine into the fleet so it can run jobs — including agent
(`claude`) jobs — with minimal fuss. You orchestrate; the user consents to
anything that moves credentials.

## Steps

1. **Confirm the control plane.** `roost ping`. If it fails, the user needs
   `roost serve` running first (note its URL, e.g. `http://<host>:8787`).

2. **Identify the target machine(s)** and how you reach them (SSH alias/IP). Note:
   DHCP IPs drift — confirm the address is current before trusting it.

3. **Choose the auth method — ASK the user** (this is a credential decision, never
   silent). Present the options with AskUserQuestion:
   - **Copy host credentials** *(v1 default)* — copy the operator's
     `~/.claude/.credentials.json` to the worker. Simplest; uses the operator's
     Claude subscription. **Tradeoff to state plainly:** the token is replicated
     to another machine (anyone with file access there can use the account), and
     one subscription driven across many workers may hit rate limits. Best for
     machines the user fully owns on a trusted network.
   - **Per-worker API key** — set `ANTHROPIC_API_KEY` on the worker instead.
     Revocable per-machine, no subscription-token spreading; bills API credits.
   - **Interactive login** — run `claude` once on the worker and log in by hand.
     No copying; needs a human at that machine.
   - **Skip auth** — the worker runs only `command` jobs (no agent jobs) for now.

4. **Mint a single-use join token** on the control plane:
   - Copy-host-creds path (default): `roost enroll-token --label <name>` — the
     server's `--provision-auth` (on by default) makes enrollment install Claude
     Code if missing and copy the host creds. To explicitly opt a token OUT of
     cred copying, the operator runs the server with `--no-provision-auth` or
     mints with a policy `{"provision_claude": false}`.
   - Add `--trust` only if the worker should honor skip-permissions jobs.

5. **Enroll on the worker.** Either the one-liner
   `curl -fsSL <cp-url>/install.sh | sh -s -- <token>` or, if roost is already
   installed there, `roost enroll <token> --url <cp-url> --name <name>`. On the
   copy-creds path this auto-installs Claude Code (if missing) and writes the
   provisioned credentials to `~/.claude/.credentials.json` (0600).
   - API-key path: instead set `ANTHROPIC_API_KEY` in the worker's environment
     (e.g. the service unit / shell profile) and enroll with a
     `{"provision_claude": false}` token so no creds are copied.

6. **Start the worker** and verify: `roost worker` (or `roost service install
   --start`). Then `roost workers` should show it `idle` and advertising
   `claude` in its capabilities once it has started. Confirm with a tiny agent
   job (`roost submit` a trivial `kind: claude` job, or `roost dispatch`) and
   watch it via `/roost-oversee`.

## Notes

- **Security boundary:** copying real credentials across machines is sensitive.
  Always get explicit confirmation for the copy-host path; if the operator's
  environment requires a permission rule to allow the deploy, surface that rather
  than working around it.
- **macOS workers:** Claude stores creds in the Keychain, not
  `~/.claude/.credentials.json`, so the copy-host method is Linux-to-Linux only;
  fall back to API key or interactive login for macOS targets.
- **Future:** the enroll response carries an `auth.method` field, so new methods
  (api_key, OAuth device-flow) slot in without changing the onboarding flow.
