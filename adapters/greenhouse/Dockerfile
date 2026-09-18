# adapters/greenhouse/Dockerfile
# Builds the FIRMARBITER Greenhouse adapter image.
# Platform: linux/amd64 (required -- Greenhouse's own build, including
# angr's Rust extension, genuinely needs native amd64; confirmed live
# that it crashes under QEMU amd64-on-arm64 emulation and works cleanly
# on real amd64 hardware).
FROM --platform=linux/amd64 ubuntu:20.04@sha256:8feb4d8ca5354def3d8fce243717141ce31e2c428701f6682bd2fafe15388214

ARG GREENHOUSE_COMMIT=829cbabbb5de64e251dc7f9c2692b2ca39e29b3b

LABEL org.opencontainers.image.title="FIRMARBITER Greenhouse Adapter"
LABEL org.opencontainers.image.source="https://github.com/sefcom/greenhouse"
LABEL org.opencontainers.image.revision="${GREENHOUSE_COMMIT}"
LABEL firmarbiter.adapter.id="greenhouse"
LABEL firmarbiter.adapter.contract-version="1.0"

ENV DEBIAN_FRONTEND=noninteractive

# Mirror flakiness mitigation -- confirmed needed live: repeated
# intermittent 400s mid-download from Ubuntu's CDN-backed mirror
# until pipelining was disabled.
RUN echo 'Acquire::http::Pipeline-Depth "0";' > /etc/apt/apt.conf.d/99no-pipelining && \
    echo 'Acquire::Retries "5";' >> /etc/apt/apt.conf.d/99no-pipelining

RUN apt-get update && apt-get install -y apt-utils

RUN apt-get -y install apt-utils git curl radare2 python2 python3 python3-venv python3-pip \
    wget sudo docker telnet net-tools iputils-ping iptables iproute2 libguestfs-tools \
    p7zip-full p7zip-rar zip libpq-dev vim netcat socat qemu-system

RUN apt update && apt install -y dnsutils jq

# Clone Greenhouse's own source at the pinned commit. Everything this
# Dockerfile needs beyond the base OS -- FirmAEreplacements/,
# chrome104.deb, squashfs-tools.tar.gz, testimage.tar.gz,
# routersploit_gh/ -- is already committed inside this same clone;
# confirmed via the real GitHub Contents API listing, nothing needed
# sourcing externally. ubuntu.tar (the repo's one genuinely external,
# .gitignore'd asset) is deliberately NOT generated here -- confirmed
# via a full repo-wide grep that it is referenced only by
# docker_init.sh's own inner dockerd bootstrap (bypassed entirely in
# favour of the docker-socket requirement) and by routersploit_gh/
# (a separate, optional vulnerability-replay module this adapter does
# not use), never by the core rehosting path this adapter runs.
RUN git clone -q https://github.com/sefcom/greenhouse.git /tmp/greenhouse-src && \
    cd /tmp/greenhouse-src && \
    git checkout "${GREENHOUSE_COMMIT}"

RUN mkdir -p /work/firmwares

RUN cd /work && git clone -q --recursive https://github.com/pr0v3rbs/FirmAE

RUN cp /tmp/greenhouse-src/FirmAEreplacements/install.sh /work/FirmAE/install.sh
RUN cp /tmp/greenhouse-src/FirmAEreplacements/v2.3.3.tar.gz /work/FirmAE/v2.3.3.tar.gz

RUN cd /work/FirmAE && ./download.sh
RUN cd /work/FirmAE && ./install.sh

RUN ln -s /bin/ntfs-3g /bin/mount.ntfs-3g

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
RUN /root/.local/bin/uv python install 3.12
RUN /root/.local/bin/uv venv /root/venv --python 3.12 --seed
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

RUN git clone -q https://github.com/angr/angr-dev
# REPOS_CPYTHON="" confirmed needed live: angr-dev's own setup.sh
# auto-appends the optional angr-management GUI tool for cpython
# regardless of the -D list, pulling in an unrelated Qt6-devel
# dependency; a full repo-wide grep confirmed Greenhouse's own code
# never imports angr_management, so this is a real, safe skip.
RUN cd /angr-dev && . /root/venv/bin/activate && . "$HOME/.cargo/env" && \
    REPOS_CPYTHON="" ./setup.sh -i -D "archinfo pyvex cle claripy ailment angr angr-doc binaries"

