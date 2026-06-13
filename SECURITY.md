# Security Policy

The Azure Support Agent connects to live Azure tenants and handles credentials,
tokens, and other secrets. We take security issues seriously and appreciate
responsible disclosure.

## Supported Versions

Only the latest released version receives security updates.

| Version | Supported |
| ------- | --------- |
| latest  | ✅        |
| older   | ❌        |

## Reporting a Vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately through GitHub's built-in private vulnerability reporting:

1. Go to the **Security** tab of this repository.
2. Click **Report a vulnerability** (GitHub Security Advisories).
3. Provide a clear description, reproduction steps, affected version/commit,
   and any relevant logs (with secrets redacted).

If you cannot use GitHub's private reporting, open a minimal issue asking a
maintainer to contact you — **without** disclosing the vulnerability details.

## What to Include

- A description of the issue and its impact.
- Steps to reproduce (a proof of concept if possible).
- The affected version, image tag, or commit SHA.
- Your assessment of severity, if any.

## Our Commitment

- We will acknowledge your report as soon as we can.
- We will investigate and keep you informed of progress.
- We will credit you in the advisory once a fix is released, unless you prefer
  to remain anonymous.

## Scope & Safe Handling

- **Never** include real secrets (API keys, client secrets, tokens, connection
  strings) in a report — redact them.
- The application runs **read-only by default**; please respect that posture
  when testing and do not attempt to access data you are not authorized to.
- Do not run tests against systems or tenants you do not own or have explicit
  permission to test.

Thank you for helping keep the project and its users safe.
