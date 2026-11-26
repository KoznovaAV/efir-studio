FROM mwader/static-ffmpeg:8.1.1@sha256:735f84b905e00d5c618b667f0b053f83b1096f5fc404c607e6134bf2275a0e0a AS codecs
FROM python:3.13.16-slim-trixie@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f

RUN groupadd --gid 1000 efir \
    && useradd --uid 1000 --gid 1000 --no-create-home efir \
    && install -d -o 1000 -g 1000 /workspace
COPY --from=codecs /ffmpeg /ffprobe /usr/local/bin/

ENV PYTHONPATH=/opt/efir \
    PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    EFIR_FFMPEG=/usr/local/bin/ffmpeg
WORKDIR /workspace
COPY efir/ /opt/efir/efir/
COPY LICENSE /opt/efir/LICENSE
LABEL org.opencontainers.image.source="https://github.com/KoznovaAV/efir-studio" \
      org.opencontainers.image.description="EFIR Studio CPU CLI"
USER 1000:1000
ENTRYPOINT ["python", "-m", "efir"]
CMD ["--help"]
