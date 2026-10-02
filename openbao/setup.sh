# Provisions OpenBao for the API, idempotently: the KV v2 mount its secrets
# live in, the policy over that mount alone, and the AppRole it signs in with.
# `task openbao:setup` pipes it into the container's shell after exporting
# BAO_TOKEN (the root token), MOUNT, ROLE_ID and SECRET_ID, so none of them
# shows in a command line on the host.
set -eu

# The mount: KV v2 keeps every version, which rotation needs.
if ! bao read "sys/mounts/$MOUNT" > /dev/null 2>&1; then
  bao secrets enable -path="$MOUNT" -version=2 kv
fi

# The API's policy: the whole mount, and nothing else.
bao policy write registry-api - <<POLICY
path "$MOUNT/data/*" {
  capabilities = ["create", "read", "update", "delete"]
}
path "$MOUNT/metadata/*" {
  capabilities = ["read", "list", "delete"]
}
POLICY

if ! bao auth list -format=json | grep -q '"approle/"'; then
  bao auth enable approle
fi

# Its tokens last an hour (the API signs in again), a day at most.
bao write auth/approle/role/registry-api \
  token_policies=registry-api token_ttl=1h token_max_ttl=24h \
  secret_id_ttl=0 secret_id_num_uses=0 > /dev/null
bao write auth/approle/role/registry-api/role-id role_id="$ROLE_ID" > /dev/null

# The secret id is .env's (task init makes it): registered once.
if ! bao write auth/approle/login role_id="$ROLE_ID" secret_id="$SECRET_ID" > /dev/null 2>&1; then
  bao write auth/approle/role/registry-api/custom-secret-id secret_id="$SECRET_ID" > /dev/null
fi
echo "OpenBao ready for the API: mount $MOUNT/, AppRole registry-api"
