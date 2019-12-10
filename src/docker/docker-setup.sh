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

apt-get update
apt-get install -y --no-install-recommends $buildDeps

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
