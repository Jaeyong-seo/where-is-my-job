# Application automation threat model

## Assets and boundaries

| Asset | Required boundary |
| --- | --- |
| Approved candidate resumes and materials | May exist in the repository as the narrow candidate-approved application-material PII carve-out; use only candidate-approved material. |
| Credentials, cookies, session secrets, MFA codes, bootstrap tokens, raw assertion values, provider payloads, and account identifiers | Must not appear in repository files, source, scripts, argv, shell history, logs, screenshots, or tickets. |
| Local fixture records and evidence | Local-only diagnostic evidence; never proof of a real-provider action or employer submission. |
| Aside | Dedicated context only; human-controlled login, MFA, CAPTCHA, recovery, consent, and security controls. |

The current runtime is fixture-only and has zero real-provider authority. The static `file://` dashboard is disconnected browser-local scratch state. A real Aside CLI doctor can enforce constructor-owned version and executable-SHA pins, but that local readiness evidence is not provider authority or a real-release gate.

## Fail-closed controls

- **Secret exposure:** Bootstrap tokens are supplied only through a mode-`0600` token file or generated into a new mode-`0600` file. No argv token option or service-generated one-time bootstrap URL is currently available.
- **Provider/form drift:** Any future release must enforce exact provider, form, script, Aside version, and executable-SHA pins. The pinned CLI doctor proves only the local executable boundary; it cannot replace provider/form capability, policy, sandbox, and release gates.
- **Unsafe answers:** Future automation may use only a candidate-confirmed assertion snapshot. Unknown, ambiguous, legal, address, demographic, salary, or authorization questions require a human checkpoint; never guess or infer.
- **Login and security controls:** The candidate alone signs in and completes MFA, CAPTCHA, recovery, consent, account-selection, rate-limit, and security challenges in the dedicated context. Never solve, bypass, spoof, automate, or blindly retry them.
- **Ambiguous dispatch:** `manualfollowup` requires human resolution. Never infer an employer submission from `completed`, `applied`, a fixture receipt, an Aside probe, or a dashboard status. Never automatically retry a possible submission.
- **Status/projection claims:** `ProjectionStore` is an implemented lower-level library that stages hash-verified immutable JSON/HTML releases and manifests and updates an atomic current pointer. It is not integrated or authorized as a supported status-import/cutover surface; its artifacts are noncanonical. Status import, publication, and cutover remain unavailable.

## Checkpoints and evidence

Fixture `doctor`, fixture `aside-doctor`, and fixture service records are limited to deterministic local behavior. They can show that a local fixture path ran; they cannot show provider login, form inspection, acceptance, dispatch, receipt, or employer submission.

On a checkpoint or any unexpected state, stop. Preserve only safe local diagnostic evidence and require the candidate to complete the necessary provider action manually. Raw assertion values, credentials, session data, and provider payloads are never evidence artifacts.

## Future release condition

The detailed future-release checklist is canonical in [Local application automation](application-automation.md#future-real-provider-release-checklist). Until it is satisfied, real-provider operation remains denied.