FROM plone:5.1.5
LABEL maintainer="Alin Voinea <contact@avoinea.com>"

RUN mv /docker-entrypoint.sh /plone-entrypoint.sh \
 && mv -v versions.cfg plone-versions.cfg \
 && mv -v /plone/instance/buildout.cfg /plone/instance/buildout-core.cfg

COPY src/docker/* /
COPY src/plone/* /plone/instance/
RUN /docker-setup.sh

RUN buildDeps="build-essential libldap2-dev libsasl2-dev libssl-dev git" \
               && apt-get update \
               && apt-get install -y --no-install-recommends $buildDeps

COPY site.cfg /plone/instance/
RUN gosu plone buildout -c site.cfg annotate
RUN gosu plone buildout -c site.cfg
