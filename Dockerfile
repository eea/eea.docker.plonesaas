FROM eeacms/plone:5.2.13-45
LABEL maintainer="EEA: IDM2 A-Team <eea-edw-a-team-alerts@googlegroups.com>"

RUN mv /plone/instance/versions.cfg /plone/instance/eea-versions.cfg

COPY src/docker/* /
COPY src/plone/* /plone/instance/
RUN pip uninstall -y setuptools && pip install setuptools==57.5.0

RUN /docker-setup.sh
