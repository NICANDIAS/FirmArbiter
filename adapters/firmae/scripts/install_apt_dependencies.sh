#!/usr/bin/env bash

set -euo pipefail

SNAPSHOT_ID="${1:?snapshot ID required}"
PACKAGES_FILE="${2:?package-list path required}"

case "$SNAPSHOT_ID" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z)
        ;;
    *)
        echo "Invalid snapshot ID: $SNAPSHOT_ID" >&2
        exit 1
        ;;
esac

test -s "$PACKAGES_FILE"

APT_VERSION="$(
    dpkg-query \
        --show \
        --showformat='${Version}' \
        apt
)"

echo "Base APT version: $APT_VERSION"

if ! dpkg --compare-versions "$APT_VERSION" ge 2.4.11
then
    echo "Jammy snapshot support requires apt >= 2.4.11." >&2
    exit 1
fi

mapfile -t PACKAGES < <(
    sed \
        -e 's/[[:space:]]*#.*$//' \
        -e '/^[[:space:]]*$/d' \
        "$PACKAGES_FILE" |
    LC_ALL=C sort -u
)

if [ "${#PACKAGES[@]}" -eq 0 ]
then
    echo "Direct package list is empty." >&2
    exit 1
fi

PROVENANCE="/opt/veritas-adapter/provenance"

mkdir -p \
    "$PROVENANCE" \
    /etc/apt/sources.list.d \
    /etc/apt/apt.conf.d

# Remove all additional repository definitions.
find /etc/apt/sources.list.d \
    -maxdepth 1 \
    -type f \
    -delete

# Jammy repositories must explicitly declare snapshot support.
cat > /etc/apt/sources.list <<'SOURCES'
deb [snapshot=yes] http://archive.ubuntu.com/ubuntu jammy main universe restricted multiverse
deb [snapshot=yes] http://archive.ubuntu.com/ubuntu jammy-updates main universe restricted multiverse
deb [snapshot=yes] http://archive.ubuntu.com/ubuntu jammy-backports main universe restricted multiverse
deb [snapshot=yes] http://security.ubuntu.com/ubuntu jammy-security main universe restricted multiverse
SOURCES

# Force all snapshot-enabled repositories to this exact timestamp.
cat > /etc/apt/apt.conf.d/50veritas-snapshot <<SNAPSHOT
APT::Snapshot "${SNAPSHOT_ID}";
SNAPSHOT

printf '%s\n' "$SNAPSHOT_ID" \
    > "$PROVENANCE/ubuntu-snapshot.txt"

cp /etc/apt/sources.list \
    "$PROVENANCE/apt-sources.list"

cp /etc/apt/apt.conf.d/50veritas-snapshot \
    "$PROVENANCE/apt-snapshot.conf"

cp "$PACKAGES_FILE" \
    "$PROVENANCE/apt-direct.txt"

# Prevent packages from starting services during image construction.
POLICY_BACKUP=""

if [ -e /usr/sbin/policy-rc.d ]
then
    POLICY_BACKUP="$(mktemp)"
    cp /usr/sbin/policy-rc.d "$POLICY_BACKUP"
fi

restore_policy() {
    if [ -n "$POLICY_BACKUP" ]
    then
        cp "$POLICY_BACKUP" /usr/sbin/policy-rc.d
        rm -f "$POLICY_BACKUP"
    else
        rm -f /usr/sbin/policy-rc.d
    fi
}

trap restore_policy EXIT

cat > /usr/sbin/policy-rc.d <<'POLICY'
#!/bin/sh
exit 101
POLICY

chmod 0755 /usr/sbin/policy-rc.d

###############################################################################
# Bootstrap the CA bundle from the selected snapshot.
#
# TLS verification is disabled only because the minimal base has no CA bundle.
# Ubuntu archive signature and package-hash verification remain enabled.
###############################################################################

apt-get \
    -o Acquire::Retries=5 \
    -o APT::Update::Error-Mode=any \
    -o Acquire::https::Verify-Peer=false \
    -o Acquire::https::Verify-Host=false \
    update

