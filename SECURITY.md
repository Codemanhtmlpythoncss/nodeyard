# Security policy

nodeyard runs as root on your machines and manages their network, firewall
and cluster, so security problems in it matter. Thank you for reporting
them responsibly.

## Reporting a vulnerability

**Please don't open a public issue for security problems.**

Report privately through GitHub: go to the repository's **Security** tab
and choose **Report a vulnerability**
(<https://github.com/Codemanhtmlpythoncss/nodeyard/security/advisories/new>).

Include, as far as you can:

- what the problem is and what an attacker could do with it,
- the nodeyard version (`nodeyard --version`) and your distribution,
- the steps to reproduce it, and
- a support bundle or log excerpt **with any remaining secrets removed**.

## What to expect

- An acknowledgement within 7 days.
- An assessment and a plan (fix, mitigation, or an explanation if it is not
  a vulnerability) within 30 days.
- A fixed release and a published advisory crediting you (unless you'd
  rather not be named). Please give us a reasonable time to ship the fix
  before disclosing it publicly; 90 days is the default.

## Supported versions

Only the latest release receives security fixes. Update with
`sudo nodeyard update`.

## What counts

Examples of things we want to hear about:

- secrets (join tokens, passwords, API keys) appearing in logs, output,
  `--dry-run`/`--json` plans, command lines or world-readable files,
- a way to run commands as root that the user didn't ask for,
- downloads that aren't verified, or verification that can be bypassed,
- the (upcoming) dashboard or agent accepting unauthenticated or forged
  requests, or arbitrary commands.

The security design is described in [docs/security.md](docs/security.md).
