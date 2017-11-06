#!/usr/bin/env python
# -*- coding: utf-8 -*-

# PEBA (Python EWS Backend API)
# v0.8.1 2017-11-06
# Authors: @vorband & @schmalle

import xml.etree.ElementTree as ET
import defusedxml.ElementTree as ETdefused

import hashlib
import json
import urllib.request, urllib.parse, urllib.error
import html
import datetime
from dateutil.relativedelta import relativedelta

from flask import Flask, request, abort, jsonify, Response, redirect
from flask_cors import CORS, cross_origin
from flask_elasticsearch import FlaskElasticsearch

from elasticsearch import Elasticsearch, ElasticsearchException
from werkzeug.contrib.fixers import ProxyFix

from functools import wraps
import putservice
from werkzeug.contrib.cache import MemcachedCache

###################
### Initialization
###################

app = Flask(__name__)
app.config.from_pyfile('/etc/ews/peba.cfg')
app.wsgi_app = ProxyFix(app.wsgi_app)
cors = CORS(app, resources={r"/alert/*": {"origins": app.config['CORSDOMAIN']}})

es = FlaskElasticsearch(app,
    timeout=app.config['ELASTICTIMEOUT']
)

cache = MemcachedCache([app.config['MEMCACHE']])

###############
### Functions
###############