RUN git clone -q https://github.com/davidribyrne/cramfs
RUN cd /cramfs && make && make install

RUN mkdir /work/FirmAE/targetfs
RUN mkdir -p /host

RUN rm -rf /work/FirmAE/analyses/routersploit
RUN cd /work/FirmAE/analyses && git clone -q -n https://github.com/threat9/routersploit
RUN cd /work/FirmAE/analyses/routersploit && git checkout 4eefc7e
# routersploit_patch (from FirmAE's own upstream repo) is confirmed
# corrupted -- independently rejected by both `git apply` and `patch`
# at the identical byte offset, a genuine hunk-header/body mismatch in
# the file as committed upstream. The first hunk (interpreter.py's
# run_command(), the load-bearing programmatic-invocation capability)
# applies cleanly; only later, optional exploit-module hunks are lost.
# Tolerated with || true rather than failing the whole build.
RUN cd /work/FirmAE/analyses/routersploit && \
    patch -p1 < /work/FirmAE/analyses/routersploit_patch || true
RUN cd /work/FirmAE/analyses/routersploit && pip install -r requirements.txt

RUN cp /tmp/greenhouse-src/FirmAEreplacements/makeNetwork.py /work/FirmAE/scripts/makeNetwork.py
RUN cp /tmp/greenhouse-src/FirmAEreplacements/delete.sh /work/FirmAE/scripts/delete.sh
RUN cp /tmp/greenhouse-src/FirmAEreplacements/inferFile.sh /work/FirmAE/scripts/inferFile.sh
RUN cp /tmp/greenhouse-src/FirmAEreplacements/test_emulation.sh /work/FirmAE/scripts/test_emulation.sh
RUN cp /tmp/greenhouse-src/FirmAEreplacements/umount.sh /work/FirmAE/scripts/umount.sh
RUN cp /tmp/greenhouse-src/FirmAEreplacements/extractor.py /work/FirmAE/sources/extractor/extractor.py
RUN cp /tmp/greenhouse-src/FirmAEreplacements/firmae.config /work/FirmAE/firmae.config
RUN cp /tmp/greenhouse-src/FirmAEreplacements/run.sh /work/FirmAE/run.sh
RUN cp /tmp/greenhouse-src/FirmAEreplacements/initializer.py /work/FirmAE/analyses/initializer.py

RUN cp -r /tmp/greenhouse-src/Greenhouse /gh

# QemuRunner.setup_bridges() crashes on every single run, for every
# firmware, regardless of whether that firmware would actually boot --
# confirmed live: it loops over every Docker network's IPAM.Config and
# iterates it directly, but Docker's own built-in "none" and "host"
# networks (present on any Docker daemon, always) have Config=None,
# not an empty list, so `for conf in configs:` throws a bare
# TypeError. Not an environment-specific issue on our side -- this
# would happen identically against Greenhouse's own original testing
# setup. A small, targeted fix (skip networks with no config) removes
# this unrelated crash so the tool's real rehosting behaviour on a
# given firmware becomes visible at all, matching the same principle
# as FirmAE's own checksummed compatibility patch.
RUN sed -i "/for conf in configs:/i\            if not configs: continue" /gh/backend/QemuRunner.py

RUN apt-get install -y python-is-python3

RUN cp /tmp/greenhouse-src/chrome104.deb /chrome104.deb
RUN apt-get install -y --allow-downgrades /chrome104.deb
RUN rm /chrome104.deb

RUN CHROMEVERSION=`/usr/bin/google-chrome --version | tr "." " " | awk '{print $3}'`; \
    DRIVERVERSION=`curl https://chromedriver.storage.googleapis.com/LATEST_RELEASE_$CHROMEVERSION`; \
    wget https://chromedriver.storage.googleapis.com/$DRIVERVERSION/chromedriver_linux64.zip
RUN unzip /chromedriver_linux64.zip
RUN cp chromedriver /work/FirmAE/chromedriver
RUN cp chromedriver /work/FirmAE/analyses/chromedriver
RUN cp chromedriver /gh
RUN cp chromedriver /gh/analysis
RUN rm /chromedriver_linux64.zip

