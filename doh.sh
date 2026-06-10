#!/usr/bin/env bash
# DoH A-record lookup using only curl (no dig/kdig/python needed).
#
# Usage: ./doh.sh <domain> <doh-url>
#   e.g. ./doh.sh host.internal.example https://dns.example.org/dns-query
#
# Prints the IPv4 address the DoH resolver returns, or "no answer".
set -euo pipefail

domain="${1:?usage: $0 <domain> <doh-url>}"
doh="${2:?usage: $0 <domain> <doh-url>}"

# Encode the domain as DNS length-prefixed labels
# (e.g. host.example -> \004host\007example).
name=""
IFS='.' read -ra labels <<<"$domain"
for l in "${labels[@]}"; do
  name+=$(printf '\\%03o' "${#l}")   # 1-byte label length, as a 3-digit octal escape
  name+="$l"
done

# DNS query packet = 12-byte header + QNAME + 0x00 + QTYPE(A=1) + QCLASS(IN=1).
# Header: id=0, flags=0x0100 (recursion desired), qdcount=1, others 0.
# POST it as RFC 8484 wire-format and read the raw answer bytes.
bytes=( $(printf "\\0\\0\\1\\0\\0\\1\\0\\0\\0\\0\\0\\0${name}\\0\\0\\1\\0\\1" \
  | curl -s -H 'content-type: application/dns-message' --data-binary @- "$doh" \
  | od -An -tu1) )

n=${#bytes[@]}
if (( n < 12 )); then
  echo "no response (check the DoH url / network)"
  exit 1
fi

ancount=$(( bytes[6] * 256 + bytes[7] ))   # number of answers, from the DNS header
if (( ancount == 0 )); then
  echo "no answer (NXDOMAIN / empty)"
else
  # last 4 bytes of the message = the A record's IPv4 address
  echo "${bytes[n-4]}.${bytes[n-3]}.${bytes[n-2]}.${bytes[n-1]}"
fi