apt-get \
    -o Acquire::Retries=5 \
    -o Acquire::https::Verify-Peer=false \
    -o Acquire::https::Verify-Host=false \
    install \
    --yes \
    --no-install-recommends \
    ca-certificates

update-ca-certificates

test -s /etc/ssl/certs/ca-certificates.crt

cat > "$PROVENANCE/ca-bootstrap.txt" <<BOOTSTRAP
Source: Ubuntu snapshot
Snapshot: ${SNAPSHOT_ID}
Package: ca-certificates
TLS verification disabled: initial CA bootstrap only
APT archive authentication disabled: no
Unauthenticated packages enabled: no
BOOTSTRAP

###############################################################################
# Repeat with normal TLS verification.
###############################################################################

rm -rf /var/lib/apt/lists/*

apt-get \
    -o Acquire::Retries=5 \
    -o APT::Update::Error-Mode=any \
    update

apt-cache policy \
    > "$PROVENANCE/apt-policy.txt"

if ! grep -F \
    "snapshot.ubuntu.com/ubuntu/${SNAPSHOT_ID}" \
    "$PROVENANCE/apt-policy.txt" \
    >/dev/null
then
    echo "APT is not using the selected snapshot." >&2
    cat "$PROVENANCE/apt-policy.txt" >&2
    exit 1
fi

###############################################################################
# Confirm that the snapshot is not older than critical base-image packages.
###############################################################################

printf 'package\tinstalled\tcandidate\n' \
    > "$PROVENANCE/base-snapshot-compatibility.tsv"

for package in \
    libc6 \
    perl-base \
    libudev1 \
    apt
do
    installed="$(
        dpkg-query \
            --show \
            --showformat='${Version}' \
            "$package"
    )"

    candidate="$(
        apt-cache policy "$package" |
        awk '/Candidate:/ {print $2; exit}'
    )"

    printf '%s\t%s\t%s\n' \
        "$package" \
        "$installed" \
        "$candidate" \
        >> "$PROVENANCE/base-snapshot-compatibility.tsv"

    if [ -z "$candidate" ] ||
       [ "$candidate" = "(none)" ]
    then
        echo "No snapshot candidate for $package." >&2
        exit 1
    fi

    if dpkg --compare-versions "$installed" gt "$candidate"
    then
        echo "Snapshot is older than the pinned base image." >&2
        echo "Package:   $package" >&2
        echo "Installed: $installed" >&2
        echo "Candidate: $candidate" >&2
        exit 1
    fi
done

cat "$PROVENANCE/base-snapshot-compatibility.tsv"

###############################################################################
# Install FirmAE dependencies.
###############################################################################

apt-get install \
    --yes \
    --no-install-recommends \
    "${PACKAGES[@]}"

for package in "${PACKAGES[@]}"
do
    dpkg-query \
        --show \
        --showformat='${binary:Package}\t${Version}\t${Architecture}\n' \
        "$package"
done |
LC_ALL=C sort \
    > "$PROVENANCE/apt-direct-lock.tsv"

dpkg-query \
    --show \
    --showformat='${binary:Package}\t${Version}\t${Architecture}\n' |
LC_ALL=C sort \
    > "$PROVENANCE/apt-installed-lock.tsv"

apt-mark showmanual |
LC_ALL=C sort \
    > "$PROVENANCE/apt-manual-packages.txt"

printf '%s\n' "$APT_VERSION" \
    > "$PROVENANCE/base-apt-version.txt"

sha256sum \
    "$PROVENANCE/ubuntu-snapshot.txt" \
    "$PROVENANCE/apt-sources.list" \
    "$PROVENANCE/apt-snapshot.conf" \
    "$PROVENANCE/apt-direct.txt" \
    "$PROVENANCE/base-snapshot-compatibility.tsv" \
    "$PROVENANCE/apt-direct-lock.tsv" \
    "$PROVENANCE/apt-installed-lock.tsv" \
    "$PROVENANCE/apt-manual-packages.txt" \
    "$PROVENANCE/ca-bootstrap.txt" \
    > "$PROVENANCE/dependency-files.sha256"

rm -rf /var/lib/apt/lists/*

echo "FirmAE deterministic dependency installation completed."
