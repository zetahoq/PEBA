#!/usr/bin/env bash


rm Geo*.gz
wget https://geolite.maxmind.com/download/geoip/database/GeoLite2-City.tar.gz
wget https://geolite.maxmind.com/download/geoip/database/GeoLite2-Country.tar.gz
wget https://geolite.maxmind.com/download/geoip/database/GeoLite2-ASN.tar.gz
gunzip GeoLite2-City.tar.gz
gunzip GeoLite2-Country.tar.gz
gunzip GeoLite2-ASN.tar.gz
