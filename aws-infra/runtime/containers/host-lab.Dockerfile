FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
ARG DEBIAN_FRONTEND=noninteractive
COPY containers/host-lab.apt.lock /opt/host-lab.apt.lock
RUN apt-get update -qq && xargs apt-get install -y --no-install-recommends < /opt/host-lab.apt.lock \
    && rm -rf /var/lib/apt/lists/*
# Test controller only; production hosts inherit bootstrap tools from their accepted AMI.
RUN python3 -m venv /opt/test-venv
COPY requirements.lock.txt /opt/requirements.lock.txt
RUN /opt/test-venv/bin/pip install --no-cache-dir -r /opt/requirements.lock.txt
ENV PATH=/opt/test-venv/bin:$PATH ANSIBLE_HOME=/tmp/ansible
CMD ["sleep", "infinity"]
