# AGENTS.md

Guidance for coding agents working on `api`.

## Layout

```
src/registry_api/
  __main__.py        entry point (`registry-api`): runs uvicorn with the settings
  main.py            FastAPI app factory: CORS, routers, lifespan, Scalar docs
  config.py          Settings (pydantic-settings, REGISTRY_* variables)
  db.py              async engine, sessionmaker and the `get_session` dependency
  security.py        sign-in codes and session tokens (both stored as SHA-256)
  mail.py            SMTP mailer (`get_mailer` dependency) and the email texts, en/es
  mediators.py       recognising a mediator from its origin (its did:web document)
  oauth.py           social sign-in providers (Google, Microsoft, Apple, GitHub):
                     authorize URL, code exchange, the verified email they vouch for
  api/router.py      /api/v1 router: include new route modules here
  api/routes/        one module per resource (health.py lives outside /api/v1);
                     auth.py holds `current_session`, the dependency for signed-in routes
                     directory.py: a tenant's issuers, verifiers, identities
                     (`member_tenant` guards them; keyset paging by cursor)
                     members.py: members and invitations (`admin_of` guards inviting);
                     auth.py's `_sign_in` turns pending invitations into membership
  models/            SQLAlchemy models; import each one in models/__init__.py
migrations/          Alembic (env.py reads the settings and models)
tests/               pytest (async, httpx ASGITransport)
```

## Rules

- Everything is written in English: code, comments, docs, commit messages.
- Use `task` for everything (`task --list`); `task check` must pass before finishing.
- mypy runs in strict mode: type every function.
- Database access is async (`AsyncSession`); no sync drivers.
- Schema changes go through an Alembic migration (`task db:revision -- "…"`),
  reviewed by hand; never `Base.metadata.create_all` outside tests.
- New settings get a `REGISTRY_` variable in `config.py`, `.env.example`,
  `compose.yml` (when the container needs it) and the README table.
- New endpoints come with tests and keep the README endpoint table current.
