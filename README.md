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

The API is published at `https://api.almena.id`, for the portal at `https://registry.almena.id`; locally it answers at `http://localhost:8000`. For development, `task dev` runs it locally with auto-reload against PostgreSQL in Docker, and serves the interactive [Scalar](https://scalar.com) reference at `http://localhost:8000/docs`.

## The root authority

Almena is the root: the tenant that governs the network. It is an ordinary tenant, created once at install with its first admin, who joins by signing in to the portal with that address:

```bash
task root -- --admin you@almena.id
```

(`registry-api init-root --admin <email> [--name Almena] [--mediator-url https://mediator.almena.id]` in a container.) A second run fails. It comes with one mediator, `Almena Mediator`, at `https://mediator.almena.id` (or `--mediator-url`), a draft until an admin publishes it. Its identity is the identity domain's own DID (`did:webvh:{SCID}:almena.id`, also served as `did:web:almena.id`), at `/.well-known/`. The server holds no key for it: keys arrive when its admins sign from their wallets.

## Configuration

All settings are `REGISTRY_*` environment variables, read from the environment or `.env`; [.env.example](.env.example) lists and explains every one.

| Variable | Default | |
|---|---|---|
| `REGISTRY_ENVIRONMENT` | `development` | `production` hides `/docs` and `/openapi.json` (the Docker image's default) |
| `REGISTRY_PUBLIC_URL` | `https://api.almena.id` | Public origin, advertised as the server in the OpenAPI document |
| `REGISTRY_DID_URL` | `https://almena.id` | The identity domain: identities' `did:web` DIDs are made from it and resolve there, so it must proxy `/ids/` and `/.well-known/` to the API |
| `REGISTRY_CORS_ORIGINS` | `["https://registry.almena.id"]` | Origins allowed by CORS, as a JSON list (add `http://localhost:3000` for the portal's `task dev`) |
| `REGISTRY_WELL_KNOWN_DIR` | — | Directory with the origin's `did-configuration.json`, served at `/.well-known/` (off while empty; Compose mounts `./well-known`) |
| `REGISTRY_DB_HOST` / `REGISTRY_DB_PORT` | `localhost` / `5432` | PostgreSQL server |
| `REGISTRY_DB_NAME` / `REGISTRY_DB_USER` / `REGISTRY_DB_PASSWORD` | `registry` / `registry` / — | Database and credentials; Compose creates them on the first start |
| `REGISTRY_SESSION_TTL_HOURS` | `168` | How long a portal sign-in lasts |
| `REGISTRY_LOGIN_CODE_TTL_MINUTES` / `REGISTRY_LOGIN_CODE_MAX_ATTEMPTS` | `10` / `5` | Emailed sign-in codes: lifetime and wrong guesses allowed |
| `REGISTRY_SMTP_HOST` / `REGISTRY_SMTP_PORT` | `localhost` / `1025` | SMTP server (Mailpit in development) |
| `REGISTRY_SMTP_USERNAME` / `REGISTRY_SMTP_PASSWORD` / `REGISTRY_SMTP_STARTTLS` | — / — / `false` | SMTP credentials and STARTTLS, for a real server |
| `REGISTRY_MAIL_FROM` | `Almena Registry <no-reply@almena.network>` | Sender of the emails |
| `REGISTRY_PORTAL_URL` | `https://registry.almena.id` | The portal; providers send the browser back to `{portal}/auth/{provider}/callback` |
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
| `GET /api/v1/auth/me` | The signed-in account (`Authorization: Bearer <token>`), `email` is `null` for an account without one |
| `PATCH /api/v1/auth/me` | Change the signed-in account's `alias` (up to 100 characters; blank clears it) |
| `POST /api/v1/auth/logout` | End the session behind the bearer token |
| `GET /api/v1/auth/me/ways-in` | The account's ways in: its `email` (or `null`) and the provider `accounts` linked to it |
| `POST /api/v1/auth/me/email` | Link an `email` with the `code` sent to it by `POST /auth/code` (replaces the one there was, accepts its invitations); `status` `linked` or `taken` — see below; `401 invalid_code`, `429 too_many_attempts` |
| `DELETE /api/v1/auth/me/email` | Unlink the email; `409 last_way_in` |
| `POST /api/v1/auth/me/accounts/{provider}/start` | Begin linking a provider account (same answer as the sign-in start) |
| `POST /api/v1/auth/me/accounts/{provider}/callback` | Finish it; `status` `linked` or `taken`; `400 invalid_state` (also for a sign-in flow, or another account's), `502 provider_error` |
| `DELETE /api/v1/auth/me/accounts/{id}` | Unlink a provider account; `404 account_not_found`, `409 last_way_in` |
| `POST /api/v1/auth/me/move` | With the `ticket` of a `taken` answer: hand this empty account's ways in to the owner, delete it and sign in there (a new session); `400 invalid_ticket`, `409 not_empty` |
| `POST /api/v1/auth/wallet/requests` | Ask an Almena wallet to sign in (`purpose` `sign_in`) or to link to the signed-in account (`link`, needs the bearer token): the `request_uri`, the `deep_link` (`almena://auth?request_uri=…`) to show as a QR code, and the `poll` secret the portal keeps; lives five minutes |
| `GET /api/v1/auth/wallet/requests/{id}` | The request as the wallet reads it: `client_id` (the portal), `nonce`, `response_uri`, `purpose`; `404 request_not_found`, `410 request_expired` |
| `POST /api/v1/auth/wallet/requests/{id}/response` | The wallet's `id_token` (`direct_post`, form or JSON): a JWS signed EdDSA by the `did:key` in `iss`/`sub`, for `aud` the portal and the request's `nonce`; `400 invalid_token`/`invalid_did`, `409 already_answered` |
| `POST /api/v1/auth/wallet/requests/{id}/result` | Poll with `poll` (and, for a link, the account's bearer token): `pending`, then once `signed_in` with a `session` (a new wallet signs up an account with no email and an unnamed tenant), `linked`, or `taken` (with a `move_ticket` when this account is empty); `403 wrong_poll` |
| `POST /api/v1/tenants/{id}/identities/{identity_id}/sign` | Admins with a linked wallet that is one of the update keys: a wallet request (`purpose` `sign`, same answer as `POST /auth/wallet/requests`) carrying the identity's next did:webvh log entry; the wallet answers with a `proof` and the entry is published; the poll answers `signed`; `403 not_a_signer`, `409 up_to_date`/`no_signers` |
| `GET /ids/{slug}/did.jsonl` | An identity's did:webvh log (JSON Lines); `404` while pending or a draft |
| `GET /ids/{slug}/did.json` | Its current document under its did:web name, with `alsoKnownAs` its did:webvh DID |
| `GET /ids/{slug}/whois.vp` | The did:webvh `#whois` (`application/vp`): a published issuer's, verifier's or mediator's endorsement by its tenant; `404` otherwise |
| `GET /.well-known/did.jsonl` | The root tenant's log (`did:webvh:{SCID}:almena.id`); `did.json` beside it is `did:web:almena.id` |
| `GET /api/v1/tenants` | The signed-in user's tenants, oldest first, with the user's `role` in each (`name` is `null` until one is given) |
| `GET /api/v1/tenants/{id}` | A tenant's details: `name`, the user's `role`, its own `identity` (created with it, named like it, renamed with it), and the `mediator` its identity receives messages through (one of its mediators, or `null`) |
| `PATCH /api/v1/tenants/{id}` | Change `name` and/or `mediator_id` (admins only; `null` removes the mediator); `422` with `name_required` or `mediator_not_found` |
| `GET /api/v1/tenants/{id}/issuers` (and `/verifiers`, `/mediators`, `/identities`) | The tenant's items, newest first: `{items, next_cursor, total}`, each with its DID's `signature` (`pending`, `signed`, `outdated`); `limit` (1–100, 20) and `cursor` from the previous page; `404 tenant_not_found` for a tenant the user is not in |
| `POST /api/v1/tenants/{id}/issuers` (and `/verifiers`) | Register one: `name` (1–200), optional `description` and optional `mediator_id` (one of the tenant's mediators, `422 mediator_not_found` otherwise). Each gets an identity of its own, named like it, and comes back with it (`identity`) and its `mediator` |
| `POST /api/v1/tenants/{id}/mediators` | Register a mediator: `name` and the `url` it listens on (`https`; plain `http` only on loopback; `https://` is added when no scheme is typed). Nothing is fetched from it: the registry gives it an identity of its own, whose DID document publishes that address; `422 mediator_invalid` or `mediator_insecure` |
| `GET /api/v1/tenants/{id}/mediators/{mediator_id}` | One mediator, with its `did`; `404 mediator_not_found` |
| `PATCH /api/v1/tenants/{id}/mediators/{mediator_id}` | Change its `name` (its identity is renamed with it) and/or `url`; its DID stays; `422` with `name_required`, `mediator_invalid` or `mediator_insecure` |
| `GET /api/v1/tenants/{id}/issuers/{issuer_id}`, `…/verifiers/{verifier_id}` | One issuer or verifier, with its `did`, its DID `document` and `document_url` (where it resolves once published); `404 issuer_not_found` / `verifier_not_found` |
| `PATCH /api/v1/tenants/{id}/issuers/{issuer_id}`, `…/verifiers/{verifier_id}` | Change its `name`, `description` and/or `mediator_id` (`null` removes the last two); its DID stays; `422` with `name_required` or `mediator_not_found` |
| `POST /api/v1/tenants/{id}/{issuers\|verifiers\|mediators}/{item_id}/publish` | Admins only (`403 not_admin`): publish it by **endorsing** it — a wallet request (`kind` `endorsement`) carrying the tenant's membership credential for it (`AlmenaMembership`, issued by the tenant's DID, valid a year) and its `whois.vp` template; the wallet signs both in one approval (the presentation as the item's controller, with a key the item's published document names under `authentication`); then it is published and its `whois.vp` served. Publishing again renews it. Drafts' DIDs do not resolve, the catalogue does not list them, no document routes through a draft mediator; `403 not_a_signer`, `409 identity_pending`/`identity_outdated`/`tenant_pending` |
| `POST /api/v1/tenants/{id}/{issuers\|verifiers\|mediators}/{item_id}/unpublish` | Admins only: back to a draft, its `whois.vp` withdrawn |
| `DELETE /api/v1/tenants/{id}/{issuers\|verifiers\|mediators}/{item_id}` | Admins only: delete it and its identity (its DID stops resolving for good); a deleted mediator's users are left without one. `204` |
| `GET /api/v1/tenants/{id}/{issuers\|verifiers}/{item_id}/signing` | How it signs (people sign from their wallets, never the server): `system` (`single_user`, or `null` while not configured) and `signer` (`id`, `email`, `alias`, and `member`: whether they still belong) |
| `PUT /api/v1/tenants/{id}/{issuers\|verifiers}/{item_id}/signing` | Admins only: `{"system": "single_user", "user_id": …}` (a member of the tenant) or `{"system": null}`; `422` with `signer_required` or `signer_not_member` |
| `GET /api/v1/catalog/{issuers\|verifiers\|mediators}` | Public: what is published, newest first, paged by `cursor`: `did`, `name`, `description` or `url`, `published_at`, and the tenant by its `did` (never its own name, which starts as "Tenant of {email}") |
| `GET /api/v1/tenants/{id}/identities/{identity_id}` | One identity: its `did`, `used_by`, and the DID `document` it publishes (at `document_url`); `404 identity_not_found` |
| `GET /api/v1/tenants/{id}/domains` | The tenant's linked domains: `{id, domain, dns_record, verified, verified_at, created_at}`, oldest first |
| `POST /api/v1/tenants/{id}/domains` | Admins: link a `domain` (whatever was typed; `https://Acme.com/x` is `acme.com`); it comes with the TXT record that proves it (`_almena.{domain}` = `almena-verify=…`); `409 domain_exists`, `422 domain_invalid` |
| `POST /api/v1/tenants/{id}/domains/{domain_id}/check` | Admins: look for the record; found, the domain is verified and the tenant's DID document names it (`LinkedDomains`) — which leaves it with changes to sign; `422 dns_record_not_found`, `503 dns_unavailable` |
| `DELETE /api/v1/tenants/{id}/domains/{domain_id}` | Admins: unlink it |
| `GET /api/v1/tenants/{id}/signatures` | The tenant's identities whose DID waits for a signature: `{id, name, signature}` (`pending` or `outdated`), oldest first |
| `POST /api/v1/tenants/{id}/identities` | Register an identity: `name` only (pending until an admin signs its first log entry). Identities are the tenant's register of DIDs, and come back with `used_by`: the tenant, issuers and verifiers that act as them |
| `GET /api/v1/tenants/{id}/members` | Who belongs (`status: member`) and who is invited but has not signed in yet (`status: invited`), each with `role` (`admin` or `member`) |
| `POST /api/v1/tenants/{id}/invitations` | Invite by `email` with a `role` (admins only; `locale` for the email): they join the next time they sign in with that address. Inviting again changes the role and resends; `403 not_admin`, `409 already_member`, `503 mail_unavailable` |
| `GET /ids/{slug}/did.json` | Public: an identity's DID document, where `did:web:almena.id:ids:{slug}` resolves (through the identity domain's proxy) (`application/did+json`). Its `DIDCommMessaging` service is a mediator's address for a mediator's identity, and the DID of the chosen mediator for the tenant's, an issuer's or a verifier's. An issuer's, verifier's or mediator's names its tenant's DID as `controller`; a tenant's has a `LinkedDomains` service with its verified domains |
| `GET /.well-known/did.json` | Public: the identity domain's own DID document (`did:web:almena.id`, `application/did+json`): the root tenant's identity; `404` until the root exists |
| `GET /.well-known/did-configuration.json` | Public: the origin's [DID configuration](https://identity.foundation/.well-known/resources/did-configuration/) (Domain Linkage Credentials, signed elsewhere), served as it is from the same directory; `404` without it |
| `GET /docs`, `GET /openapi.json` | API reference ([Scalar](https://scalar.com)) and the OpenAPI document, generated from the code (not in production) |

## Social sign-in

An account's ways in are its email (optional) and the provider accounts linked to it — an Almena wallet among them (provider `almena`, its `did:key` the subject); the last one cannot be unlinked. Linking one that belongs to another account answers `taken` and leaves it there. When the account doing it is empty — it belongs to no tenant, or only to its own untouched one — the answer also carries a `move_ticket`, valid ten minutes, for `POST /auth/me/move`.

Identities are `did:webvh` (`webvh.py`, `dids.py`): the registry works out the document each should publish and prepares the log entry that gets it there, and a tenant admin signs it from a wallet (`eddsa-jcs-2022`, over RFC 8785 JCS). The update keys are the tenant admins' linked wallets. Until its first entry is signed an identity is `pending` and has no DID (and nothing acting as it can be published: `409 identity_pending`); when what it should publish changes it is `outdated` until signed again. Details carry `did`, `signature`, `document`, `log_url` and `document_url`. A document lists the keys that sign for it as `Multikey` verification methods under `assertionMethod` (`{did}#{multikey}`): a tenant's own identity, its admins' wallets; an issuer or verifier, the wallets of the member its signing system names (`signer.wallet` says whether they have one); a mediator, none. Any change to those keys leaves the identity `outdated` until an admin signs it.

The wallet (`../wallet`, `registry.rs`) signs with a key it derives for the portal's origin. Its side can also be played in development by `task wallet -- '<deep link>'` (`tests/fake_wallet.py`, its key kept in `.fake-wallet-key`).

Google, Microsoft, Apple and GitHub run the authorization code flow with PKCE and `state` (and a `nonce` for the OpenID Connect ones). Each is off until its variables are set. Register this redirect URI with each provider, with the portal's origin: `https://registry.almena.id/auth/{google|microsoft|apple|github}/callback` (for development, `http://localhost:3000/…`; Apple accepts only HTTPS).

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
