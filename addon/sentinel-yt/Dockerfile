ARG BUILD_FROM=ghcr.io/joeblack2k/sentinel-yt:latest
FROM ${BUILD_FROM}

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]

