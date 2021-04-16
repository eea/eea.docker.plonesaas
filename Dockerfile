FROM eeacms/plonesaas:5.2.4-8
LABEL maintainer="EEA: IDM2 A-Team <eea-edw-a-team-alerts@googlegroups.com>"

COPY src/docker/* /
COPY src/plone/site.cfg /plone/instance/

RUN /docker-setup.sh
