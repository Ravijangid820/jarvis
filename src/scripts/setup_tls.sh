#!/usr/bin/env bash
# Generate a local Certificate Authority + a server certificate for the Jarvis orchestrator, so the
# LAN link (browser + camera/voice agents → server) can run over HTTPS with real verification.
#
# Why a local CA (not Let's Encrypt): the box is reached by LAN IP / a local hostname, not a public
# domain, so public ACME can't issue for it. We make our own CA, trust it on each client, and issue
# the server a cert for its IP + hostname(s). Re-running reuses the existing CA (so already-trusted
# clients keep working) and only re-issues the server cert.
#
#   bash src/scripts/setup_tls.sh                         # SANs: 127.0.0.1, 192.168.0.101, localhost, jarvis.local
#   TLS_IP=192.168.1.50 TLS_HOSTS="localhost jarvis.lan" bash src/scripts/setup_tls.sh
#
# After running: enable HTTPS on the service (systemd drop-in below) and install tls/ca.crt as a
# trusted root on each client (browser + agents).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TLS="$ROOT/tls"
SVC_USER="${TLS_SERVICE_USER:-jarvis}"      # the unprivileged user the orchestrator runs as
IP="${TLS_IP:-192.168.0.101}"
HOSTS="${TLS_HOSTS:-localhost jarvis.local jarvis}"
DAYS_CA=3650
DAYS_CERT=825                                # browsers reject leaf certs valid >825 days

mkdir -p "$TLS"

# Subject Alternative Names — a client accepts the cert only if the name/IP it connects to is listed.
SAN="IP:127.0.0.1,IP:${IP}"
for h in $HOSTS; do SAN="${SAN},DNS:${h}"; done

# 1) Certificate Authority — created once and REUSED (don't clobber a CA clients already trust).
if [ ! -f "$TLS/ca.key" ]; then
  echo "▸ creating local CA"
  openssl genrsa -out "$TLS/ca.key" 4096
  # A CA cert MUST carry basicConstraints=CA:TRUE + keyUsage=keyCertSign — OpenSSL 3.x strict
  # verification (used by Python's ssl) rejects a CA without them.
  openssl req -x509 -new -nodes -key "$TLS/ca.key" -sha256 -days "$DAYS_CA" \
    -subj "/O=Jarvis/CN=Jarvis Local CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$TLS/ca.crt"
else
  echo "▸ reusing existing CA ($TLS/ca.crt)"
fi

# 2) Server key + certificate, signed by the CA, valid for the SANs above.
echo "▸ issuing server cert for: $SAN"
openssl genrsa -out "$TLS/server.key" 2048
openssl req -new -key "$TLS/server.key" -subj "/O=Jarvis/CN=${IP}" -out "$TLS/server.csr"
cat > "$TLS/server.ext" <<EOF
subjectAltName=${SAN}
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EOF
openssl x509 -req -in "$TLS/server.csr" -CA "$TLS/ca.crt" -CAkey "$TLS/ca.key" -CAcreateserial \
  -days "$DAYS_CERT" -sha256 -extfile "$TLS/server.ext" -out "$TLS/server.crt"
rm -f "$TLS/server.csr" "$TLS/server.ext"

# 3) Permissions: the service must READ server.key/crt; the CA *private* key stays root-only.
chown "$SVC_USER:$SVC_USER" "$TLS/server.key" "$TLS/server.crt" 2>/dev/null || true
chmod 640 "$TLS/server.key"; chmod 644 "$TLS/server.crt" "$TLS/ca.crt"
chmod 600 "$TLS/ca.key"                      # CA key: signing only, keep locked down

# This CA is unique to THIS deployment — it is NOT committed. Copy the public cert to each device
# (this file, or GET /ca.crt). Print its fingerprint so a device can verify the copy it received
# matches (defeats a tampered transfer).
FP="$(sha256sum "$TLS/ca.crt" | awk '{print $1}')"

echo
echo "Done. Files in $TLS/ :  ca.crt (public)  server.crt  server.key  ca.key (keep secret, never commit)"
echo "CA fingerprint (SHA-256 of ca.crt) — clients compare against this after fetching:"
echo "  $FP"
echo "Next:"
echo "  1) Enable HTTPS:  install systemd/jarvis-orchestrator.service.d/tls.conf → daemon-reload + restart"
echo "  2) Trust this CA on each device — copy  $TLS/ca.crt  to it (or download https://${IP}:5000/ca.crt):"
echo "     • camera agent: put it at  camera/config/ca.crt"
echo "     • browser/phone: import it as a trusted root CA"
echo "     (compare the file's SHA-256 to the fingerprint above before trusting.)"
