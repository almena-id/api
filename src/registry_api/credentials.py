"""A tenant's endorsement of its issuers, verifiers and mediators.

Each one the tenant publishes carries a membership credential (W3C VC Data
Model 2.0) issued by the tenant's DID, whose subject is the component's DID,
presented in the component's own `whois.vp` — what did:webvh resolvers fetch
for ``<did>/whois`` — by the tenant as its controller. It holds for a year and
is renewed by publishing again.

The registry builds them and signs none: the proofs (`eddsa-jcs-2022`) come
from an admin's wallet and are checked by `webvh.check_proof` before anything
is kept.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

CONTEXT = "https://www.w3.org/ns/credentials/v2"
MEMBERSHIP = "AlmenaMembership"
VALIDITY = timedelta(days=365)


def _moment(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def membership(
    *,
    issuer: str,
    subject: str,
    role: str,
    name: str,
    now: datetime,
) -> dict[str, Any]:
    """The unsigned endorsement of a component (`role`: issuer, verifier or
    mediator) by its tenant (`issuer`, by its DID)."""
    return {
        "@context": [CONTEXT],
        "id": f"urn:uuid:{uuid.uuid4()}",
        "type": ["VerifiableCredential", MEMBERSHIP],
        "issuer": issuer,
        "validFrom": _moment(now),
        "validUntil": _moment(now + VALIDITY),
        "credentialSubject": {
            "id": subject,
            "name": name,
            "role": role,
            "memberOf": {"id": issuer},
        },
    }
