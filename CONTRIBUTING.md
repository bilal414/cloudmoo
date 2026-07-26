# Contributing to CloudMoo

Thanks for your interest in contributing!

## Getting Started

1. Fork the repository and clone your fork.
2. Copy `.env.example` to `.env` and fill in the required values.
3. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
4. Run the migrations and start the dev server:
   ```bash
   python manage.py migrate
   python manage.py runserver
   ```

## Guidelines

- Keep pull requests focused — one feature or fix per PR.
- Follow the existing code style (plain Django, no framework rewrites).
- Add or update tests in `tests/` when changing behavior.
- Do not commit secrets, credentials, or personal data. Configuration belongs
  in environment variables; see `.env.example`.
- When adding a new cloud provider, follow the established pattern in
  `apps/console/cloud/<provider>/` (models, forms, views, urls, templates).

## Reporting Bugs

Open a GitHub issue with steps to reproduce, expected vs actual behavior, and
your environment (OS, Python, Django, database).

## Security Issues

Please see [SECURITY.md](SECURITY.md) — do not open public issues for
security vulnerabilities.

## License

By contributing, you agree that your contributions will be licensed under the
MIT License.
