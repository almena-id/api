# OpenBao's server configuration under Docker Compose (`task openbao`).
# The keys of the platform's issuers, verifiers and mediators will live here.

ui = true

# Raft (integrated storage) keeps everything in the openbao-data volume. It
# never swaps to disk itself, so mlock is not needed (nor the IPC_LOCK
# capability).
disable_mlock = true

storage "raft" {
  path    = "/openbao/file"
  node_id = "openbao"
}

# Plain HTTP inside Docker's network: Compose publishes the port on loopback
# only, and TLS is the edge's job wherever it is reached from outside.
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

api_addr     = "http://openbao:8200"
cluster_addr = "http://openbao:8201"

# Every request and answer, secrets hashed, to the container's output
# (`docker compose logs openbao`). Declared here: OpenBao no longer takes audit
# devices through its API.
audit "file" "stdout" {
  options {
    file_path = "stdout"
  }
}
