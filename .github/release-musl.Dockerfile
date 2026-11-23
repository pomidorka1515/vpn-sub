# Native amd64/aarch64 builds; dynamically linked musl 1.2.5 (Alpine 3.23).
# The official normal-GIL interpreter has shared libpython, not its static .a.
FROM python:3.14.10-alpine3.23

RUN apk add --no-cache \
    bash binutils build-base ccache coreutils curl findutils freetype-dev \
    libffi-dev linux-headers openssl openssl-dev perl pkgconf \
    libraqm-dev redis tar zlib-dev zstd-dev

# Nuitka rejects Alpine's patchelf 0.18.0 as buggy. Build a pinned supported
# version against the native musl instead of using a prebuilt glibc executable.
RUN curl -fL --retry 3 \
      https://github.com/NixOS/patchelf/releases/download/0.17.2/patchelf-0.17.2.tar.gz \
      -o /opt/patchelf.tar.gz \
    && echo '20427b718dd130e4b66d95072c2a2bd5e17232e20dad58c1bea9da81fae330e0  /opt/patchelf.tar.gz' \
      | sha256sum --check --strict \
    && mkdir /opt/patchelf-build \
    && tar -xzf /opt/patchelf.tar.gz -C /opt/patchelf-build --strip-components=1 \
    && cd /opt/patchelf-build \
    && ./configure --prefix=/usr/local \
    && make -j"$(nproc)" \
    && make install-strip \
    && patchelf --version \
    && rm -r /opt/patchelf-build /opt/patchelf.tar.gz

# A fresh copy of this image also runs the isolated lifecycle smoke harness.
# No checkout, Node, or build-time private libraries are mounted into it.
COPY requirements.txt /opt/release-requirements.txt
RUN python -m pip install --no-cache-dir -r /opt/release-requirements.txt
