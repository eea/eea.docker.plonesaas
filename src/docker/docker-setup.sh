#!/bin/bash
set -e

buildDeps="
  build-essential
  curl
  git
  libc6-dev
  libexpat1-dev
  libjpeg-dev
  libldap2-dev
  libmemcached-dev
  libpq-dev
  libreadline-dev
  libsasl2-dev
  libssl-dev
  libxml2-dev
  libxslt-dev
  libxslt1-dev
  libz-dev
  zlib1g-dev
"

runDeps="
  curl
  ghostscript
  git
  graphviz
  gsfonts
  libjpeg62
  libmemcached11
  libpng16-16
  libpq5
  librsvg2-bin
  libssl1.0-dev
  libxml2
  libxslt1.1
  lynx
  poppler-utils
  subversion
  tex-gyre
  vim
  wv
"

echo "========================================================================="
echo "Installing $buildDeps"
echo "========================================================================="

apt-get update
apt-get install -y --no-install-recommends $buildDeps

echo "========================================================================="
echo "Installing pip, zc.buildout and setuptools"
echo "========================================================================="

VERSION_CFG="/plone/instance/versions.cfg"

if [ -z "$PIP" ]; then
  PIP=$(cat $VERSION_CFG | grep "pip\s*=\s*" | sed 's/^.*\=\s*//g')
fi

if [ -z "$ZC_BUILDOUT" ]; then
  ZC_BUILDOUT=$(cat $VERSION_CFG | grep "zc\.buildout\s*=\s*" | sed 's/^.*\=\s*//g')
fi

if [ -z "$SETUPTOOLS" ]; then
  SETUPTOOLS=$(cat $VERSION_CFG | grep "setuptools\s*\=\s*" | sed 's/ *//g' | sed 's/=//g' | sed 's/[a-z]//g')
fi

echo "Running: pip install pip==$PIP zc.buildout==$ZC_BUILDOUT setuptools==$SETUPTOOLS"
pip install pip==$PIP zc.buildout==$ZC_BUILDOUT setuptools==$SETUPTOOLS

echo "========================================================================="
echo "SVN checkout products"
echo "========================================================================="

svn co https://svn.eionet.europa.eu/repositories/Zope/bundles/Eionet/trunk products

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

mkdir -p /data/log

touch /data/log/instance.log
touch /data/log/instance-Z2.log

touch /data/log/standalone.log
touch /data/log/standalone-Z2.log

touch /data/log/zeo_client.log
touch /data/log/zeo_client-Z2.log

touch /data/log/zeo_async.log
touch /data/log/zeo_async-Z2.log

touch /data/log/rel_async.log
touch /data/log/rel_async-Z2.log

touch /data/log/rel_client.log
touch /data/log/rel_client-Z2.log

# BBB - Backward compatibility
mkdir -p /plone/instance/var
rm -rf /plone/instance/var/log
ln -s /data/log /plone/instance/var/log
# BBB - end

chown -R plone:plone /plone /data
