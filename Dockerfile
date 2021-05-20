FROM eeacms/plone:5.2.4-17
LABEL maintainer="EEA: IDM2 A-Team <eea-edw-a-team-alerts@googlegroups.com>"

RUN mv /plone/instance/versions.cfg /plone/instance/eea-versions.cfg

COPY src/docker/* /
COPY src/plone/* /plone/instance/

RUN /docker-setup.sh
