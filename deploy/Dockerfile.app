# The app: Python, the pinned dependencies and server/ (ARCHITECTURE.md §18.1).
FROM python:3.12-slim

# The app runs from the source tree: it finds layouts/, filters/ and web/ next to server/.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/docwizard/server \
    DOCWIZARD_CONFIG=/etc/docwizard/docwizard.toml

RUN groupadd --system --gid 10001 docwizard-app \
 && useradd --system --uid 10001 --gid 10001 --home-dir /var/lib/docwizard --shell /usr/sbin/nologin docwizard-app

WORKDIR /opt/docwizard
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY LICENSE ./
COPY server ./server
COPY worker ./worker
COPY layouts ./layouts
COPY filters ./filters
COPY web ./web
RUN install -d -o 10001 -g 10001 /var/lib/docwizard /var/lib/docwizard-publish /var/lib/docwizard-jobs \
      /var/lib/docwizard-jobs/run \
      /var/lib/docwizard-cache /run/docwizard \
 && install -d -o 0 -g 0 -m 0755 /var/lib/docwizard-jobs/sock

USER 10001:10001
CMD ["python", "-m", "docwizard", "serve"]
