# Security Policy

## Supported Versions

CloudMoo is currently maintained on the `main` branch. Security fixes are
applied to the latest release; please run the most recent version.

| Version | Supported          |
| ------- | ------------------ |
| latest  | :white_check_mark: |
| older   | :x:                |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please report them privately via
[GitHub Security Advisories](../../security/advisories/new) on this repository,
or by emailing the maintainers (see the repository profile for the current
contact address).

Please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof of concept
- Affected versions/configurations, if known

You can expect an acknowledgment within a few days. Once the issue is
confirmed, we will work on a fix and coordinate disclosure with you.

## Security Notes for Self-Hosters

- Keep `DJANGO_DEBUG=false` in production.
- Serve the app over HTTPS and set `HTTPS_ENABLED=true` plus
  `DJANGO_CSRF_TRUSTED_ORIGINS`.
- Cloud provider credentials you connect to CloudMoo are stored in the
  database — grant them read-only IAM/API permissions wherever possible
  (see `aws-cloudmoo-readonly-policy.json` for an AWS example).
- Protect `CLOUDMOO_API_KEY`: it authenticates internal monitoring webhooks.
- Set `REGISTRATION_OPEN=false` if you don't want public sign-ups.
