"""What every code the API answers with means, in one place.

`REFUSALS`: each code a refusal carries (`{"detail": "<code>"}`, `errors.py`),
and what it tells whoever sent the request. `PROBLEMS`: each code a
verification gives for a presented credential it does not believe
(`presentations.Problem`). Both are published in the OpenAPI document
(`x-refusals`, `x-presentation-problems`), which the documentation's
reference is generated from; `tests/test_refusals.py` checks that every code
the source raises is here, and that nothing here is raised no more.
"""

REFUSALS: dict[str, str] = {
    # Signing in, ways in and tokens
    "not_authenticated": "No valid sign-in or API token came with the request.",
    "invalid_code": "The six-digit code is wrong or has expired.",
    "too_many_codes": (
        "A code was sent too recently, or too many to this address: wait for `Retry-After`."
    ),
    "too_many_attempts": (
        "Too many wrong codes for this address, or this code: wait for `Retry-After`."
    ),
    "too_many_requests": "Too many requests from this network address: wait for `Retry-After`.",
    "mail_unavailable": "The email could not be sent now; try again later.",
    "unknown_provider": "There is no sign-in provider by that name.",
    "provider_disabled": "That provider is not configured on this registry.",
    "provider_error": "The sign-in provider failed or refused the exchange.",
    "email_unverified": "The provider does not vouch for the account's email.",
    "invalid_state": "The sign-in's state is unknown, used, expired or another account's.",
    "invalid_ticket": "The move ticket is unknown, used or expired.",
    "not_empty": "The account holds something now, so it cannot be deleted to move.",
    "last_way_in": "It is the account's only way in: link another one first.",
    "account_not_found": "The account has no linked way in by that id.",
    "sign_in_required": "Only a sign-in can do this, not an API token.",
    "too_many_tokens": "The account has as many API tokens as it may keep: revoke one first.",
    "token_not_found": "The account has no API token by that id.",
    "wrong_poll": "The poll's secret is not the one the request was made with.",
    # Wallet requests and answers
    "request_not_found": "There is no wallet request by that id.",
    "request_expired": "The wallet request has expired: start again.",
    "already_answered": "The wallet request has been answered already.",
    "body_too_large": (
        "The request's body is larger than this endpoint takes (a wallet's answer, "
        "or anything past the API's limit): `max_bytes` says how large it may be."
    ),
    "invalid_token": "The wallet's token is not a valid JWS, or not for this request.",
    "invalid_did": "The DID the wallet answers with cannot be read.",
    "invalid_signature": "The signature does not hold over what was sent.",
    "invalid_proof": "The proof over the identity's log entry does not hold.",
    "not_canonical": "The log entry is not written as its canonical JSON.",
    "not_your_key": "The answer is signed by another key than the account's wallet.",
    "not_an_update_key": "The key that signed is not one of the identity's update keys.",
    "not_a_signer": "Your wallet's key may not sign this: you are not its signer.",
    "no_signers": "Nobody who may sign it has an Almena wallet linked.",
    "up_to_date": "The identity is already signed by a key in force: nothing to sign.",
    "log_moved": "The identity's log changed while it was being signed: sign again.",
    # Accounts (tenants), members and subscriptions
    "tenant_not_found": "You belong to no account by that id.",
    "tenant_required": (
        "Filtering by an account's own type (`custom:…`) needs the `tenant` it belongs to."
    ),
    "not_admin": "Only an admin of the account may do this.",
    "anchor_only": "Only the trust anchor (Almena's own account) may do this.",
    "name_required": "The name is empty.",
    "languages_invalid": "A language is not one of the platform's, or none is left.",
    "languages_anchor": "The trust anchor works in every language: its languages cannot change.",
    "language_not_enabled": "A text is in a language the account does not work in.",
    "signer_required": "The single-user signing flow names nobody to sign.",
    "signer_not_member": "Whoever is named to sign is not a member of the account.",
    "already_member": "That address belongs to a member already.",
    "invitation_not_found": "The account has no pending invitation by that id.",
    "member_not_found": "The account has no member by that id.",
    "last_admin": "It would leave the account with no admin.",
    "member_signs": (
        "They sign for the account, or for one of its issuers or verifiers: choose somebody "
        "else first."
    ),
    "subscription_required": (
        "The account's subscription does not let it keep its own fields or credential types."
    ),
    "plan_invalid": "Not one of the plans.",
    "period_end_invalid": "An active subscription's end is already past.",
    # Issuers, verifiers, mediators, identities and publishing
    "issuer_not_found": "There is no issuer by that id (or slug) here.",
    "verifier_not_found": "There is no verifier by that id here.",
    "mediator_not_found": (
        "There is no mediator by that id: neither the account's nor a public one."
    ),
    "identity_not_found": "The account has no identity by that id.",
    "identity_in_use": (
        "The account, or one of its issuers, verifiers or mediators, acts as this identity."
    ),
    "identity_signed": "Its DID exists and others may rely on it: it is not deleted.",
    "identity_pending": "Its identity is not signed yet: sign it first.",
    "tenant_pending": "The account's own identity is not signed yet: sign it first.",
    "signer_needed": "Its signing flow names nobody with a wallet to sign.",
    "subdomain_invalid": "The subdomain is not a valid DNS label.",
    "domain_unverified": "The domain is not one of the account's verified domains.",
    "verifier_unpublished": "Only a published verifier may ask a wallet for credentials.",
    # Domains
    "domain_invalid": "Not a domain name the account can link.",
    "domain_exists": "The account has linked that domain already.",
    "domain_not_found": "The account has no domain by that id.",
    "dns_record_not_found": (
        "The domain's TXT record was not found, or no longer says what it should."
    ),
    "dns_unavailable": "DNS could not be asked now; try again later.",
    # Queues
    "queue_exists": "It has a queue already.",
    "queue_not_found": "It has no queue.",
    "broker_unavailable": "The message broker cannot be reached now; try again later.",
    # Catalogue: fields, value lists, categories, credential types
    "field_not_found": "There is no field by that id.",
    "field_in_use": (
        "Something uses the field, and the change would turn away what it already took."
    ),
    "key_invalid": "The key is not lower snake case, or is too long.",
    "key_exists": "The account has something by that key already.",
    "key_reserved": "The key is one of Almena's catalogue: choose another.",
    "labels_required": "A label is missing (the trust anchor's need every language).",
    "descriptions_required": "A description is missing (the trust anchor's need every language).",
    "source_required": "The trust anchor's own entries name the standard they come from.",
    "category_invalid": "Not one of the catalogue's categories.",
    "category_not_found": "There is no category by that id.",
    "category_in_use": "A field, or a credential type of any account, is filed under it.",
    "icon_invalid": "Not one of the icons categories may take.",
    "constraint_invalid": "A length, range or other restriction does not fit the field's type.",
    "pattern_invalid": "The pattern is not a valid regular expression.",
    "options_invalid": "The options are missing, repeated or without a label.",
    "parts_invalid": "A group's parts are missing, repeated or not valid.",
    "formats_invalid": "A file format is not one the platform knows.",
    "codes_invalid": "The list's codes are missing, repeated or not of its kind.",
    "media_type_invalid": "Not a valid media type.",
    "domain_in_use": (
        "A field draws on this list, and the change would take a code away or change it."
    ),
    "claims_invalid": "A claim is not a field of the catalogue, is repeated, or is a file.",
    "credential_type_invalid": (
        "Not a credential type of the catalogue, or not one an account issues."
    ),
    "credential_type_not_found": "There is no credential type by that id.",
    "credential_type_in_use": "Something uses the type, and the change would alter what it is.",
    "issuance_invalid": (
        "An account's own types are issued by its issuers (`almena`); only the trust anchor's "
        "may be issued elsewhere."
    ),
    "vct_invalid": (
        "A `vct` goes with a type issued under its own framework (`external`), and must be a "
        "valid URI."
    ),
    "w3c_type_invalid": "The W3C type is not a valid name.",
    "mdoc_doctype_invalid": "The mdoc doctype is not a valid name.",
    "grants_issuers_only": "Only issuers grant credential types: `grants` filters issuers alone.",
    "version_not_found": "There is no catalogue of that version.",
    "not_found": "Nothing is published there (or not signed yet).",
    "invalid_cursor": "The cursor is not one this list gave.",
    # Forms
    "form_not_found": "The account has no form by that id.",
    "form_in_use": (
        "The form is in use: it may only take more than it took (`field` or `credential` says "
        "where)."
    ),
    "fields_required": "The form asks for nothing.",
    "field_unknown": "A field is not in the catalogue (`field` says which).",
    "field_key_invalid": "A field's own name is not lower snake case (`field` says which).",
    "field_key_duplicate": "Two fields have the same name (`field` says which).",
    "field_narrow_invalid": "A field's restriction does not fit it (`field` says which).",
    "field_rename_invalid": "Only a repeatable field takes a name of its own (`field` says which).",
    "credential_unknown": (
        "A credential asked for is not in the catalogue (`credential` says which)."
    ),
    "credential_key_invalid": (
        "A credential's name in the form is not valid (`credential` says which)."
    ),
    "credential_key_duplicate": (
        "Two credentials have the same name in the form (`credential` says which)."
    ),
    "credential_type_duplicate": (
        "The same credential type is asked for twice (`credential` says which)."
    ),
    "credential_claims_invalid": (
        "A credential's claims are not fields of its type (`credential` says which)."
    ),
    "credential_trust_invalid": (
        "Whom a credential is trusted from is not valid (`credential` says which)."
    ),
    "request_form_invalid": "Not one of the account's forms, or not for a type the issuer grants.",
    # Applying (the holder's side)
    "offer_not_found": "The issuer does not offer that credential type.",
    "application_not_found": "There is no application by that id, or not with this secret.",
    "application_expired": (
        "The application was not sent within a day, or it was sent and its latest step is "
        "older than `REGISTRY_APPLICATION_HOLDER_DAYS`: it can no longer be opened."
    ),
    "application_gone": (
        "The application is gone or no longer accepted, so the signed credential is not issued."
    ),
    "application_not_paired": "Pair a wallet with the application first.",
    "application_closed": "The application has been sent already.",
    "already_paired": "A wallet is paired with the application already.",
    "answers_invalid": "Some answers do not meet the form (`errors` says which and why).",
    "answers_incomplete": "Answer the form, and present the credentials it needs, before sending.",
    "nothing_to_present": "The form asks for no credentials.",
    "not_the_holder": "It is signed by another key than the wallet that paired.",
    "digest_mismatch": "The digest is not that of the application's content.",
    "invalid_vp_token": "The presentation (`vp_token`) cannot be read.",
    "file_field_unknown": "The form asks for no file by that key.",
    "file_empty": "The file is empty.",
    "file_too_large": (
        "The file is larger than the form allows: `max_bytes` says how large it may be."
    ),
    "file_type_invalid": "The form does not accept that kind of file.",
    "file_not_found": "The application has no file by that key.",
    "already_received": "The credential has been taken already.",
    "not_issued": "The credential has not been issued yet.",
    # Deciding and issuing (the issuer's side)
    "already_decided": "The application was decided already.",
    "not_accepted": "Accept the application first.",
    "already_issued": "The credential has been issued already.",
    "not_paired": "No wallet is paired with the application, so there is no holder to issue to.",
    "no_draft": "There is no credential prepared to sign: save its claims first.",
    "valid_until_invalid": "The end of validity must be after today.",
    "not_the_issuers_signer": "Only the issuer's signer may do this.",
    "status_list_unsigned": "The issuer's status list must be signed first.",
    "status_list_not_found": "The issuer has no status list by that id.",
    "status_list_changed": "The status list changed while it was being signed: sign again.",
    "status_list_gone": "The status list is no longer there.",
    "no_status_list": "Issued before status lists: it cannot be suspended or revoked.",
    "status_unchanged": "The credential already has that status.",
    "credential_revoked": "The credential is revoked: that is final.",
    # Verifications
    "verification_not_found": "The verifier has no verification by that id.",
}

PROBLEMS: dict[str, str] = {
    "format_invalid": "It is not a well-formed SD-JWT VC.",
    "signature_invalid": "Its signature does not hold.",
    "disclosure_invalid": "A disclosure is not one the credential lists.",
    "holder_binding_invalid": "The key-binding JWT is missing or not by the credential's key.",
    "audience_mismatch": "It was presented to someone else.",
    "nonce_mismatch": "It was presented for another request.",
    "type_mismatch": "It is not of the type asked for.",
    "claims_missing": "It lacks data the request needs.",
    "issuer_unresolvable": "Its issuer's DID could not be resolved.",
    "issuer_untrusted": "Its issuer is not one the request trusts.",
    "trust_framework_unsupported": "Credentials of its framework cannot be verified yet.",
    "not_yet_valid": "It is not valid yet.",
    "expired": "It has expired.",
    "suspended": "Its issuer has suspended it.",
    "revoked": "Its issuer has revoked it.",
    "status_unverifiable": "Its status list could not be read or believed.",
}
