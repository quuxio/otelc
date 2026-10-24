#!/usr/bin/env bash
# USAGE: bash ci/install_llvm22.sh [--help]
# Install the matched compiler/development tools on an Ubuntu 24.04 CI host.
set -euo pipefail

case "${1:-}" in
  -h|--help) sed -n '2,3s/^# //p' "${BASH_SOURCE[0]}"; exit 0 ;;
  '') ;;
  *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
esac

# shellcheck source=/dev/null
. /etc/os-release
if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != 24.04 ]]; then
  printf 'LLVM CI installation requires Ubuntu 24.04\n' >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install --yes --no-install-recommends ca-certificates curl gnupg
llvm_key=$(mktemp)
trap 'rm -f "$llvm_key"' EXIT
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  https://apt.llvm.org/llvm-snapshot.gpg.key --output "$llvm_key"
fingerprint=$(gpg --batch --show-keys --with-colons "$llvm_key" | awk -F: '$1=="fpr" {print $10; exit}')
test "$fingerprint" = 6084F3CF814B57C1CF12EFD515CF4D18AF4F7421
sudo install -m 0644 "$llvm_key" /usr/share/keyrings/otelc-llvm.asc
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/otelc-llvm.asc] https://apt.llvm.org/noble/ llvm-toolchain-noble-22 main' \
  | sudo tee /etc/apt/sources.list.d/otelc-llvm22.list >/dev/null
sudo apt-get update
sudo apt-get install --yes --no-install-recommends clang-22 llvm-22-dev libclang-rt-22-dev
/usr/lib/llvm-22/bin/llvm-config --version
/usr/lib/llvm-22/bin/clang --version
