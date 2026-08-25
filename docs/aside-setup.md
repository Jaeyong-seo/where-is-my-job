# Aside setup for application automation

## Current boundary

Aside is not a real-provider integration in this release. The runtime is fixture-only and has zero real-provider authority. An Aside probe checks local CLI availability; it is not a release gate, login proof, pin-completeness proof, or submission authority.

Use a fresh, dedicated Aside browser context only. Do not reuse the daily/default context or import its cookies, history, extensions, or password store.

Official references:

- [Aside download](https://aside.com/download)
- [Aside getting started](https://docs.aside.com/help/get-started)
- [Aside developer documentation](https://docs.aside.com/help/developers)
- [Aside security and permissions](https://docs.aside.com/help/security.md)
- [Aside password access](https://docs.aside.com/help/passwords.md)
- [Aside tasks and Incognito](https://docs.aside.com/help/tasks.md)

## Install and probe

Install Aside using the official instructions for the installed version. First use the isolated `AUTOMATION_DB` and `AUTOMATION_CATALOG` setup in [the fixture checks](application-automation.md#install-and-fixture-checks). `aside-doctor` opens or creates that SQLite database, applies migrations, parses the catalog, and synchronizes catalog roles before it probes `aside` from `PATH` with `--version`, `account status`, `mcp --help`, and `repl --help`.

```bash
uv run python tools/apply_service.py --db "$AUTOMATION_DB" --catalog "$AUTOMATION_CATALOG" aside-doctor
```

The installed CLI probe has been run separately. It establishes only the local CLI probe result; it does not enable provider runtime wiring. Each expected pin is independently enforced: `--expected-version` checks the exact CLI version, and `--expected-executable-sha256` checks the executable hash. `--enforce-pins` requires the complete pair; it does not make either individual expected value conditional on the other.

```bash
uv run python tools/apply_service.py --db "$AUTOMATION_DB" --catalog "$AUTOMATION_CATALOG" aside-doctor \
  --expected-version "<approved-version>" \
  --expected-executable-sha256 "<approved-sha256>" \
  --enforce-pins
```

This is an enforced local pin check, not a real-provider release gate. Do not substitute an alias or wrapper, and do not infer additional CLI flags or context-management commands; syntax not shown by the installed CLI help is unavailable.

## Fixture MCP contract

`AsideMcpAdapter` is an implemented lower-level, fixture-verified runtime contract. It is currently unwired: no supported service or CLI surface invokes it, and provider runtime wiring remains disabled. Its fixture/contract coverage verifies pinned executable, CLI and MCP server version, MCP protocol version, exact `repl` tool schema, fixture script digest, domain, and page/form fingerprints before accepting a result. The deterministic fixture script does not navigate or contact a provider.

Candidate values and synthetic fixture values do not appear in argv or REPL source. Aside resolves an opaque filename inside its private agent-session root. The adapter verifies that root remains under `~/.aside`, is owned by the current user, and is not group- or world-writable; it then creates the payload with exclusive, no-follow, mode-`0600` semantics. Cleanup is attempted in `finally` after success or failure. A cleanup error is surfaced and can leave a mode-`0600` payload; abrupt process or host termination can also leave a residual payload. A later MCP call reaps matching stale payloads older than `max(timeout, 30s)`. Result parsing accepts one bounded, typed result and fails closed on MCP errors, extra output, schema drift, or timeouts.

```bash
uv run pytest -q tests/application_automation/test_aside_executor.py
```

That suite is unit/contract coverage using deterministic fixtures and mocked MCP transport, not an installed Aside MCP end-to-end verification. It does not prove an employer submission or permit a real-provider script.

## Future-pilot-only provider login and challenges

Provider login/setup is future-pilot-only and is not enabled by this fixture release. For any approved future pilot, use only the installed Aside UI and the documented controls:

1. Create or select a fresh dedicated context; set AI password access to `Never`.
2. Disable autofill, or use Incognito, before any provider login.
3. Review browser and network rules for the exact provider/employer domains and deny broad repository/filesystem access. The material route is authenticated and catalog-bound; do not describe it as opaque.
4. The candidate manually signs in and completes MFA, CAPTCHA, recovery, consent, security, and account-selection steps.
5. On login/session expiry, CAPTCHA, MFA, rate limit, security challenge, unexpected prompt, redirect, domain, or form change, stop and preserve only safe local evidence. Do not automate, delegate, bypass, spoof, or retry around the control.

Approved candidate resumes and application materials are the narrow repository PII carve-out. Credentials, cookies, session secrets, MFA codes, bootstrap tokens, raw assertion values, provider payloads, and account identifiers must not appear in source, scripts, command history, logs, screenshots, tickets, or repository files.

## Future release requirement

The detailed future-release checklist is canonical in [Local application automation](application-automation.md#future-real-provider-release-checklist). Until it is satisfied, real-provider operations and any corresponding Aside execution syntax remain unavailable.