def authentication_required(f):
    """ This login decorator verifies that the correct username
        and password are sent over POST in the XML format.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        postdata = request.data.decode('utf-8')

        if len(postdata) == 0:
            app.logger.error('Authentication: No xml post data in request')
            return abort(403)
        else:
            root = ETdefused.fromstring(postdata)
            user_data = root.find("./Authentication/username")
            pass_data = root.find("./Authentication/token")

            if user_data is None or pass_data is None:
                app.logger.error('Authentication: Invalid XML, token not present or empty')
                return abort(403)

            username = user_data.text
            password = pass_data.text

            if not authenticate(username, password):
                app.logger.error("Authentication failure for user %s", username)
                return abort(403)

            return f(*args, **kwargs)
    return decorated_function

@app.after_request
def add_header(response):
    response.headers["Access-Control-Allow-Credentials"] = "true"
    return response

def testElasticsearch():
    try:
        return es.ping()
    except:
        return False

def testMemcached():
    try:
        getCache("heartbeat", "test")
        return True
    except:
        return False

def getCache(cacheItem, cacheType):
    cacheTypeItem = cacheType + ":" + cacheItem
    rv = cache.get(cacheTypeItem)
    app.logger.debug("Returning item from cache: {0} - Value: {1}".format(cacheTypeItem, str(rv)[:200]+" ..."))
    if rv is None:
        return False
    return rv

def setCache(cacheItem, cacheValue, cacheTimeout, cacheType):
    try:
        cacheTypeItem = cacheType + ":" + cacheItem
        cache.set(cacheTypeItem, cacheValue, timeout=cacheTimeout)
        app.logger.debug("Setting item to cache: {0} - Value: {1}".format(cacheTypeItem, str(cacheValue)[:200]+" ..."))
    except:
        app.logger.error("Could not set memcache cache {0} to value {1} and Timeout {2}".format(cacheTypeItem, str(cacheValue), cacheTimeout))

def authenticate(username, token):
    """ Authenticate user from cache or in ES """

    # check for user in cache
    authtoken = getCache(username, "user")
    if authtoken is not False:
        if len(authtoken) == 128:
            tokenhash = hashlib.sha512(token.encode('utf-8')).hexdigest()
            if authtoken == tokenhash:
                return True
        elif len(authtoken) == 32:
            tokenhash = hashlib.md5(token.encode('utf-8')).hexdigest()
            if authtoken == tokenhash:
                return True
        else:
            app.logger.error('authenticate(): Hash "{0}" for user "{1}" is not matching md5 or sha512 length! Needs to be checked in memcache!'.format(authtoken, username))

    # query ES
    else:
        try:
            res = es.search(index=app.config['WSUSERINDEX'], body={
                  "query": {
                    "term": {
                      "peerName.keyword": username
                    }
                  }
                })

            if res["hits"]["total"] > 1:
                app.logger.error('authenticate(): More than one user "%s" in ES index "users" found!' % username)
            elif res["hits"]["total"] < 1:
                app.logger.error('authenticate(): No user "%s" in ES index "users" found!' % username)
            elif res["hits"]["total"] == 1:
                authtoken = res["hits"]["hits"][0]["_source"]["token"]
                getOnly = res["hits"]["hits"][0]["_source"]["getOnly"]
                community = res["hits"]["hits"][0]["_source"]["community"]

                if len(authtoken) == 128:
                    tokenhash = hashlib.sha512(token.encode('utf-8')).hexdigest()
                    if authtoken == tokenhash:
                        # add user and token to cache for 24h
                        setCache(username, authtoken, (60 * 60 * 24), "user")
                        return True
                elif len(authtoken) == 32:
                    tokenhash = hashlib.md5(token.encode('utf-8')).hexdigest()
                    if authtoken == tokenhash:
                        # add user and token to cache for 24h
                        setCache(username, authtoken, (60 * 60 * 24),"user")
                        return True
                else:
                    app.logger.error('authenticate(): Hash "{0}" for user "{1}" is not matching md5 or sha512 length! Needs to be checked in ES index!'.format(authtoken, username))
                    return False

        except ElasticsearchException as err:
            app.logger.error('ElasticSearch error: %s' %  err)

    return False

def checkCommunityUser():
    """ Checks if community credentials are used
    """
    postdata = request.data.decode('utf-8')

    if len(postdata) == 0:
        app.logger.error('no xml post data in request')
        return abort(403)
    else:
        root = ETdefused.fromstring(postdata)
        user_data = root.find("./Authentication/username")
        pass_data = root.find("./Authentication/token")

        if user_data is None or pass_data is None:
            app.logger.error('Invalid XML: token not present or empty')
            return abort(403)

        username = user_data.text
        password = pass_data.text

        if username == app.config['COMMUNITYUSER'] and password == app.config['COMMUNITYTOKEN']:
            return True

        if not authenticate(username, password):
            app.logger.error("simplePostMessage-Authentication failure for user %s", username)
            return abort(403)

        return False

def checkCommunityIndex(request):
    """check if request is agains community index or production index"""
    if not request.args.get('ci'):
        return True
    elif request.args.get('ci') == "0":
        return False
    return True

# GET functions

def queryBadIPs(badIpTimespan, clientDomain):
    """ Get IP addresses from alerts in elasticsearch """
    try:
        res = es.search(index=app.config['ELASTICINDEX'], body={
          "query": {
            "bool": {
              "must": [
                {
                  "range": {
                    "createTime": {
                        "gte": "now-%sm" % badIpTimespan
                    }
                  }
                },
                {
                  "match": {
                      "clientDomain": clientDomain
                    }
                }
              ]
            }
          },
          "aggs": {
            "ips": {
              "terms": {
                "field": "sourceEntryIp",
                "size": 1000000
              }
            }
          },
          "size": 0
        })
        return res["aggregations"]["ips"]
    except ElasticsearchException as err:
        app.logger.error('ElasticSearch error: %s' %  err)

    return False

def queryAlerts(maxAlerts, clientDomain):
    """ Get IP addresses from alerts in elasticsearch """
    try:
        res = es.search(index=app.config['ELASTICINDEX'], body={
            "query": {
                "match": {
                    "clientDomain": clientDomain
                }
            },
            "sort": {
                "recievedTime": {
                    "order": "desc"
                    }
                },
            "size": maxAlerts,
            "_source": [
                "createTime",
                "recievedTime",
                "peerIdent",
                "peerType",
                "country",
                "targetCountry",
                "originalRequestString",
                "location",
                "sourceEntryIp"
                ]
            })
        return res["hits"]["hits"]
    except ElasticsearchException as err:
        app.logger.error('ElasticSearch error: %s' %  err)

    return False

def queryAlertsWithoutIP(maxAlerts, clientDomain):
    """ Get IP addresses from alerts in elasticsearch """
    try:
        res = es.search(index=app.config['ELASTICINDEX'], body={
            "query": {
                "match": {
                    "clientDomain": clientDomain
                }
            },
            "sort": {
                "recievedTime": {
                    "order": "desc"
                    }
                },
            "size": maxAlerts,
            "_source": [
                "createTime",
                "peerType",
                "country",
                "originalRequestString",
                "location",
                "targetCountry",
                "countryName",
                "locationDestination",
                "recievedTime",
                "username",
                "password",
                "login"
                ]
            })
        return res["hits"]["hits"]
    except ElasticsearchException as err:
        app.logger.error('ElasticSearch error: %s' % err)

    return False

def queryAlertsCount(timeframe, clientDomain):
    """ Get number of Alerts in timeframe in elasticsearch """

    # check if timespan = d or number
    if timeframe == "day":
        span = "now/d"
    elif timeframe.isdecimal():
        span = "now-%sm" % timeframe
    else:
        app.logger.error('Non numeric value in retrieveAlertsCount timespan. Must be decimal number (in minutes) or string "day"')
        return False

    try:
        res = es.search(index=app.config['ELASTICINDEX'], body={
          "query": {
            "bool": {
              "must": [
                {
                  "match": {
                    "clientDomain": clientDomain
                  }
                }
              ],
              "filter": [
                {
                  "range": {
                    "createTime": {
                        "gte": str(span)
                    }
                  }
                }
              ]
            }
          },
          "size": 0
        })
        return res['hits']['total']
    except ElasticsearchException as err:
        app.logger.error('ElasticSearch error: %s' %  err)

    return False

def queryAlertsCountWithType(timeframe, clientDomain):
    """ Get number of Alerts in timeframe in elasticsearch """

    # check if timespan = d or number
    if timeframe == "day":
        span = "now/d"
    elif timeframe.isdecimal():
        span = "now-%sm" % timeframe
    else:
        app.logger.error('Non numeric value in retrieveAlertsCountWithType timespan. Must be decimal number (in minutes) or string "day"')
        return False

    try:
        res = es.search(index=app.config['ELASTICINDEX'], body={
          "query": {
            "range": {
              "createTime": {
                  "gte": str(span)
              }
            }
          },
          "aggs": {
            "communityfilter": {
              "filter": {
                "term": {
                  "clientDomain": clientDomain
                }
              },
              "aggs": {
                "honeypotTypes": {
                  "terms": {
                    "field": "peerType.keyword"
                  }
                }
              }
            }
          },
          "size": 0
        })
        return res
    except ElasticsearchException as err:
        app.logger.error('ElasticSearch error: %s' %  err)

    return False

def queryDatasetAlertsPerMonth(days, clientDomain):
    # check if months is a number
    if days is None:
        span = "now-1M/d"
    elif days.isdecimal():
        span = "now-%sd/d" % days
    else:
        app.logger.error('Non numeric value in datasetAlertsPerMonth timespan. Must be decimal number in days')
        return False

    try:
        res = es.search(index=app.config['ELASTICINDEX'], body={
              "query": {
                "range": {
                  "createTime": {
                    "gte": str(span)
                  }
                }
              },
            "aggs": {
                "communityfilter": {
                    "filter": {
                        "term": {
                            "clientDomain": clientDomain
                        }
                    },
            "aggs": {
                "range": {
                  "date_histogram": {
                    "field": "createTime",
                    "interval": "day"
                            }
                        }
                    }
                }
              },
              "size": 0
                })
        return res["aggregations"]["communityfilter"]["range"]
    except ElasticsearchException as err:
        app.logger.error('ElasticSearch error: %s' %  err)

    return False

def queryDatasetAlertTypesPerMonth(days, clientDomain):
    # check if days is a number
    if days is None:
        span = "now-1M/d"
    elif days.isdecimal():
        span = "now-%sd/d" % days
    else:
        app.logger.error('Non numeric value in datasetAlertsTypesPerMonth timespan. Must be decimal number in days')
        return False

    try:
        res = es.search(index=app.config['ELASTICINDEX'], body={
          "query": {
            "range": {
              "createTime": {
                "gte": span
              }
            }
          },
         "aggs": {
                "communityfilter": {
                    "filter": {
                        "term": {
                            "clientDomain": clientDomain
                        }
            },
          "aggs": {
            "range": {
              "date_histogram": {
                "field": "createTime",
                "interval": "day"
              },
              "aggs": {
                "nested_terms_agg": {
                  "terms": {
                    "field": "peerType.keyword"
                  }}}
                }
              }
            }
          },
          "size": 0
        })
        return res["aggregations"]["communityfilter"]["range"]
    except ElasticsearchException as err:
        app.logger.error('ElasticSearch error: %s' %  err)

    return False

def queryAlertStats(clientDomain):
    """ Get combined statistics from elasticsearch """
    try:
        res = es.search(index=app.config['ELASTICINDEX'], body={
            "aggs": {
                "communityfilter": {
                    "filter": {
                        "term": {
                            "clientDomain": clientDomain
                        }
                    },
            "aggs": {
            "ctr": {
              "range": {
                "field": "createTime",
                "ranges": [
                  {
                    "key": "1d",
                    "from": "now-1440m"
                  },
                  {
                    "key": "1h",
                    "from": "now-60m"
                  },
                  {
                    "key": "1m",
                    "from": "now-1m"
                  }
                ]
              }
            }}}
          },
          "size": 0
        })
        return res['aggregations']['communityfilter']['ctr']['buckets']
    except ElasticsearchException as err:
        app.logger.error('ElasticSearch error: %s' % err)

    return False

def queryTopCountriesAttacks(monthOffset, topX, clientDomain):
    # use THIS month
    if monthOffset is None or monthOffset == "0" :
        span = "now/M"
        monthOffset = 0
        span2 = "now"
    # check if months is a number
    elif monthOffset.isdecimal():
        span = "now-%dM/M" % int(monthOffset)
        span2 = "now-%dM/M" % (int(monthOffset)-1)
    else:
        app.logger.error('Non numeric value in topCountriesAttacks monthOffset. Must be decimal number in months')
        return False

    # use top10 default
    if topX is None:
        topx = 10
    # check if months is a number
    elif topX.isdecimal():
        topx = topX
    else:
        app.logger.error(
            'Non numeric value in topCountriesAttacks topX. Must be decimal number.')
        return False


    # Get top 10 attacker countries
    try:
        res = es.search(index=app.config['ELASTICINDEX'], body={
          "query": {
            "range": {
              "recievedTime": {
                "gte": span,
                "lt": span2
              }
            }
          },
            "aggs": {
                "communityfilter": {
                    "filter": {
                        "term": {
                            "clientDomain": clientDomain
                        }
                    },
                    "aggs": {
            "countries": {
              "terms": {
                "field": "country.keyword",
                "size" : str(topx)
              },
              "aggs": {
                "country": {
                  "top_hits": {
                    "size": 1,
                    "_source": {
                      "include": [
                        "countryName"
                      ]
                    }
                  }
                }}}
              }
            }
          },
          "size": 0
        })
    except ElasticsearchException as err:
        app.logger.error('ElasticSearch error: %s' % err)

    # Get top 10 attacked countries
    try:
        res2 = es.search(index=app.config['ELASTICINDEX'], body={
            "query": {
                "range": {
                    "recievedTime": {
                        "gte": span,
                        "lt": span2
                    }
                }
            },
            "aggs": {
                "communityfilter": {
                    "filter": {
                        "term": {
                            "clientDomain": clientDomain
                        }
                    },

                    "aggs": {
                "countries": {
                    "terms": {
                        "field": "targetCountry.keyword",
                        "size": str(topx)
                    },
                    "aggs": {
                        "country": {
                            "top_hits": {
                                "size": 1,
                                "_source": {
                                    "include": [
                                        "targetCountryName"
                                    ]
                                }
                            }}}
                        }
                    }
                }
            },
            "size": 0,
            "_source": [
                "createTime"
            ]
        })

        monthData = (datetime.date.today()+ relativedelta(months=-(int(monthOffset)))).strftime('%Y-%m')
        return [ res["aggregations"]["communityfilter"]["countries"]["buckets"], monthOffset, monthData, res2["aggregations"]["communityfilter"]["countries"]["buckets"] ]
    except ElasticsearchException as err:
        app.logger.error('ElasticSearch error: %s' % err)

    return False

def queryLatLonAttacks(direction, topX, dayoffset, clientDomain):
    # use default: Lat long of source
    if direction is None:
        locationString = "location"
    elif direction == "src":
        locationString = "location"
    elif direction == "dst":
        locationString = "locationDestination"
    else:
        app.logger.error('Invalid value in /retrieveLatLonAttacks direction. Must be "src" or "dest"')
        return False

    # use top10 default
    if topX is None:
        topx = 10
        # check if months is a number
    elif topX.isdecimal():
        topx = topX
    else:
        app.logger.error(
            'Non numeric value in /retrieveLatLonAttacks topX. Must be decimal number.')
        return False

    # statistics for 24 hours
    if dayoffset is None or dayoffset == "0":
        span = "now-24h"
        span2 = "now"
        dayoffset = 0
    # check if days is a number
    elif dayoffset.isdecimal():
        span = "now-%dd/d" % int(dayoffset)
        span2 = "now-%dd/d" % (int(dayoffset)-1)
    else:
        app.logger.error(
            'Non numeric value in /retrieveLatLonAttacks day offset. Must be decimal number.')
        return False


    # Get location strings
    try:
        res = es.search(index=app.config['ELASTICINDEX'], body={
          "query": {
            "range": {
              "createTime": {
                "gte": span,
                "lt": span2
              }
            }
          },
          "_source": [
            "location",
            "createTime"
          ],
          "size": 1,
            "aggs": {
                "communityfilter": {
                    "filter": {
                        "term": {
                            "clientDomain": clientDomain
                        }
                    },
          "aggs": {
            "topLocations": {
              "terms": {
                "field": "%s.keyword" % locationString,
                "size": str(topx)
              }
            }}}
          }
        })

        dayData = (datetime.date.today()+ relativedelta(days=-(int(dayoffset))))
        return [ res["aggregations"]["communityfilter"]["topLocations"]["buckets"], dayData.strftime('%Y-%m-%d') ]
    except ElasticsearchException as err:
        app.logger.error('ElasticSearch error: %s' % err)

    return False


# Formatting functions

def prettify(elem, level=0):
    """ Prettify the xml output """
    i = "\n" + level*"  "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = i
        for elem in elem:
            prettify(elem, level+1)
        if not elem.tail or not elem.tail.strip():
            elem.tail = i
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = i

def formatBadIPxml(iplist):
    """ Create XML Strucure for BadIP list """

    if iplist:
        ewssimpleinfo = ET.Element('EWSSimpleIPInfo')
        sources = ET.SubElement(ewssimpleinfo, 'Sources')

        for ip in iplist['buckets']:
            source = ET.SubElement(sources, 'Source')
            address = ET.SubElement(source, 'Address')
            address.text = ip['key']
            counter = ET.SubElement(source, 'Count')
            counter.text = str(ip['doc_count'])


        prettify(ewssimpleinfo)
        iplistxml = '<?xml version="1.0" encoding="UTF-8"?>'
        iplistxml += (ET.tostring(ewssimpleinfo, encoding="utf-8", method="xml")).decode('utf-8')

        return iplistxml
    else:
        return app.config['DEFAULTRESPONSE']

def formatAlertsXml(alertslist):
    """ Create XML Strucure for Alerts list """

    EWSSimpleAlertInfo = ET.Element('EWSSimpleAlertInfo')
    alertsElement = ET.SubElement(EWSSimpleAlertInfo, 'Alerts')

    if alertslist:
        for alert in alertslist:
            if datetime.datetime.strptime(alert['_source']['createTime'],"%Y-%m-%d %H:%M:%S") > datetime.datetime.utcnow():
                returnDate = alert['_source']['recievedTime']
                app.logger.debug('formatAlertsJson: createTime > now, returning recievedTime, honeypot timezone probably manually set to eastern timezone')
            else:
                returnDate = alert['_source']['recievedTime']

            alertElement = ET.SubElement(alertsElement, 'Alert')
            alertId = ET.SubElement(alertElement, 'Id')
            alertId.text = alert['_id']
            alertDate = ET.SubElement(alertElement, 'DateCreated')
            alertDate.text = returnDate
            peerElement = ET.SubElement(alertElement, 'Peer')
            peerId = ET.SubElement(peerElement, 'Id')
            peerId.text = alert['_source']['peerIdent']
            peerType = ET.SubElement(peerElement, 'Type')
            peerType.text = alert['_source']['peerType']
            peerCountry = ET.SubElement(peerElement, 'Country')
            peerCountry.text = alert['_source']['targetCountry']
            requestElement = ET.SubElement(alertElement, 'Request')
            requestElement.text = alert['_source']['originalRequestString']
            sourceElement = ET.SubElement(alertElement, 'Source')
            sourceAddress = ET.SubElement(sourceElement, 'Address')
            sourceAddress.text = alert['_source']['sourceEntryIp']
            sourceCountry = ET.SubElement(sourceElement, 'Country')
            sourceCountry.text = alert['_source']['country']
            sourceCoordinates = alert['_source']['location'].split(',')
            sourceLatitude = ET.SubElement(sourceElement, 'Latitude')
            sourceLatitude.text = sourceCoordinates[0].strip()
            sourceLongitude = ET.SubElement(sourceElement, 'Longitude')
            sourceLongitude.text = sourceCoordinates[1].strip()

    prettify(EWSSimpleAlertInfo)
    alertsxml = '<?xml version="1.0" encoding="UTF-8"?>'
    alertsxml += (ET.tostring(EWSSimpleAlertInfo, encoding="utf-8", method="xml")).decode('utf-8')

    return alertsxml

def formatAlertsJson(alertslist):
    """ Create JSON Structure for Alerts list """
    jsonarray = []

    if alertslist:

        for alert in alertslist:
            if datetime.datetime.strptime(alert['_source']['createTime'], "%Y-%m-%d %H:%M:%S") > datetime.datetime.utcnow():
                returnDate = alert['_source']['recievedTime']
                app.logger.debug('formatAlertsJson: createTime > now, returning recievedTime, honeypot timezone probably manually set to eastern timezone')
            else:
                returnDate = alert['_source']['recievedTime']

            latlong = alert['_source']['location'].split(' , ')
            destlatlong = alert['_source']['locationDestination'].split(' , ')

            # kippo attack details
            if alert['_source']['peerType'] == "SSH/console(cowrie)" and alert['_source']['originalRequestString'] == "":
                requestString =  ""
                if alert['_source']['username'] is not None:
                    requestString+= "Username: \"" + str(urllib.parse.unquote(alert['_source']['username'])) + "\""
                else:
                    requestString += "Username: <none>"
                if alert['_source']['password'] is not None:
                    requestString+= " | Password: \"" + str(urllib.parse.unquote(alert['_source']['password'])) + "\""
                else:
                    requestString += " | Password: <none>"
                if alert['_source']['login'] is not None:
                    requestString+= " | Status: "+ str(alert['_source']['login'])
                requestStringOut = html.escape(requestString)
            else:
                requestStringOut = html.escape(urllib.parse.unquote(alert['_source']['originalRequestString']))

            # map private IP ranges 0:0 Locations to DTAG HQ in Bonn :) # 50.708021, 7.129191
            if latlong == ["0.0","0.0"]:
                latlong = ["50.708021", "7.129191"]
                app.logger.debug('formatAlertsJson: mapping location 0.0/0.0 to DTAG HQ')

            if destlatlong == ["0.0","0.0"]:
                destlatlong = ["50.708021", "7.129191"]
                app.logger.debug('formatAlertsJson: mapping location 0.0/0.0 to DTAG HQ')

            jsondata = {
                'id': alert['_id'],
                'dateCreated': "%s" % returnDate,
                'country': alert['_source']['country'],
                'countryName': alert['_source']['countryName'],
                'targetCountry': alert['_source']['targetCountry'],
                'sourceLat': latlong[0],
                'sourceLng': latlong[1],
                'destLat' : destlatlong[0],
                'destLng' : destlatlong[1],
                'analyzerType': alert['_source']['peerType'],
                'requestString': '%s' % requestStringOut
            }

            jsonarray.append(jsondata)

    return ({'alerts': jsonarray})

def formatAlertsCount(numberofalerts, outformat):
    """ Create XML / Json Structure with number of Alerts in requested timespan """

    if outformat == "xml":
        ewssimpleinfo = ET.Element('EWSSimpleIPInfo')
        alertCount = ET.SubElement(ewssimpleinfo, 'AlertCount')
        if numberofalerts:
            alertCount.text = str(numberofalerts)
        else:
            alertCount.text = str(0)
        prettify(ewssimpleinfo)
        alertcountxml = '<?xml version="1.0" encoding="UTF-8"?>'
        alertcountxml += (ET.tostring(ewssimpleinfo, encoding="utf-8", method="xml")).decode('utf-8')
        return alertcountxml
    else:
        return ({'AlertCount': numberofalerts})

def formatAlertsCountWithType(numberofalerts):
    if numberofalerts:
        jsondata1 = {}
        for alertTypes in numberofalerts['aggregations']['communityfilter']['honeypotTypes']['buckets']:
            jsondata1[alertTypes['key']] = alertTypes['doc_count']

        jsondata2 = {
                "AlertCountTotal" : numberofalerts['aggregations']['communityfilter']['doc_count'],
                "AlertCountPerType" : jsondata1
        }
        return (jsondata2)
    else:
        return app.config['DEFAULTRESPONSE']

def formatDatasetAlertsPerMonth(datasetAlertsPerMonth):
    if datasetAlertsPerMonth:
        jsondata = {}
        for alertsPerMonth in datasetAlertsPerMonth['buckets']:
                jsondata[alertsPerMonth['key_as_string']] = alertsPerMonth['doc_count']

        return ([{'datasetAlertsPerMonth': jsondata}])
    else:
        return app.config['DEFAULTRESPONSE']

def formatDatasetAlertTypesPerMonth(datasetAlertTypePerMonth):
    if datasetAlertTypePerMonth:
        jsondatamonth = {}
        for alertTypesPerMonth in datasetAlertTypePerMonth['buckets']:
            jsondatatype = {}
            for alertTypes in alertTypesPerMonth['nested_terms_agg']['buckets']:
                jsondatatype[alertTypes['key']] =  alertTypes['doc_count']
                jsondatamonth[alertTypesPerMonth['key_as_string']] = jsondatatype

        return ([{'datasetAlertsPerMonth': jsondatamonth}])
    else:
        return app.config['DEFAULTRESPONSE']

def formatAlertStats(retrieveAlertStat):
    if retrieveAlertStat:
        jsondata = {
            'AlertsLast24Hours': retrieveAlertStat[0]['doc_count'],
            'AlertsLastHour': retrieveAlertStat[1]['doc_count'],
            'AlertsLastMinute': retrieveAlertStat[2]['doc_count']
        }
        return (jsondata)
    else:
        return app.config['DEFAULTRESPONSE']

def formatTopCountriesAttacks(retrieveTopCountryAttacksArr):
    if retrieveTopCountryAttacksArr:
        retrieveTopCountryAttacker = retrieveTopCountryAttacksArr[0]
        monthOffset = retrieveTopCountryAttacksArr[1]
        monthdate = retrieveTopCountryAttacksArr[2]
        retrieveTopCountryAttacked = retrieveTopCountryAttacksArr[3]

    else:
        return app.config['DEFAULTRESPONSE']

    # Create json structure for ATTACKER and ATTACKED countries
    jsonarray_attacker = []
    jsonarray_attacked = []

    if retrieveTopCountryAttacker and retrieveTopCountryAttacked:

        # attacker
        for topCountry in retrieveTopCountryAttacker:
            jsondata_attacker = {
                'code': topCountry['key'],
                'country': topCountry['country']['hits']['hits'][0]['_source']['countryName'],
                'count': topCountry['doc_count']
            }
            jsonarray_attacker.append(jsondata_attacker)

        # attacked
        for topCountry in retrieveTopCountryAttacked:
            jsondata_attacked = {
                'code': topCountry['key'],
                'country': topCountry['country']['hits']['hits'][0]['_source']['targetCountryName'],
                'count': topCountry['doc_count']
            }
            jsonarray_attacked.append(jsondata_attacked)

    countryStats = { 'id' :  monthOffset,
                 'date': monthdate,
                 'attacksPerCountry': jsonarray_attacker,
                 'attacksToTargetCountry': jsonarray_attacked
                     }
    return ([countryStats])

def formatLatLonAttacks(retrieveLatLonAttacksArr):
    if retrieveLatLonAttacksArr:
        retrieveLatLonAttacks = retrieveLatLonAttacksArr[0]
        daydata = retrieveLatLonAttacksArr[1]
    jsonarray_location = []


    if retrieveLatLonAttacks:
        for attackLocation in retrieveLatLonAttacks:
            latLonArr = attackLocation['key'].split(" , ")
            jsondata_location = {
                'lat': latLonArr[0],
                'lng' : latLonArr[1],
                'count': attackLocation['doc_count']
            }
            jsonarray_location.append(jsondata_location)


    LatLonStats = {
                 'statsSince': daydata,
                 'latLonAttacks': jsonarray_location
                     }


    return ([LatLonStats])


###############
### App Routes
###############

# Default webroot access
@app.route("/")
def webroot():
    return redirect(app.config['CORSDOMAIN'])

# Heartbeat
@app.route("/heartbeat", methods=['GET'])
def heartbeat():
    if testElasticsearch() and testMemcached():
        return "I'm alive"
    else:
        abort(401)


# Routes with XML output

@app.route("/alert/retrieveIPs", methods=['POST'])
@app.route("/ews-0.1/alert/retrieveIPs", methods=['POST'])
@authentication_required
def retrieveIPs():
    """ Retrieve IPs from ElasticSearch and return formatted XML with IPs """

    # get result from cache
    getCacheResult = getCache(request.url, "url")
    if getCacheResult is not False:
        return getCacheResult

    # query ES
    else:
        returnResult = formatBadIPxml(queryBadIPs(app.config['BADIPTIMESPAN'], checkCommunityIndex(request)))
        setCache(request.url, returnResult, 60, "url")
        return Response(returnResult, mimetype='text/xml')

@app.route("/alert/retrieveAlertsCyber", methods=['POST'])
@authentication_required
def retrieveAlertsCyber():
    """ Retrieve Alerts from ElasticSearch and return formatted 
        XML with limited alert content
    """

    # get result from cache
    getCacheResult = getCache(request.url, "url")
    if getCacheResult is not False:
        app.logger.debug('Returning /retrieveAlertsCyber from Cache for %s' % str(request.remote_addr))
        return Response(getCacheResult)

    # query ES
    else:
        returnResult = formatAlertsXml(queryAlerts(app.config['MAXALERTS'], checkCommunityIndex(request)))
        setCache(request.url, returnResult, 1, "url")
        app.logger.debug('Returning /retrieveAlertsCyber from ES for %s' % str(request.remote_addr))
        return Response(returnResult, mimetype='text/xml')


# Routes with both XML and JSON output

@app.route("/alert/retrieveAlertsCount", methods=['GET'])
def retrieveAlertsCount():
    """ Retrieve number of alerts in timeframe (GET-Parameter time as decimal or "day") """

    # Retrieve Number of Alerts from ElasticSearch and return as xml / json
    if not request.args.get('time'):
        app.logger.error('No time GET-parameter supplied in retrieveAlertsCount. Must be decimal number (in minutes) or string "day"')
        return app.config['DEFAULTRESPONSE']
    else:
        if request.args.get('out') and request.args.get('out') == 'json':
            # get result from cache
            getCacheResult = getCache(request.url, "url")
            if getCacheResult is not False:
                return jsonify(getCacheResult)
            else:
                returnResult = formatAlertsCount(queryAlertsCount(request.args.get('time'), checkCommunityIndex(request)), 'json')
                setCache(request.url, returnResult, 60, "url")
                return jsonify(returnResult)

        else:
            # get result from cache
            getCacheResult = getCache(request.url, "url")
            if getCacheResult is not False:
                return Response(getCacheResult, mimetype='text/xml')
            else:
                returnResult = formatAlertsCount(queryAlertsCount(request.args.get('time'), checkCommunityIndex(request)), 'xml')
                setCache(request.url, returnResult, 60, "url")
                return Response(returnResult, mimetype='text/xml')


# Routes with JSON output

@app.route("/alert/retrieveAlertsCountWithType", methods=['GET'])
def retrieveAlertsCountWithType():
    """ Retrieve number of alerts in timeframe (GET-Parameter time as decimal or "day") and divide into honypot types"""

    # get result from cache
    getCacheResult = getCache(request.url, "url")
    if getCacheResult is not False:
        return jsonify(getCacheResult)

    # query ES
    else:
        # Retrieve Number of Alerts from ElasticSearch and return as xml / json
        if not request.args.get('time'):
            app.logger.error('No time GET-parameter supplied in retrieveAlertsCountWithType. Must be decimal number (in minutes) or string "day"')
            return app.config['DEFAULTRESPONSE']
        else:
            returnResult = formatAlertsCountWithType(queryAlertsCountWithType(request.args.get('time'), checkCommunityIndex(request)))
            setCache(request.url, returnResult, 13, "url")
            app.logger.debug('UNCACHED %s' % str(request.url))
            return jsonify(returnResult)

@app.route("/alert/retrieveAlertsJson", methods=['GET'])
def retrieveAlertsJson():
    """ Retrieve last 5 Alerts in JSON without IPs """

    # set cacheItem independent from url parameters, respect community index
    cacheEntry = request.url

    # get result from cache
    getCacheResult = getCache(cacheEntry, "url")
    if getCacheResult is not False:
        app.logger.debug('Returning /retrieveAlertsJson from Cache %s' % str(request.remote_addr))
        return jsonify(getCacheResult)

    # query ES
    else:
        numAlerts = 35
        # Retrieve last X Alerts from ElasticSearch and return JSON formatted with limited alert content
        returnResult =  formatAlertsJson(queryAlertsWithoutIP(numAlerts, checkCommunityIndex(request)))
        setCache(cacheEntry, returnResult, 25, "url")
        app.logger.debug('UNCACHED %s' % str(request.url))
        return jsonify(returnResult)

@app.route("/alert/datasetAlertsPerMonth", methods=['GET'])
def retrieveDatasetAlertsPerMonth():
    """ Retrieve the attacks / day in the last x days from elasticsearch
        and return as JSON for the last months, defaults to last month,
        if no GET parameter days is given
    """

    # get result from cache
    getCacheResult = getCache(request.url, "url")
    if getCacheResult is not False:
        return jsonify(getCacheResult)

    # query ES
    else:
        if not request.args.get('days'):
            # Using default : within the last month
            returnResult = formatDatasetAlertsPerMonth(queryDatasetAlertsPerMonth(None, checkCommunityIndex(request)))
        else:
            returnResult = formatDatasetAlertsPerMonth(queryDatasetAlertsPerMonth(request.args.get('days'), checkCommunityIndex(request)))
        setCache(request.url, returnResult, 600, "url")
        return jsonify(returnResult)

@app.route("/alert/datasetAlertTypesPerMonth", methods=['GET'])
def retrieveDatasetAlertTypesPerMonth():
    """ Retrieve the attacks / day in the last x days from elasticsearch,
        split by attack group
        and return as JSON for the last x months, defaults to last month,
        if no GET parameter days is given
    """

    # get result from cache
    getCacheResult = getCache(request.url, "url")
    if getCacheResult is not False:
        return jsonify(getCacheResult)

    # query ES
    else:
        if not request.args.get('days'):
            # Using default : within the last month
            returnResult = formatDatasetAlertTypesPerMonth(queryDatasetAlertTypesPerMonth(None, checkCommunityIndex(request)))
        else:
            returnResult = formatDatasetAlertTypesPerMonth(queryDatasetAlertTypesPerMonth(request.args.get('days'), checkCommunityIndex(request)))
        setCache(request.url, returnResult, 3600, "url")
        return jsonify(returnResult)

@app.route("/alert/retrieveAlertStats", methods=['GET'])
def retrieveAlertStats():
    """ Retrieve combined statistics
        AlertsLastMinute, AlertsLastHour,  AlertsLast24Hours
    """

    # get result from cache
    getCacheResult = getCache(request.url, "url")
    if getCacheResult is not False:
        return jsonify(getCacheResult)

    # query ES
    else:
        returnResult = formatAlertStats(queryAlertStats(checkCommunityIndex(request)))
        setCache(request.url, returnResult, 13, "url")
        app.logger.debug('UNCACHED %s' % str(request.url))
        return jsonify(returnResult)

@app.route("/alert/topCountriesAttacks", methods=['GET'])
def retrieveTopCountriesAttacks():
    """ Retrieve the Top X countries and their attacks within month
    """

    # get result from cache
    getCacheResult = getCache(request.url, "url")
    if getCacheResult is not False:
        return jsonify(getCacheResult)

    # query ES
    else:
        if not request.args.get('monthOffset'):
            # Using default : within the last month
            offset = None
        else:
            offset = request.args.get('monthOffset')

        if not request.args.get('topx'):
            # Using default top 10
            topx = None
        else:
            topx = request.args.get('topx')
        returnResult = formatTopCountriesAttacks(queryTopCountriesAttacks(offset, topx, checkCommunityIndex(request)))
        setCache(request.url, returnResult, 60, "url")
        app.logger.debug('UNCACHED %s' % str(request.url))
        return jsonify(returnResult)

@app.route("/alert/retrieveLatLonAttacks", methods=['GET'])
def retrieveLatLonAttacks():
    """ Retrieve statistics on Latitude and Longitude of the attack sources / destinations.
        offset in days
        topX determines how many
        direction src = src lat lng
        direction dst = dest lat lng
    """

    # get result from cache
    getCacheResult = getCache(request.url, "url")
    if getCacheResult is not False:
        return jsonify(getCacheResult)

    # query ES
    else:
        if not request.args.get('direction'):
            # Using default : lat and long of attack sources
            direction = None
        else:
            # using attack destinations
            direction = request.args.get('direction')

        if not request.args.get('topx'):
            # Using default top 10
            topx = None
        else:
            topx = request.args.get('topx')

        if not request.args.get('offset'):
            # Using default 24h
            offset = None
        else:
            offset = request.args.get('offset')

        returnResult=formatLatLonAttacks(queryLatLonAttacks(direction, topx, offset, checkCommunityIndex(request)))
        setCache(request.url, returnResult, 60, "url")
        return jsonify(returnResult)

# PUT Service

@app.route("/ews-0.1/alert/postSimpleMessage", methods=['GET'])
def getSimpleMessage():
    return Response("POST is required for this action.", mimetype='text/html', status=500)

@app.route("/ews-0.1/alert/postSimpleMessage", methods=['POST'])
def postSimpleMessage():
    if request.data:
        tree = putservice.checkPostData(request.data)
        if tree:
            putservice.handleAlerts(tree, checkCommunityUser(), es, cache)
            message = "<Result><StatusCode>OK</StatusCode><Text></Text></Result>"
            return Response(message, mimetype='text/xml')
    return app.config['DEFAULTRESPONSE']


###############
### Main
###############

if __name__ == '__main__':
    app.run(host=app.config['BINDHOST'].split(':')[0], port=int(app.config['BINDHOST'].split(':')[1]))