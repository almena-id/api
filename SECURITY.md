# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: on
[almena-network/registry-api](https://github.com/almena-network/registry-api),
open the **Security** tab and choose **Report a vulnerability**. Do not open a
public issue, pull request or discussion about it.

Include what you can of:

- the version or commit;
- what an attacker can do, and under which configuration;
- steps or a proof of concept to reproduce it.

We aim to acknowledge a report within 3 working days and to agree on a
disclosure date with you once the issue is understood. We credit reporters in
the release notes unless you prefer otherwise.

## Supported versions

The project is before its first release: only the `main` branch receives
security fixes.

## Scope

In scope, among others:

- access to or modification of registry data without authorization;
- injection (SQL or otherwise) and input validation flaws;
- CORS or proxy-header misconfiguration in the shipped defaults;
- anything that exposes secrets such as the database credentials.

Out of scope:

- the development setup (`compose.yml`, `.env.example`, `task dev`), e.g.
  PostgreSQL published on loopback or `/docs` enabled in `development`;
- denial of service through sheer traffic volume;
- vulnerabilities in dependencies with no demonstrated impact on this
  project (report those upstream).
