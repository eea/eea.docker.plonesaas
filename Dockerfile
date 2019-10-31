FROM plone:5.1.5
LABEL maintainer="Alin Voinea <contact@avoinea.com>"

RUN mv /docker-entrypoint.sh /plone-entrypoint.sh \
 && mv -v versions.cfg plone-versions.cfg \
 && mv -v /plone/instance/buildout.cfg /plone/instance/buildout-core.cfg

COPY src/docker/* /
COPY src/plone/* /plone/instance/
RUN /docker-setup.sh