RUN git clone -q https://github.com/sviehb/jefferson

RUN cp /tmp/greenhouse-src/squashfs-tools.tar.gz /squashfs-tools.tar.gz
RUN tar -xzf squashfs-tools.tar.gz
RUN chown -R root /squashfs-tools
RUN cd /squashfs-tools/squashfs-tools && make && make install
RUN rm /squashfs-tools.tar.gz

RUN . /root/venv/bin/activate && pip uninstall -y selenium && pip install selenium=="3.141.0"

# lxml==4.7.1's pinned C extension touches CPython internals removed
# in 3.12's restructured exception handling; relaxed to current,
# confirmed live.
RUN sed -i 's/lxml==4.7.1/lxml/' /gh/requirements.txt
# requests==2.24.0's own dependency metadata constrains urllib3
# to a pre-1.26 range; that old urllib3's bundled six.moves
# dynamic-import shim genuinely fails under Python 3.12 (confirmed
# live -- ModuleNotFoundError: urllib3.packages.six.moves), the
# same 'old pin vs 3.12 internals' failure class as lxml above.
# Relax the pin so pip resolves a modern, mutually-compatible pair.
RUN sed -i 's/requests==2.24.0/requests/' /gh/requirements.txt
# docker==5.0.0 (2021-era) registers its custom http+docker/
# http+unix transport adapter using older requests/urllib3
# internals -- confirmed live: URLSchemeUnknown: Not supported
# URL scheme http+docker, a well-known, widely-documented
# docker-py compatibility issue against modern requests, same
# 'old pin vs modern dependency' failure class as lxml/requests
# above. Relax the pin the same way.
RUN sed -i 's/docker==5.0.0/docker/' /gh/requirements.txt
RUN . /root/venv/bin/activate && pip install wheel && \
    pip install --retries 20 --timeout 60 -r /gh/requirements.txt

RUN . /root/venv/bin/activate && cd /jefferson && pip install -r requirements.txt && python3 setup.py install

RUN . /root/venv/bin/activate && pip install paramiko && pip uninstall pyelftools -y && pip install pyelftools==0.29

RUN chmod 777 /usr/bin/sudo

RUN cp /tmp/greenhouse-src/testimage.tar.gz /testimage.tar.gz
RUN tar -xzf /testimage.tar.gz
RUN rm testimage.tar.gz

RUN mkdir /routersploit
RUN cp -r /tmp/greenhouse-src/routersploit_gh/routersploit_ghpatched /routersploit/routersploit_gh
RUN pip3 install -r /routersploit/routersploit_gh/requirements.txt
RUN pip3 install lxml pycrypto

RUN curl -L "https://github.com/docker/compose/releases/download/1.29.2/docker-compose-$(uname -s)-$(uname -m)" \
    -o /usr/local/bin/docker-compose
RUN chmod +x /usr/local/bin/docker-compose

RUN cp /tmp/greenhouse-src/routersploit_gh/GH_PATH_TRAVERSAL /routersploit/GH_PATH_TRAVERSAL
RUN cp /tmp/greenhouse-src/routersploit_gh/exploits.list /routersploit/exploits.list
RUN cp -r /tmp/greenhouse-src/routersploit_gh/routersploit-log-parser /routersploit/routersploit_gh/routersploit-log-parser
RUN cp -r /tmp/greenhouse-src/crashing_inputs /crashing_inputs
RUN cp /tmp/greenhouse-src/routersploit_gh/run_routersploit.sh /routersploit
RUN cp /tmp/greenhouse-src/test.sh /gh/test.sh
RUN cp /tmp/greenhouse-src/run.sh /gh/run.sh

ENV TERM=xterm

RUN rm -rf /tmp/greenhouse-src

WORKDIR /firmarbiter_adapter
COPY lifecycle/ ./lifecycle/
COPY entrypoint.py ./entrypoint.py
COPY schemas/adapter-event-v1.schema.json ./schemas/adapter-event-v1.schema.json
RUN chmod +x ./entrypoint.py

ENTRYPOINT ["python3", "./entrypoint.py"]
