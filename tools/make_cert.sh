#!/bin/sh
# Produce cert.der + key.der in the format the Pico's TLS stack accepts
# (DER certificate, DER SEC1 EC key).
#
#   tools/make_cert.sh mail.example.com
#       -> self-signed P-256 certificate, valid 3 years
#   tools/make_cert.sh mail.example.com fullchain.pem privkey.pem
#       -> convert an existing certificate (e.g. Let's Encrypt, issued with
#          `certbot certonly --key-type ecdsa ...`) instead
set -e
[ -n "$1" ] || { echo "usage: $0 HOSTNAME [cert.pem key.pem]"; exit 1; }
mkdir -p certs
if [ -n "$2" ]; then
    # Only the leaf certificate is sent by the Pico.
    openssl x509 -in "$2" -outform DER -out certs/cert.der
    openssl ec -in "$3" -outform DER -out certs/key.der
else
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
        -keyout certs/key.pem -out certs/cert.pem -days 1095 \
        -subj "/CN=$1" -addext "subjectAltName=DNS:$1"
    openssl x509 -in certs/cert.pem -outform DER -out certs/cert.der
    openssl ec -in certs/key.pem -outform DER -out certs/key.der
    rm certs/key.pem
fi
echo "Wrote certs/cert.der and certs/key.der"
