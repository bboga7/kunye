#!/bin/sh
# Makes a throwaway certificate chain for signing the test files.
# These keys sign nothing real and are not meant for anything else.
set -e
cd "$(dirname "$0")/certs"
openssl ecparam -name prime256v1 -genkey -noout -out ca.key
openssl req -new -x509 -key ca.key -subj "/CN=kunye test root/O=kunye tests" -days 3650 -out ca.crt -extensions ext -config ca.cnf
openssl ecparam -name prime256v1 -genkey -noout -out ee.key
openssl req -new -key ee.key -subj "/CN=kunye test signer/O=kunye tests" -out ee.csr -config ca.cnf
openssl x509 -req -in ee.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 3650 -extfile ee.cnf -extensions ext -out ee.crt
cat ee.crt ca.crt > chain.pem
