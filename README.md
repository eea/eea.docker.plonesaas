# Plone 5 with RelStorage, RestAPI, ElasticSearch, Memcached, Async, Graylog, Sentry and more support

Plone 5 with built-in support for:
* RelStorage
* RestAPI
* ElasticSearch
* Memcached
* zc.async
* Graylog
* Sentry
* Taxonomy
* Faceted Navigation
* Faceted Inheritance
* Image Cropping
* Easy Form

This image is generic, thus you can obviously re-use it within your own projects.

## Supported tags and respective Dockerfile links

  - [Tags](https://hub.docker.com/r/eeacms/plonesaas/tags/)

## Base docker image

 - [hub.docker.com](https://hub.docker.com/r/eeacms/plonesaas/)

## Source code

  - [github.com](http://github.com/eeacms/eea.docker.plonesaas)

## Simple Usage

    $ docker-compose up -d

Now, ask for http://localhost/ in your workstation web browser and add a Plone site (default credentials `admin:admin`).

See [docker-compose.yml](http://github.com/eeacms/eea.docker.plonesaas) for more details and more about Plone at [plone](https://hub.docker.com/_/plone)

## Extending this image

For this you'll have to provide the following custom files:

* `site.cfg`
* `Dockerfile`

Below is an example of `site.cfg` and `Dockerfile` to build a custom version of Plone with some add-ons based on this image:

**site.cfg**:

    [buildout]
    extends = buildout.cfg

    [configuration]
    eggs +=
      eea.facetednavigation
      collective.elasticsearch
      collective.taxonomy

    [versions]
    eea.facetednavigation = 11.7
    collective.elasticsearch = 3.0.2
    collective.taxonomy = 1.5.1


**Dockerfile**:

    FROM eeacms/plonesaas

    COPY site.cfg /plone/instance/
    RUN gosu plone buildout -c site.cfg

and then run

    $ docker build -t plone-rocks .


## Supported environment variables

See **eeacms/plone** [supported environment variables](https://github.com/eea/eea.docker.plone#supported-environment-variables)
