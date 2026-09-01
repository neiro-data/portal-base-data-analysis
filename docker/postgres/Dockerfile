# syntax=docker/dockerfile:1

ARG PG_MAJOR=17
ARG PG_PARQUET_VERSION=0.5.1
ARG PGRX_VERSION=0.16.0

# ---- stage 1: build pg_parquet from source via cargo-pgrx ----
FROM postgres:${PG_MAJOR}-bookworm AS build
ARG PG_MAJOR
ARG PG_PARQUET_VERSION
ARG PGRX_VERSION

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    pkg-config \
    libssl-dev \
    libclang-dev \
    clang \
    postgresql-server-dev-${PG_MAJOR} \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.cargo/bin:${PATH}"
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal

RUN cargo install --locked cargo-pgrx --version ${PGRX_VERSION}
RUN cargo pgrx init --pg${PG_MAJOR} $(which pg_config)

RUN git clone --depth 1 --branch v${PG_PARQUET_VERSION} \
    https://github.com/CrunchyData/pg_parquet /build/pg_parquet
WORKDIR /build/pg_parquet

RUN cargo pgrx package --pg-config $(which pg_config)

# ---- stage 2: runtime ----
FROM postgres:${PG_MAJOR}-bookworm AS runtime
ARG PG_MAJOR

# Copy the compiled extension artifacts produced by `cargo pgrx package`.
COPY --from=build /build/pg_parquet/target/release/pg_parquet-pg${PG_MAJOR}/usr/lib/postgresql/${PG_MAJOR}/lib/*.so \
    /usr/lib/postgresql/${PG_MAJOR}/lib/
COPY --from=build /build/pg_parquet/target/release/pg_parquet-pg${PG_MAJOR}/usr/share/postgresql/${PG_MAJOR}/extension/*.control \
    /usr/share/postgresql/${PG_MAJOR}/extension/
COPY --from=build /build/pg_parquet/target/release/pg_parquet-pg${PG_MAJOR}/usr/share/postgresql/${PG_MAJOR}/extension/*.sql \
    /usr/share/postgresql/${PG_MAJOR}/extension/

# pg_parquet must be loaded at server start.
RUN echo "shared_preload_libraries = 'pg_parquet'" >> /usr/share/postgresql/postgresql.conf.sample

RUN mkdir -p /parquet && chown postgres:postgres /parquet

VOLUME ["/var/lib/postgresql/data"]
EXPOSE 5432
