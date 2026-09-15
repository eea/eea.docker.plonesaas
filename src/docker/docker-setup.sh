#!/bin/bash
set -e

buildDeps="
  build-essential
  libldap2-dev
  libsasl2-dev
  libssl-dev
"

runDeps="
  curl
  git
  subversion
  vim
"

echo "========================================================================="
echo "Installing $buildDeps"
echo "========================================================================="

# Replace live Debian mirrors with a frozen snapshot. The deb.debian.org CDN
# currently returns 404s for security packages while snapshot.debian.org is
# consistent.
printf '%s\n' \
  'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260722T000000Z bullseye main' \
  'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20260722T000000Z bullseye-security main' \
  'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260722T000000Z bullseye-updates main' \
  > /etc/apt/sources.list

rm -rf /var/lib/apt/lists/*
apt-get -o Acquire::Check-Valid-Until=false update
apt-get install -y --no-install-recommends $buildDeps

echo "========================================================================="
echo "Fixing permissions for packages checked-out in upstream image..."
echo "========================================================================="

find /plone/instance/src -not -user root -exec chown root:root {} \+

echo "========================================================================="
echo "Running buildout -c buildout.cfg"
echo "========================================================================="

buildout -c buildout.cfg

echo "========================================================================="
echo "Unininstalling $buildDeps"
echo "========================================================================="

apt-get purge -y --auto-remove $buildDeps


echo "========================================================================="
echo "Installing $runDeps"
echo "========================================================================="

apt-get install -y --no-install-recommends $runDeps


echo "========================================================================="
echo "Cleaning up cache..."
echo "========================================================================="

rm -rf /var/lib/apt/lists/*
rm -rf /plone/buildout-cache/downloads/*
rm -rf /tmp/*

echo "========================================================================="
echo "Fixing permissions..."
echo "========================================================================="

find /data  -not -user plone -exec chown plone:plone {} \+
find /plone -not -user plone -exec chown plone:plone {} \+
