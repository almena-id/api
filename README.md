# almena-api

The backend of the Almena Network registry portal: a [FastAPI](https://fastapi.tiangolo.com) service on PostgreSQL, consumed by [registry](../registry).

Built with Python 3.13, [uv](https://docs.astral.sh/uv/), SQLAlchemy 2 (async, asyncpg), Alembic for migrations and pydantic-settings for configuration.

## Quick start

Needs [uv](https://docs.astral.sh/uv/), [Task](https://taskfile.dev) and Docker.

```bash
task init   # .env from .env.example, with a random database password
task up     # PostgreSQL + migrations + API in Docker
task health # {"status":"ok","version":"0.1.0","database":"ok"}
```

There are no passwords: signing up and signing in are the same flow, an email with a six-digit code. In development every email lands in [Mailpit](https://mailpit.axllent.org) at `http://localhost:8025` (`task up` starts it, `task dev` too).

The API is published at `https://api.almena.network`, for the portal at `https://registry.almena.network`; locally it answers at `http://localhost:8000`. For development, `task dev` runs it locally with auto-reload against PostgreSQL in Docker, and serves the interactive [Scalar](https://scalar.com) reference at `http://localhost:8000/docs`.

## Configuration

All settings are `REGISTRY_*` environment variables, read from the environment or `.env`; [.env.example](.env.example) lists and explains every one.

| Variable | Default | |
|---|---|---|
| `REGISTRY_ENVIRONMENT` | `development` | `production` hides `/docs` and `/openapi.json` (the Docker image's default) |
| `REGISTRY_PUBLIC_URL` | `https://api.almena.network` | Public origin, advertised as the server in the OpenAPI document |
| `REGISTRY_CORS_ORIGINS` | `["https://registry.almena.network"]` | Origins allowed by CORS, as a JSON list (`.env.example` adds `http://localhost:3000` for development) |
| `REGISTRY_DB_HOST` / `REGISTRY_DB_PORT` | `localhost` / `5432` | PostgreSQL server |
| `REGISTRY_DB_NAME` / `REGISTRY_DB_USER` / `REGISTRY_DB_PASSWORD` | `registry` / `registry` / — | Database and credentials; Compose creates them on the first start |
| `REGISTRY_SESSION_TTL_HOURS` | `168` | How long a portal sign-in lasts |
| `REGISTRY_LOGIN_CODE_TTL_MINUTES` / `REGISTRY_LOGIN_CODE_MAX_ATTEMPTS` | `10` / `5` | Emailed sign-in codes: lifetime and wrong guesses allowed |
| `REGISTRY_SMTP_HOST` / `REGISTRY_SMTP_PORT` | `localhost` / `1025` | SMTP server (Mailpit in development) |
| `REGISTRY_SMTP_USERNAME` / `REGISTRY_SMTP_PASSWORD` / `REGISTRY_SMTP_STARTTLS` | — / — / `false` | SMTP credentials and STARTTLS, for a real server |
| `REGISTRY_MAIL_FROM` | `Almena Registry <no-reply@almena.network>` | Sender of the emails |
| `REGISTRY_PORTAL_URL` | `https://registry.almena.network` | The portal; providers send the browser back to `{portal}/auth/{provider}/callback` |
| `REGISTRY_GOOGLE_CLIENT_ID` / `REGISTRY_GOOGLE_CLIENT_SECRET` | — | Sign in with Google (off while empty) |
| `REGISTRY_MICROSOFT_CLIENT_ID` / `REGISTRY_MICROSOFT_CLIENT_SECRET` / `REGISTRY_MICROSOFT_TENANT` | — / — / `common` | Sign in with Microsoft (off while empty) |
| `REGISTRY_GITHUB_CLIENT_ID` / `REGISTRY_GITHUB_CLIENT_SECRET` | — | Sign in with GitHub (off while empty) |
| `REGISTRY_APPLE_CLIENT_ID` / `REGISTRY_APPLE_TEAM_ID` / `REGISTRY_APPLE_KEY_ID` / `REGISTRY_APPLE_PRIVATE_KEY` | — | Sign in with Apple: Services ID, team, key id and the `.p8` key (PEM) (off while empty) |
| `REGISTRY_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |

## Endpoints

| | |
|---|---|
| `GET /health` | Liveness: the process is up |
| `GET /health/ready` | Readiness: `503` while the database is unreachable |
| `POST /api/v1/auth/code` | Email a six-digit sign-in code (`locale`: `en` or `es`); `503 mail_unavailable` |
| `POST /api/v1/auth/verify` | Exchange the code for a session, creating the account the first time — with a tenant of its own named "Tenant of {email}" in `locale` (`en`, `es`: "Tenant de …"), unless it was invited into one; `401 invalid_code`, `429 too_many_attempts` |
| `GET /api/v1/auth/providers` | Social sign-in providers (`google`, `microsoft`, `apple`, `github`) and which are configured |
| `POST /api/v1/auth/oauth/{provider}/start` | Begin a social sign-in: the provider URL to send the browser to, and the `state` to keep; `409 provider_disabled` |
| `POST /api/v1/auth/oauth/{provider}/callback` | Finish it with the `code` and `state` the provider sent back; creates the account the first time; `400 invalid_state`, `403 email_unverified`, `502 provider_error` |
| `GET /api/v1/auth/me` | The signed-in account (`Authorization: Bearer <token>`) |
| `PATCH /api/v1/auth/me` | Change the signed-in account's `alias` (up to 100 characters; blank clears it) |
| `POST /api/v1/auth/logout` | End the session behind the bearer token |
| `GET /api/v1/tenants` | The signed-in user's tenants, oldest first, with the user's `role` in each (`name` is `null` until one is given) |
| `GET /api/v1/tenants/{id}` | A tenant's details: `name`, the user's `role`, its own `identity` (created with it, named like it, renamed with it), and its mediator (`mediator_url`, `mediator_did`), the mailbox of all its issuers and verifiers |
| `PATCH /api/v1/tenants/{id}` | Change `name` and/or `mediator_url` (admins only; `null` or empty removes the mediator). The mediator is checked: its origin must serve a `did:web` document with a `DIDCommMessaging` service, HTTPS except on loopback; `422` with `name_required`, `mediator_invalid`, `mediator_insecure`, `mediator_unreachable` or `mediator_not_a_mediator` |
| `GET /api/v1/tenants/{id}/issuers` (and `/verifiers`, `/identities`) | The tenant's items, newest first: `{items, next_cursor, total}`; `limit` (1–100, 20) and `cursor` from the previous page; `404 tenant_not_found` for a tenant the user is not in |
| `POST /api/v1/tenants/{id}/issuers` (and `/verifiers`) | Register one: `name` (1–200), optional `description`, and the identity it acts as: `identity_id` of one of the tenant's identities, or left out to create one named like it; `422 identity_not_found`. Issuers and verifiers come back with their `identity` |
| `POST /api/v1/tenants/{id}/identities` | Register an identity: `name` only, until the DID method and key custody are decided. Identities are the tenant's register of DIDs, and come back with `used_by`: the tenant, issuers and verifiers that act as them |
| `GET /api/v1/tenants/{id}/members` | Who belongs (`status: member`) and who is invited but has not signed in yet (`status: invited`), each with `role` (`admin` or `member`) |
| `POST /api/v1/tenants/{id}/invitations` | Invite by `email` with a `role` (admins only; `locale` for the email): they join the next time they sign in with that address. Inviting again changes the role and resends; `403 not_admin`, `409 already_member`, `503 mail_unavailable` |
| `GET /docs`, `GET /openapi.json` | API reference ([Scalar](https://scalar.com)) and the OpenAPI document, generated from the code (not in production) |

## Social sign-in

Google, Microsoft, Apple and GitHub run the authorization code flow with PKCE and `state` (and a `nonce` for the OpenID Connect ones). Each is off until its variables are set. Register this redirect URI with each provider, with the portal's origin: `https://registry.almena.network/auth/{google|microsoft|apple|github}/callback` (for development, `http://localhost:3000/…`; Apple accepts only HTTPS).

An account is its email, however it signs in. A provider's account joins or creates the account for its email only when the provider vouches for the address: Google's and Apple's `email_verified`, GitHub's primary verified email, and for Microsoft a personal account or a work account whose ID token carries the optional claim `xms_edov` (add it in the app registration's *Token configuration*).

## Database migrations

The schema is managed with Alembic; `task up` applies pending migrations (the `migrate` service) before starting the API.

```bash
task db:revision -- "add entries"   # autogenerate a migration from the models
task db:migrate                     # apply it
task db:shell                       # psql on the Docker database
```

Review every autogenerated migration before committing it.

## Development

`task --list` shows every task. Before sending a change, `task check` (formatting, ruff, mypy, tests) must pass; see [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) for the code layout.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md). Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
