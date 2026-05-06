FROM ubuntu:latest
LABEL authors="hpz4"

ENTRYPOINT ["top", "-b"]