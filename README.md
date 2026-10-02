# almena-api

The backend of the Almena ID registry portal: a [FastAPI](https://fastapi.tiangolo.com) service on PostgreSQL, consumed by [registry](../registry).

Built with Python 3.13, [uv](https://docs.astral.sh/uv/), SQLAlchemy 2 (async, asyncpg), Alembic for migrations and pydantic-settings for configuration.

## Quick start

Needs [uv](https://docs.astral.sh/uv/), [Task](https://taskfile.dev) and Docker.

```bash
task init   # .env from .env.example, with random database, OpenObserve and RabbitMQ passwords
task up     # PostgreSQL + migrations + API in Docker
task health # {"status":"ok","version":"0.1.0","database":"ok","vault":"ok"}
```

There are no passwords: signing up and signing in are the same flow, an email with a six-digit code. In development every email lands in [Mailpit](https://mailpit.axllent.org) at `http://localhost:8025` (`task up` starts it, `task dev` too).

Logs are indexed in [OpenObserve](https://openobserve.ai) at `http://localhost:5080` (`task up` starts it, `task dev` too), signing in as `REGISTRY_OPENOBSERVE_ADMIN` with `REGISTRY_OPENOBSERVE_PASSWORD`; entries older than `REGISTRY_OPENOBSERVE_RETENTION_DAYS` (30) are deleted.

Tenants' applications subscribe to the platform's messages through [RabbitMQ](https://www.rabbitmq.com) (`task up` starts it, `task dev` too, or `task rabbitmq` alone): AMQP on port `5672`, published on every interface for them to reach, in the `REGISTRY_RABBITMQ_VHOST` virtual host. `REGISTRY_RABBITMQ_ADMIN` is the platform's own account, which replaces `guest`; tenants get users of their own, limited to their queues. Its management UI stays on loopback at `http://localhost:15672`.

The keys of the issuers, verifiers and mediators are kept in a vault, behind a provider the API can swap (`registry_api.vault`: `REGISTRY_VAULT_PROVIDER`). Today's is [OpenBao](https://openbao.org) (`task up` starts it, `task dev` too, or `task openbao` alone): a real server on Raft storage, which starts sealed. The first run initializes it with a single unseal key and keeps that key and the root token in `.openbao-keys.json` (out of git); every run unseals it (`task openbao:unseal` alone) and provisions what the API needs (`task openbao:setup` alone): the KV v2 mount `REGISTRY_OPENBAO_MOUNT`, a policy over that mount alone, and the AppRole `REGISTRY_OPENBAO_ROLE_ID` with `REGISTRY_OPENBAO_SECRET_ID` (`task init` makes it). Every request it serves is audited to its container's output. Its API and UI stay on loopback at `http://localhost:8200/ui`, signing in with the root token. `task down:volume` deletes its data and that file.

The API is published at `https://api.almena.id`, for the portal at `https://registry.almena.id`; locally it answers at `http://localhost:8000`. For development, `task dev` runs it locally with auto-reload against PostgreSQL in Docker, and serves the interactive [Scalar](https://scalar.com) reference at `http://localhost:8000/docs`.

## The root authority

Almena is the root: the tenant that governs the network. It is an ordinary tenant, created once at install with its first admin, who joins by signing in to the portal with that address: `registry-api init-root --admin <email> [--name Almena] [--mediator-url https://mediator.almena.id]`. A second run fails. It comes with one mediator, `Almena Mediator`, at `https://mediator.almena.id` (or `--mediator-url`), public, and a draft until an admin publishes it; once published, every new tenant starts with it as its mediator. Its identity is the identity domain's own DID (`did:webvh:{SCID}:almena.id`, also served as `did:web:almena.id`), at `/.well-known/`. The server holds no key for it: keys arrive when its admins sign from their wallets.

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
| `REGISTRY_SESSION_TTL_HOURS` / `REGISTRY_SESSION_IDLE_MINUTES` | `12` / `30` | A portal sign-in ends this many hours after it started, or earlier once unused this many minutes |
| `REGISTRY_LOGIN_CODE_TTL_MINUTES` / `REGISTRY_LOGIN_CODE_MAX_ATTEMPTS` | `10` / `5` | Emailed sign-in codes: lifetime and wrong guesses allowed |
| `REGISTRY_SMTP_HOST` / `REGISTRY_SMTP_PORT` | `localhost` / `1025` | SMTP server (Mailpit in development) |
| `REGISTRY_SMTP_USERNAME` / `REGISTRY_SMTP_PASSWORD` / `REGISTRY_SMTP_STARTTLS` | — / — / `false` | SMTP credentials and STARTTLS, for a real server |
| `REGISTRY_MAIL_FROM` | `Almena Registry <no-reply@almena.id>` | Sender of the emails |
| `REGISTRY_PORTAL_URL` | `https://registry.almena.id` | The portal; providers send the browser back to `{portal}/auth/{provider}/callback` |
| `REGISTRY_GOOGLE_CLIENT_ID` / `REGISTRY_GOOGLE_CLIENT_SECRET` | — | Sign in with Google (off while empty) |
| `REGISTRY_MICROSOFT_CLIENT_ID` / `REGISTRY_MICROSOFT_CLIENT_SECRET` / `REGISTRY_MICROSOFT_TENANT` | — / — / `common` | Sign in with Microsoft (off while empty) |
| `REGISTRY_GITHUB_CLIENT_ID` / `REGISTRY_GITHUB_CLIENT_SECRET` | — | Sign in with GitHub (off while empty) |
| `REGISTRY_APPLE_CLIENT_ID` / `REGISTRY_APPLE_TEAM_ID` / `REGISTRY_APPLE_KEY_ID` / `REGISTRY_APPLE_PRIVATE_KEY` | — | Sign in with Apple: Services ID, team, key id and the `.p8` key (PEM) (off while empty) |
| `REGISTRY_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `REGISTRY_LOG_FORMAT` | `json` | `json`, one object per line (Compose always uses it), or `text` for a terminal (`task dev`'s); see [Logs](#logs) |
| `REGISTRY_OPENOBSERVE_PORT` / `REGISTRY_OPENOBSERVE_ADMIN` / `REGISTRY_OPENOBSERVE_PASSWORD` | `5080` / `admin@almena.id` / — | OpenObserve (Compose): port published on the host, and its admin; the password needs a lowercase and an uppercase letter, a digit and a symbol (`task init` makes one) |
| `REGISTRY_OPENOBSERVE_RETENTION_DAYS` | `30` | Days before OpenObserve deletes log entries |
| `REGISTRY_RABBITMQ_PORT` / `REGISTRY_RABBITMQ_UI_PORT` | `5672` / `15672` | RabbitMQ (Compose): AMQP port published on every interface, for the tenants' applications, and its management UI, on loopback |
| `REGISTRY_RABBITMQ_ADMIN` / `REGISTRY_RABBITMQ_PASSWORD` / `REGISTRY_RABBITMQ_VHOST` | `almena` / — / `almena` | RabbitMQ's admin, the platform's own account (`task init` makes the password), and the virtual host holding the tenants' queues |
| `REGISTRY_VAULT_PROVIDER` | `openbao` | The vault keeping the platform's secrets (only `openbao` so far) |
| `REGISTRY_OPENBAO_ADDR` / `REGISTRY_OPENBAO_MOUNT` | `http://localhost:8200` / `almena` | OpenBao's address and the KV v2 mount the secrets live in (Compose points the API at `http://openbao:8200`) |
| `REGISTRY_OPENBAO_ROLE_ID` / `REGISTRY_OPENBAO_SECRET_ID` | `registry-api` / — | The AppRole the API signs in with (`task init` makes the secret id; `task openbao:setup` registers it) |
| `REGISTRY_OPENBAO_PORT` | `8200` | OpenBao (Compose): its API and UI, published on loopback |

## Endpoints

| | |
|---|---|
| `GET /` | The home page for browsers (HTML, in red): status, version, database |
| `GET /fonts/{name}` | The home page's typefaces (Chakra Petch, Inter, JetBrains Mono; woff2) |
| `GET /health` | Liveness: the process is up |
| `GET /health/ready` | Readiness: `503` while the database or the vault is unreachable (`database`, `vault`: `ok` or `unavailable`) |
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
| `POST /api/v1/tenants/{id}/identities/{identity_id}/sign` | Whoever signs as the tenant under its flow (`403 not_a_signer` otherwise), with a linked wallet that is one of the update keys: a wallet request (`purpose` `sign`, same answer as `POST /auth/wallet/requests`) carrying the identity's next did:webvh log entry; the wallet answers with a `proof` and the entry is published; the poll answers `signed`. Signing an issuer or a verifier the first time makes its messaging key (X25519, private half in the vault), so the entry signed lists it under `keyAgreement`; `403 not_a_signer`, `409 up_to_date`/`no_signers`, `503 vault_unavailable` |
| `GET /ids/{slug}/did.jsonl` | An identity's did:webvh log (JSON Lines); `404` while pending or a draft |
| `GET /ids/{slug}/did.json` | Its current document under its did:web name, with `alsoKnownAs` its did:webvh DID |
| `GET /ids/{slug}/whois.vp` | The did:webvh `#whois` (`application/vp`): a published issuer's, verifier's or mediator's endorsement by its tenant; `404` otherwise |
| `GET /.well-known/did.jsonl` | The root tenant's log (`did:webvh:{SCID}:almena.id`); `did.json` beside it is `did:web:almena.id` |
| `GET /api/v1/tenants` | The signed-in user's tenants, oldest first, with the user's `role` in each and whether they `signs` as it under its flow (`name` is `null` until one is given) |
| `GET /api/v1/tenants/{id}` | A tenant's details: `name`, the user's `role`, its own `identity` (created with it, named like it, renamed with it), the `mediator` its identity receives messages through (`{id, name, own}`: one of its mediators, or another tenant's public one when `own` is `false`; `null` when none; new tenants start with the root's once it is published), its `signing_flow`: who signs as the tenant — its identities' log entries and its endorsements (`any_admin`: any one admin with a linked wallet, alone; `single_user`: the member named in `signer`, admin or not, alone), `signer` (`null` unless `single_user`) and whether the user `signs` |
| `PATCH /api/v1/tenants/{id}` | Change `name`, `mediator_id`, `signing_flow` and/or `signer_id` (admins only; `null` removes the mediator, and leaves the flow as it is; a flow other than `single_user` drops the signer); `422` with `name_required`, `mediator_not_found` (neither the tenant's nor a published public one), `signer_required` or `signer_not_member` |
| `GET /api/v1/tenants/{id}/health` | Any member: what the tenant still needs set up to operate. `score` (0 to 100, the share of checks done) and `checks`, in order, each `{check, done, issue}`: `name` (`missing`), `mediator` (`missing`, or `unpublished`: its DID does not resolve yet), `signing_flow` (`no_signer`: `single_user` names nobody who still belongs; `no_wallet`: nobody who signs under the flow has a wallet linked) |
| `GET /api/v1/tenants/{id}/mediator-choices` | The mediators the tenant, its issuers and verifiers may pick: its own (drafts too, oldest first), then other tenants' public and published ones (by name); each `{id, name, url, own, published}` |
| `GET /api/v1/tenants/{id}/issuers` (and `/verifiers`, `/mediators`, `/identities`) | The tenant's items, newest first: `{items, next_cursor, total}`, each with its DID's `signature` (`pending`, `signed`, `outdated`); `limit` (1–100, 20) and `cursor` from the previous page; `404 tenant_not_found` for a tenant the user is not in |
| `POST /api/v1/tenants/{id}/issuers` (and `/verifiers`) | Register one: `name` (1–200), optional `description` and optional `mediator_id` (one of the tenant's mediators or a published public one, `422 mediator_not_found` otherwise). Each gets an identity of its own, named like it, and comes back with it (`identity`) and its `mediator` |
| `POST /api/v1/tenants/{id}/mediators` | Register a mediator: `name`, whether it is `public` (offered to every tenant once published; `false` by default) and where it listens: a `subdomain` (one label or more, `eu.relay`) of `domain_id`, one of the tenant's verified domains; its `url` is `https://{subdomain}.{domain}`. Nothing is fetched from it: the registry gives it an identity of its own, whose DID document publishes that address; `422` with `domain_not_found` (not the tenant's), `domain_unverified` or `subdomain_invalid` |
| `GET /api/v1/tenants/{id}/mediators/{mediator_id}` | One mediator, with its `did`; `404 mediator_not_found` |
| `PATCH /api/v1/tenants/{id}/mediators/{mediator_id}` | Change its `name` (its identity is renamed with it), `url` and/or `public` (made private, it is no longer offered; tenants that picked it keep it); its DID stays; `422` with `name_required`, `mediator_invalid` or `mediator_insecure` |
| `GET /api/v1/tenants/{id}/issuers/{issuer_id}`, `…/verifiers/{verifier_id}` | One issuer or verifier, with its `did`, its DID `document` and `document_url` (where it resolves once published); `404 issuer_not_found` / `verifier_not_found` |
| `PATCH /api/v1/tenants/{id}/issuers/{issuer_id}`, `…/verifiers/{verifier_id}` | Change its `name`, `description` and/or `mediator_id` (`null` removes the last two); its DID stays; `422` with `name_required` or `mediator_not_found` |
| `POST /api/v1/tenants/{id}/{issuers\|verifiers\|mediators}/{item_id}/publish` | Whoever signs as the tenant under its flow (`403 not_a_signer` otherwise): publish it by **endorsing** it — a wallet request (`kind` `endorsement`) carrying the tenant's membership credential for it (`AlmenaMembership`, issued by the tenant's DID, valid a year) and its `whois.vp` template; the wallet signs both in one approval (the presentation as the item's controller, with a key the item's published document names under `authentication`); then it is published and its `whois.vp` served. Publishing again renews it. Drafts' DIDs do not resolve, the catalogue does not list them, no document routes through a draft mediator; `403 not_a_signer`, `409 identity_pending`/`identity_outdated`/`tenant_pending` |
| `POST /api/v1/tenants/{id}/{issuers\|verifiers\|mediators}/{item_id}/unpublish` | Admins only: back to a draft, its `whois.vp` withdrawn |
| `DELETE /api/v1/tenants/{id}/{issuers\|verifiers\|mediators}/{item_id}` | Admins only: delete it and its identity (its DID stops resolving for good) and, an issuer's or verifier's, its keys in the vault; a deleted mediator's users are left without one. `204` |
| `GET /api/v1/tenants/{id}/{issuers\|verifiers}/{item_id}/signing` | How it signs (people sign from their wallets, never the server): `system` (`single_user`, or `null` while not configured) and `signer` (`id`, `email`, `alias`, and `member`: whether they still belong) |
| `PUT /api/v1/tenants/{id}/{issuers\|verifiers}/{item_id}/signing` | Admins only: `{"system": "single_user", "user_id": …}` (a member of the tenant) or `{"system": null}`; `422` with `signer_required` or `signer_not_member` |
| `GET /api/v1/catalog/{issuers\|verifiers\|mediators}` | Public: what is published, newest first, paged by `cursor`: `did`, `name`, `description` or `url`, an issuer's `slug`, the `credential_types` it grants and those it `offers`, `published_at`, and the tenant by its `did` (never its own name, which starts as "Tenant of {email}") |
| `GET /api/v1/tenants/{id}/identities/{identity_id}` | One identity: its `did`, `used_by`, and the DID `document` it publishes (at `document_url`); `404 identity_not_found` |
| `GET /api/v1/tenants/{id}/domains` | The tenant's linked domains: `{id, domain, dns_record, verified, verified_at, created_at}`, oldest first |
| `POST /api/v1/tenants/{id}/domains` | Admins: link a `domain` (whatever was typed; `https://Acme.com/x` is `acme.com`); it comes with the TXT record that proves it (`_almena.{domain}` = `almena-verify=…`); `409 domain_exists`, `422 domain_invalid` |
| `POST /api/v1/tenants/{id}/domains/{domain_id}/check` | Admins: look for the record; found, the domain is verified and the tenant's DID document names it (`LinkedDomains`) — which leaves it with changes to sign; `409 domain_verified` once verified, `422 dns_record_not_found`, `503 dns_unavailable` |
| `DELETE /api/v1/tenants/{id}/domains/{domain_id}` | Admins: unlink it |
| `GET /api/v1/tenants/{id}/forms` | The tenant's forms, newest first: `{id, slug, name, description, fields, credentials, created_at, updated_at}` |
| `POST /api/v1/tenants/{id}/forms` | Any member: create a form — what is asked of a person, whoever asks it (an issuer's offer, or a verifier's): its `name` and optional `description`, each by language (`{"en": …, "es": …}`, the portal's languages; the name in one at least) and its `fields` in order (1–100), each a field of the catalogue: `ref` (its id, or `custom:{key}` for one of the tenant's own), `required` (`true` by default), optional `help` (by language), `as` (its name in the form, only for repeatable fields such as `document_file`) and `narrow`, which only makes it stricter — `values` (of its domain: countries, file formats…), `min_date`/`max_date`, a lower `max_length`. Optionally `credentials` (up to 20) it asks to be presented: each a `type` of the credential catalogue under a `key` (the type's id by default), `required` (`true` by default), an optional `purpose` (by language), the `claims` wanted (all the type's by default) and its `trust` — `registry` (any published issuer granting the type; the default), `issuers` (only the published ones whose DIDs are named, granting the type) or `framework` (types issued elsewhere, such as the EU PID; their only mode). Each comes back with `fills`: the form's fields named as its claims. At least one field or credential. `422` with `fields_required`, `field_unknown`, `field_rename_invalid`, `field_key_invalid`, `field_key_duplicate`, `field_narrow_invalid`, `credential_unknown`, `credential_key_invalid`, `credential_key_duplicate`, `credential_type_duplicate` (a type is asked for once), `credential_claims_invalid` (also `claims: []`) or `credential_trust_invalid` |
| `GET /api/v1/tenants/{id}/forms/{form_id}` | One form; `404 form_not_found` |
| `GET /api/v1/tenants/{id}/forms/{form_id}/dcql` | The OpenID4VP DCQL query of its credentials: one credential query per format of each type (`dc+sd-jwt` by `vct`, `jwt_vc_json` by W3C type), each entry's alternatives in a `credential_sets` entry with its `required`. Trust is not in the query (DCQL cannot name DIDs or the registry): it is the form's, checked on verification |
| `POST /api/v1/tenants/{id}/forms/{form_id}/verify` | Verify the credentials presented for a form: `vp_token` (OpenID4VP's: for each DCQL query id, its presentations), the `nonce` and `audience` the holder's key binding must carry. Each credential comes back `{key, presented, verified, query, format, issuer, claims, fills, problems}`: SD-JWT VC with key binding or W3C VC/VP JWTs, EdDSA; the issuer's signature with a key its registry DID lists (`did:webvh` or `did:web` alias; only registry DIDs resolve), trust as the form says, type, validity, status (Token Status List or Bitstring Status List, fetched), holder binding, nonce, audience and disclosures; `claims` and `fills` only when verified. `verified` overall: every required one presented and verified, none presented that failed. Problems: `format_invalid`, `signature_invalid`, `issuer_unresolvable`, `issuer_untrusted`, `trust_framework_unsupported` (the EU PID's framework is not verified yet), `type_mismatch`, `expired`, `not_yet_valid`, `revoked`, `suspended`, `status_unverifiable`, `holder_binding_invalid`, `nonce_mismatch`, `audience_mismatch`, `disclosure_invalid`, `claims_missing`, `not_presented` |
| `GET /api/v1/tenants/{id}/forms/{form_id}/schema` | The JSON Schema 2020-12 its answers meet: each of Almena's fields the catalogue's schema (`$ref`), the tenant's own written out whole; narrowed by the form (`enum`, `formatMinimum`/`formatMaximum`, `maxLength`); `help` as `description` |
| `GET /api/v1/catalog/fields` | Public: Almena's field catalogue — the only fields a form may ask for: `{version, languages, categories, fields, domains}`. Each field has its `id` (named as OIDC, the EU PID rule book, ISO 18013-5/23220 or schema.org name it), `type` (`text`, `email`, `phone`, `date`, `code`, `codes`, `file`, `group`), `labels` per language, `source`, its `schema` URL, what a form may `narrow`, whether it is `repeatable`, its `domain` and, for groups, its `parts`. `domains` holds the value lists with their labels (ISO 3166-1 countries, ISO 639-1 languages, ISO/IEC 5218 sex, ISCED 2011 levels, identity document types, file formats with their media types) |
| `GET /api/v1/tenants/{id}/fields` | The tenant's own fields, newest first: `{id, slug, key, ref, field, created_at, updated_at}` — `ref` is how forms name it (`custom:{key}`), `field` its shape in the catalogue's terms (category `custom`, its options inline as `codes`, no published schema) |
| `POST /api/v1/tenants/{id}/fields` | Any member: add a field of the tenant's own when Almena's catalogue lacks it — `key` (`[a-z][a-z0-9_]*`, never one of Almena's ids), `type` (`text`, `email`, `phone`, `date`, `code`, `codes`, `file`), `labels` (`en` and/or `es`) and what the type takes: `max_length`/`pattern` (text), `options` (`[{value, labels}]`, at least two; lists), `formats` (files, of the catalogue's). Not published, seen only by the tenant. `409 key_exists`; `422` with `key_invalid`, `key_reserved`, `labels_required`, `constraint_invalid`, `pattern_invalid`, `options_invalid` or `formats_invalid` |
| `DELETE /api/v1/tenants/{id}/fields/{field_id}` | Any member: delete one; `409 field_in_use` while a form asks for it |
| `GET /api/v1/catalog/credentials` | Public: Almena's credential type catalogue — what issuers grant and forms ask for: `{version, languages, categories, types}`. Each type has its `id`, `category`, `labels` and `descriptions` per language, `source`, `issuance` (`almena`: any tenant's issuer grants it; `external`: issued under its own framework, such as the EU PID, only asked for), its `claims` (`{field, required}`, each a field of the field catalogue), how each format names it (`formats`: `dc+sd-jwt` `vct`, `jwt_vc_json` `type`, `mso_mdoc` `doctype`), its claims' `schema` and, for Almena's own, its SD-JWT VC Type `metadata` URL |
| `GET /schemas/credentials/v1` | Public, on the identity domain: the same catalogue |
| `GET /schemas/credentials/v1/{type_id}.json` | Public, on the identity domain: the JSON Schema 2020-12 of a type's claims (each the field's published schema), `application/schema+json` — a W3C credential's `credentialSchema` and the type metadata's `schema_uri`; `404 credential_type_not_found` |
| `GET /.well-known/vct/credentials/{type_id}/v1` | Public, on the identity domain: the SD-JWT VC Type Metadata of one of Almena's types, where its `vct` (`https://almena.id/credentials/{type_id}/v1`) resolves: `name`, `description`, `display` and claim labels per `locale`, `mandatory` claims, `schema_uri` |
| `GET /api/v1/tenants/{id}/issuers/{issuer_id}/credential-types` | The credential types the issuer grants: `{types}` |
| `PUT /api/v1/tenants/{id}/issuers/{issuer_id}/credential-types` | Any member: declare them, `{types, forms}` — `forms` maps a type it grants to one of the tenant's forms, which makes it an *offer* (`422 request_form_invalid` otherwise); ids of the catalogue a tenant issues (not `external` ones); kept in the catalogue's order and listed in the public catalogue of issuers (`credential_types`); `422 credential_type_invalid` |
| `GET /schemas/fields/v1` | Public, on the identity domain (proxied like `/ids/`): the same catalogue |
| `GET /schemas/fields/v1/{field_id}.json` | Public, on the identity domain: a field's JSON Schema 2020-12 (`application/schema+json`), its `$id` this very URL; `404 field_not_found` |
| `GET /api/v1/tenants/{id}/signatures` | The tenant's identities whose DID waits for a signature: `{id, name, signature}` (`pending` or `outdated`), oldest first |
| `POST /api/v1/tenants/{id}/identities` | Register an identity: `name` only (pending until an admin signs its first log entry). Identities are the tenant's register of DIDs, and come back with `used_by`: the tenant, issuers and verifiers that act as them |
| `GET /api/v1/catalog/issuers/{issuer_slug}/offers/{type_id}` | Public: an issuer's offer — the issuer, the credential type and its form, each field with its catalogue definition (`field`) and the credentials it asks for with what they fill; `404 offer_not_found` |
| `POST /api/v1/applications` | Public: start an application for an offer, `{issuer: slug, type}`: `{id, slug, secret}`. Whoever started it keeps `secret` and sends it as `X-Application-Secret` on every holder-side call below. Unsubmitted applications expire after a day |
| `GET /api/v1/applications/{id}` | The application: `status` (`open`, `paired`, `submitted`, `accepted`, `rejected`, `issued`, with `issued_at`, `valid_until` and `delivered_at`), the `holder_did`, the `offer`, the `answers`, what verified credentials `filled`, the `files`, the credentials `presented` and the wallet request in course (`wallet`: `purpose`, `answered`, `live`, `deep_link`); `404 application_not_found`, `410 application_expired` |
| `POST /api/v1/applications/{id}/wallet` | Ask the holder's wallet, `{purpose}`: `pair` (QR 1, once: `409 already_paired` after), `present` (the form's credentials, an OpenID4VP request with its DCQL query), `submit` (QR 2: once the answers hold and every required credential is verified) or `receive` (QR 3, once issued: the wallet answers with an `id_token` by the paired `did:key` and the response is the credential, `{format, credential, issuer, credential_type}`). Its `deep_link` is `almena://auth?request_uri=…`, good for 5 minutes; `409` with `application_closed`, `application_not_paired`, `nothing_to_present`, `answers_incomplete`, `not_issued`, `already_paired` or `already_received` (the credential is taken once) |
| `GET /api/v1/applications/{id}/wallet` | The wallet request in course: whether it has been answered (the portal polls) |
| `PUT /api/v1/applications/{id}/answers` | Save the typed answers, `{answers}` by field key (a group as an object), checked against each field — fields a verified credential filled are not typed; `422 {code: answers_invalid, errors: {field: required\|format\|range\|value\|file}}` (a group's parts as `field.part`) |
| `POST /api/v1/applications/{id}/files` | Upload a file for a file field (multipart: `key`, `file`; replaces the last): `{filename, media_type, size, digest}`; only the formats the field accepts, up to 10 MB; `422 file_field_unknown`/`file_type_invalid`/`file_empty`, `413 file_too_large`. `DELETE …/files/{key}` removes it |
| `GET /api/v1/applications/{id}/request` | The wallet request in course, as the wallet reads it (its id is in the QR): `response_type` (`id_token`, `vp_token` or `signature`), `direct_post` to `response_uri`, `client_id` (the portal), `nonce`, `purpose`, the `issuer` (whose `did:key` the wallet keeps one of) and the `credential_type`; `present` adds the `dcql_query`, `submit` the `submission`: the `content` to sign — every answer with its label and readable `text` per language, files by digest, the credentials presented — and its `digest` (SHA-256 of its JCS); `404 request_not_found`, `409 already_answered`, `410 request_expired` |
| `POST /api/v1/applications/{id}/request/response` | The wallet's answer, a form or JSON: `id_token` (pair: by the holder's `did:key`, for the portal and nonce), `vp_token` (present: verified as `…/forms/{id}/verify` does) or `signature` (submit: an EdDSA JWS by the paired `did:key` with `aud`, `nonce`, `application` and `digest`, which must be the content's); `400` with `invalid_token`, `invalid_did`, `invalid_vp_token`, `invalid_signature`, `not_the_holder` or `digest_mismatch`; `409 answers_incomplete` (the answers are checked again as the signature arrives), `already_paired` or `already_received` |
| `GET /api/v1/tenants/{id}/applications` | The applications the tenant's issuers received (submitted or decided), newest first; `issuer_id` filters |
| `GET /api/v1/tenants/{id}/applications/{application_id}` | One: what the holder signed (`content`), its `files`, the credentials `presented`, the `digest` and `signature`, and whether the signature still holds over the content (`signature_valid`) |
| `GET /api/v1/tenants/{id}/applications/{application_id}/files/{key}` | A file, as uploaded (as an attachment) |
| `POST /api/v1/tenants/{id}/applications/{application_id}/decision` | Any member: `{decision: accepted\|rejected, note?}` on a submitted one; `409 already_decided` |
| `GET /api/v1/tenants/{id}/applications/{application_id}/issuance` | An accepted application's credential to issue: the type's claims with their values proposed (the holder's answers, what verified credentials filled, the issuer's name as `organization_name`; the draft once saved), `valid_until` (a year by default), the `holder_did` it will be bound to, and `can_sign` (the one asking is the issuer's signer, with a wallet whose key its DID lists); `409 not_accepted`/`already_issued` |
| `PUT /api/v1/tenants/{id}/applications/{application_id}/issuance` | The issuer's signer (`403 not_the_issuers_signer` otherwise): settle `{claims, valid_until}` — each claim checked against its field, the always-present ones required; the draft its signer signs; `422 {code: claims_invalid, errors}` or `valid_until_invalid` |
| `POST /api/v1/tenants/{id}/applications/{application_id}/issuance/sign` | The issuer's signer: a wallet request (`purpose: sign`, kind `credential`) to sign the SD-JWT VC built from the draft — header (`EdDSA`, `typ: dc+sd-jwt`, `kid` the issuer's DID and the signer's key), payload (`iss`, `iat`, `exp`, `vct`, `cnf.kid` the holder's `did:key`, `_sd`) and a disclosure per claim. The wallet answers `{jws}`; checked against that very header and payload with the signer's key, the application is `issued`. `403 not_the_issuers_signer`, `409 no_draft`/`not_paired` |
| `GET /api/v1/tenants/{id}/members` | Who belongs (`status: member`) and who is invited but has not signed in yet (`status: invited`), each with `role` (`admin` or `member`) and, members only, whether they have an Almena `wallet` linked |
| `POST /api/v1/tenants/{id}/invitations` | Invite by `email` with a `role` (admins only; `locale` for the email): they join the next time they sign in with that address. Inviting again changes the role and resends; `403 not_admin`, `409 already_member`, `503 mail_unavailable` |
| `GET /ids/{slug}/did.json` | Public: an identity's DID document, where `did:web:almena.id:ids:{slug}` resolves (through the identity domain's proxy) (`application/did+json`). Its `DIDCommMessaging` service is a mediator's address for a mediator's identity, and the DID of the chosen mediator for the tenant's, an issuer's or a verifier's. An issuer's, verifier's or mediator's names its tenant's DID as `controller`; a tenant's has a `LinkedDomains` service with its verified domains |
| `GET /.well-known/did.json` | Public: the identity domain's own DID document (`did:web:almena.id`, `application/did+json`): the root tenant's identity; `404` until the root exists |
| `GET /.well-known/did-configuration.json` | Public: the origin's [DID configuration](https://identity.foundation/.well-known/resources/did-configuration/) (Domain Linkage Credentials, signed elsewhere), served as it is from the same directory; `404` without it |
| `GET /.well-known/security.txt` | Public: where to report a vulnerability ([RFC 9116](https://www.rfc-editor.org/rfc/rfc9116)): this repository's private advisories; `Expires` stays 180 days ahead |
| `GET /docs`, `GET /openapi.json` | API reference ([Scalar](https://scalar.com)) and the OpenAPI document, generated from the code (not in production) |

## Social sign-in

An account's ways in are its email (optional) and the provider accounts linked to it — an Almena wallet among them (provider `almena`, its `did:key` the subject); the last one cannot be unlinked. Linking one that belongs to another account answers `taken` and leaves it there. When the account doing it is empty — it belongs to no tenant, or only to its own untouched one — the answer also carries a `move_ticket`, valid ten minutes, for `POST /auth/me/move`.

Identities are `did:webvh` (`webvh.py`, `dids.py`): the registry works out the document each should publish and prepares the log entry that gets it there, and a tenant admin signs it from a wallet (`eddsa-jcs-2022`, over RFC 8785 JCS). The update keys are the tenant admins' linked wallets. Until its first entry is signed an identity is `pending` and has no DID (and nothing acting as it can be published: `409 identity_pending`); when what it should publish changes it is `outdated` until signed again. Details carry `did`, `signature`, `document`, `log_url` and `document_url`. A document lists the keys that sign for it as `Multikey` verification methods under `assertionMethod` (`{did}#{multikey}`): a tenant's own identity, its admins' wallets; an issuer or verifier, the wallets of the member its signing system names (`signer.wallet` says whether they have one); a mediator, none. Any change to those keys leaves the identity `outdated` until an admin signs it.

The wallet (`../wallet`, `registry.rs`) signs with a key it derives for the portal's origin. Its side can also be played in development by `task wallet -- '<deep link>'` (`tests/fake_wallet.py`, its key kept in `.fake-wallet-key`).

Google, Microsoft, Apple and GitHub run the authorization code flow with PKCE and `state` (and a `nonce` for the OpenID Connect ones). Each is off until its variables are set. Register this redirect URI with each provider, with the portal's origin: `https://registry.almena.id/auth/{google|microsoft|apple|github}/callback` (for development, `http://localhost:3000/…`; Apple accepts only HTTPS).

An account is its email, however it signs in. A provider's account joins or creates the account for its email only when the provider vouches for the address: Google's and Apple's `email_verified`, GitHub's primary verified email, and for Microsoft a personal account or a work account whose ID token carries the optional claim `xms_edov` (add it in the app registration's *Token configuration*).

## Logs

The API writes one line per record on stdout: JSON (`REGISTRY_LOG_FORMAT=json`, what the container always uses) or text for a terminal (`task dev`'s). Every record made while a request is served carries its context, so any query can be narrowed to one request, user or tenant:

| Field | |
|---|---|
| `timestamp`, `level`, `logger`, `message` | Always |
| `request_id` | The request's id: the caller's `X-Request-ID` when it is one (up to 64 letters, digits, `.`, `_`, `-`), a new one otherwise; every response carries it back in `X-Request-ID` |
| `user_id` | The signed-in user, once the bearer token holds |
| `tenant_id` | The tenant the request acts on, once membership is checked (`/api/v1/tenants/{id}/…`) |
| `exception` | The traceback, for an error |

Each request leaves an access record (logger `registry_api.access`) with `method`, `path` (never the query string), `route` (the path template), `status` and `duration_ms`; an unhandled error turns it into an `ERROR` with the traceback. Successful health checks leave none. Only ids are logged: no email, name or IP address, and extra fields named like a secret or personal data (`email`, `code`, `token`, `password`, `authorization`, `nonce`, `state`, `proof`, `poll`, `ticket`, `*_key`, `*_secret`…) are written as `[redacted]`.

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
