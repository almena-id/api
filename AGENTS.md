# AGENTS.md

Guidance for coding agents working on `api`.

## Layout

```
src/registry_api/
  __main__.py        entry point (`registry-api`): runs uvicorn with the settings;
                     `registry-api init-root --admin <email>` creates the root tenant once
                     (Almena Trust Anchor, with Almena's catalogue)
  main.py            FastAPI app factory: CORS, routers, lifespan, Scalar docs
  config.py          Settings (pydantic-settings, REGISTRY_* variables)
  db.py              async engine, sessionmaker and the `get_session` dependency
  logs.py            log format (JSON or text), each request's context (request id, user,
                     tenant: `set_user`, `set_tenant`), redaction and the access record
  security.py        sign-in codes and session tokens (both stored as SHA-256)
  errors.py          `Refusal`: every error is `{"detail": "<code>"}`, what more says why
                     beside it (`field`, `credential`, `errors`); never an object in `detail`
  paging.py          keyset paging, newest first: `Limit`, `Cursor`, `encode`/`decode`, `after`
  housekeeping.py    sweeping what has run out, every `REGISTRY_HOUSEKEEPING_MINUTES` (the
                     app's lifespan runs it); the one place that deletes expired rows
  memo.py            what a request reads more than once (the catalogue, who acts as an
                     identity, signers' keys), kept in the session until it writes
  throttle.py        limits on what callers do, counted in `throttles` rows per key and
                     window: sign-in codes and wrong codes per address, and per network
                     address (`client_ip`: the visitor a portal names with the shared
                     `REGISTRY_PORTAL_SECRET`) codes, applications, wallet sign-ins;
                     refusals are 429 with `Retry-After`
  mail.py            SMTP mailer (`get_mailer` dependency) and the email texts, en/es: plain
                     text plus HTML, framed by `assets/mail.html` + `mail.css` (light, dark
                     by `prefers-color-scheme`/`[data-ogsc]`), the green mark inline (cid)
  mediators.py       checking the address a mediator listens on (nothing is fetched)
  dns_proof.py       proving a domain with a DNS TXT record (`_almena-{tenant-slug}.{domain}`;
                     the bare `_almena.{domain}` first used still counts)
  root.py            the root tenant (Almena Trust Anchor): created once, with Almena's
                     catalogue and no mediator; its identity is the domain's DID
                     (did:web:almena.id); the public mediator its admins add and publish is
                     every new tenant's default (`default_mediator`)
  trust_anchor.py    the trust anchor (the root) and its catalogue, everyone's: `load` reads
                     it from the database (the anchor's fields, domains, categories, credential
                     types, plus a tenant's own fields as `custom:{key}`), `seed` gives the
                     anchor `assets/catalogue-v1.json`. Nothing of the catalogue is code
  entitlements.py    what a tenant's subscription (`models.Subscription`, one per tenant) lets
                     it do: features, plans (`standard`), `in_force` (active; past_due for
                     `GRACE`), `allows`/`features` — the one place that decides; routes
                     refuse with `subscription_required`. The anchor has every feature
  dids.py            identities' did:webvh DIDs (`{did_url}/ids/{slug}`, almena.id): the document
                     each should publish (built in phases: it names only what exists
                     already), its signed log, the next entry to sign, its status
  signing_flows.py   the signing engine: who signs as a tenant, by its flow (`any_admin`,
                     `single_user`); the platform keeps no private keys: every signature is
                     a person's, from their wallet
  credential_catalog.py  what a credential type is (the types are the anchor's data): claims
                     named as catalogue fields, how each format names them (vct, W3C type,
                     mdoc doctype), their claims' JSON Schema and SD-JWT VC Type Metadata
                     (`/.well-known/vct/…`); `external` ones (EU PID) are only asked for
  form_credentials.py  a form's `credentials` block: credential types asked to be
                     presented, their claims, whom they are trusted from, the fields they
                     fill, and the OpenID4VP DCQL query they make
  texts.py           texts by language (`{"en": …, "es": …}`): a form's name, description,
                     help and purposes; `clean` checks them, `text_of` picks one,
                     `enabled_only` refuses what a tenant writes in a language it does not
                     work in (`language_not_enabled`), checked after everything else
  answers.py         checking a holder's typed answers against the form's fields
  patterns.py        tenants' text patterns, run with a time limit (`regex`), so one that
                     backtracks without end fails the answer instead of holding the server
  issuance.py        issuing an accepted application's credential: the SD-JWT VC its
                     signer's wallet signs (header, payload, disclosures) and the check
                     of that JWS
  status_lists.py    issuers' status lists (IETF Token Status List, 2 bits: valid, revoked,
                     suspended): packing, the token its signer's wallet signs, random
                     indexes, whether a list must be signed (again)
  presentations.py   verifying presented credentials (SD-JWT VC, W3C JWT VC/VP): issuer
                     signature by registry DID, the form's trust, type, validity, status
                     lists (`get_status_fetch`, overridden in tests), holder binding, claims
  field_catalog.py   what a field is (the fields are data: the anchor's, everyone's, and each
                     tenant's own): types, groups, value domains, narrowing, the JSON Schema
                     an answer meets and the catalogue as served. A new field of Almena's is
                     added by the anchor (`POST …/fields`), never here
  tenant_health.py   a tenant's health: the checks it must pass to operate (name, mediator,
                     signing flow) and its score; the root's are its onboarding in order
                     (name, signing flow, domain, identity, its own mediator: created,
                     signed, published, chosen), each with the item it is done on; new
                     checks go here
  webvh.py           did:webvh 1.0 logs: JCS, SCID, entry hashes, eddsa-jcs-2022 proofs (checked)
  wallet.py          checking a wallet's answer: did:key (Ed25519) and its signed id_token;
                     `channel_url`: every address the wallet reads or answers (sign-in, sign,
                     applications, collect, verifications) is on the identity domain
                     (`did_url`), since the wallet's key follows the origin it reads from
  didcomm.py         reading the did:peer:2 a holder's wallet names at pairing (kept on the
                     application; nothing is sent to it)
  broker/            the message broker issuers' and verifiers' back offices read from:
                     base.py (`Broker`: declare a queue, grant its read-only user,
                     remove both, publish), rabbitmq.py (the management HTTP API),
                     memory.py (tests); `get_broker`, overridden in tests
  queues.py          what goes into an issuer's queue (`subject.{slug}`): applications
                     submitted, decided, issued, and credential status changes; never a
                     condition for what happened
  mcp.py             the MCP tools: each one a route of the API (`TOOLS`: name, method,
                     path), its input schema built from that route's OpenAPI operation, called
                     in process with the caller's bearer token; a new tool is a line there
  oauth.py           social sign-in providers (Google, Microsoft, Apple, GitHub):
                     authorize URL, code exchange, the verified email they vouch for
  api/router.py      /api/v1 router: include new route modules here
  api/routes/        one module per resource (home.py, the browser page at `/` in red
                     with its typefaces at `/fonts/` from `assets/fonts`; health.py and did_documents.py,
                     the public did.json, and well_known.py, the root's
                     did.json, the did-configuration.json served from disk and security.txt, live outside /api/v1);
                     auth.py holds `current_session`, the dependency for signed-in routes;
                     account.py: the account's ways in (email, provider accounts), linked
                     and unlinked, and the move out of an empty account;
                     tokens.py: the account's API tokens for scripts and CI (sessions
                     with a name: no idle end; only a sign-in makes them);
                     wallet.py: signing in or linking with an Almena wallet
                     (requests, the direct_post answer, the portal's or the CLI's poll:
                     `client` names who asks, and so the token's audience), and
                     the tenant's signers signing an identity's next log entry (`purpose: sign`)
                     directory.py: a tenant's issuers, verifiers, mediators, identities;
                     pending.py: what waits in a tenant to be signed or published, in
                     the order it is done, what blocks each and whether it is the asker's;
                     publication.py: publishing and unpublishing them (admins, no signature: once its
                     DID and the tenant's are signed) and the public catalogue;
                     signing.py: an issuer's or verifier's signing system (admins set it)
                     queues.py: an issuer's or verifier's queue at the broker (admins make,
                     rotate and delete it; its password shown once)
                     verifications.py: a verifier asking a wallet by QR to present a form's
                     credentials (OpenID4VP `purpose: verify`), its verdict kept and sent
                     to the verifier's queue
                     (`member_tenant` guards them; keyset paging by cursor)
                     domains.py: the tenant's linked domains, proved by DNS TXT
                     forms.py: the tenant's forms — the same whoever puts them (an issuer's
                     offer, a verifier's) —, made of catalogue fields, and the JSON Schema of their answers;
                     changed whole (`PUT`) and deleted; while `in_use` only taking more, never deleted;
                     custom_fields.py: the tenant's own fields, beside Almena's catalogue
                     (`custom:{key}` in forms, never published); the anchor's, Almena's
                     catalogue (by key, published, with category and source) — groups
                     too, whose parts it shapes (`PartIn`; in use, only growing);
                     value_domains.py: the anchor's value domains (`anchor_only`):
                     added, changed — while a field draws on one, it only grows —
                     and deleted while none does; coded fields of any tenant may
                     draw on them (`domain`);
                     categories.py: the anchor's categories for fields and credential
                     types (`anchor_only`): added, renamed, deleted while nothing is
                     filed under them. Everything the anchor keeps only it changes;
                     what a tenant makes of its own only it sees and uses;
                     tenants.py: the user's tenants and one's details — name, mediator,
                     signing flow and the languages it works in (`Tenant.languages`, of
                     `field_catalog.LANGUAGES`; a new one starts with its creator's, the
                     anchor has them all: `languages_anchor`) — and its health;
                     accounts.py: a tenant's subscription (read), and the anchor's admins
                     listing every other tenant (an account) and setting or removing its
                     subscription by hand (`anchor_admin` guards them);
                     credential_types.py: the anchor's credential types (Almena's) and a
                     tenant's own (`custom:{key}`, its issuers and forms only, published
                     under its slug; `own_credential_types` entitlement);
                     fields and types are edited (never their key) and deleted, and
                     while something uses them a change may only take more;
                     issuer_credentials.py: the credential types an issuer grants, and
                     the form for each (its offers);
                     issuance.py: an accepted application's claims, draft and the
                     signer's wallet request (`sign`, kind `credential`), once its
                     status list is signed;
                     status_lists.py: the public list (`/status-lists/{slug}`, outside
                     /api/v1), the issuer's lists and their signing, and suspending,
                     reinstating or revoking a credential (`sign`, kind `status_list`);
                     applications.py: holders applying for an offer (no account: a
                     secret): start, answers, files and what they sign;
                     application_wallet.py: the wallet's pair/present/submit/receive
                     requests on the sign-in channel, and collecting an issued credential;
                     offers.py: the public offers (`/catalog/offers`, one by one);
                     inbox.py: the issuer's inbox, a page at a time, and deciding;
                     fields.py: the field and credential catalogues, public (`/catalog/fields`,
                     `/catalog/credentials`; outside /api/v1, on the identity domain, their
                     schemas `/schemas/fields/{version}[/{name}]` and
                     `/schemas/credentials/{version}[/{name}]`, and SD-JWT VC Type Metadata
                     at `/.well-known/vct/{path}`)
                     members.py: members and invitations (`admin_of` guards inviting, roles,
                     removing and taking invitations back; anyone leaves; a tenant keeps an
                     admin and its signers; an account left with none gets `own_tenant`);
                     auth.py's `_sign_in` turns pending invitations into membership;
                     mcp.py: the MCP endpoint at `/mcp` (outside /api/v1; JSON-RPC, stateless)
  models/            SQLAlchemy models; import each one in models/__init__.py
migrations/          Alembic (env.py reads the settings and models)
tests/               pytest (async, httpx ASGITransport)
```

## Rules

- Red `#e0413a` is the API's identity: `--brand` in `assets/home.css` and `BRAND`
  in `api/routes/home.py` (the favicon), carried by the mark in the header and footer
  of the page at `/`.

- Everything is written in English: code, comments, docs, commit messages.
- Use `task` for everything (`task --list`); `task check` must pass before finishing.
- mypy runs in strict mode: type every function.
- Database access is async (`AsyncSession`); no sync drivers.
- Schema changes go through an Alembic migration (`task db:revision -- "…"`),
  reviewed by hand; never `Base.metadata.create_all` outside tests.
- New settings get a `REGISTRY_` variable in `config.py`, `.env.example`,
  `compose.yml` (when the container needs it) and the README table.
- The API validates everything it is sent, whatever the portal checked first:
  required fields, lengths, formats, allowed values, cross-field rules and what
  an item's state allows. A rule the portal checks is a rule here too, with a
  test; the portal's checks only spare people a round trip.
- New endpoints come with tests and keep the README endpoint table current.
