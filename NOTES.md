# Project Notes

## Home Assistant App Builder — Gotchas

### `config.yaml` must include an `image` field

The `build-app.yaml` workflow uses `home-assistant/actions/helpers/info` to read app metadata. If the `image` field is missing from `config.yaml`, the action outputs `null`, which propagates through the normalize step as the literal string `"null"`, producing broken image references like `null/aarch64-null:latest`.

Every app's `config.yaml` must include:

```yaml
image: "ghcr.io/<owner>/<image-name>"
```

### `image` field format: no `{arch}` placeholder

The `image` field is just a base registry path — **do not include `{arch}`**. The builder prepends the arch automatically (e.g. `ghcr.io/janejeon/grafana-image-renderer` → `ghcr.io/janejeon/aarch64-grafana-image-renderer`).

See the upstream template for reference: https://github.com/home-assistant/apps-example/blob/main/example/config.yaml

### Per-arch image names, not per-arch tags

The HA builder (`home-assistant/builder` actions) uses **arch-prefixed image names**, not arch-suffixed tags. Each arch gets its own image (`{registry}/{arch}-{name}:{tag}`), and a multi-arch manifest is published on top. There is no supported way to use a `{name}:{version}-{arch}` tag scheme with these actions.

### `schema` with `?` + `options` defaults: both work together

The docs imply that using `?` (optional) in `schema` and providing a default in `options` are mutually exclusive, but in practice they work fine together. The `?` prevents the UI from requiring the user to fill in the field; the `options` value serves as the default written to `options.json`. Use this pattern freely.

### `CONFIG_PATH` is not needed when using bashio

`CONFIG_PATH=/data/options.json` is the raw `jq` approach — a completely separate pattern from bashio. `bashio::config` does not read from that file; it hits the Supervisor HTTP API at `http://supervisor/addons/self/options/config` using the injected `SUPERVISOR_TOKEN`. You never need to set `CONFIG_PATH` when using bashio.

### Config reading is runtime-only — not available during Docker build

`SUPERVISOR_TOKEN` and `/data/options.json` only exist when the Supervisor starts the container. They are not available during `docker build`. All config reading must happen in the startup script, not the Dockerfile.

### `BUILD_FROM` is injected by the Supervisor — never define it yourself

The `ARG BUILD_FROM` / `FROM $BUILD_FROM` pattern is the HA convention. The Supervisor passes the appropriate arch-specific base image as a `--build-arg` at build time. You never set this variable — just declare the `ARG` and use it in `FROM`.

To use a custom base image (e.g. an upstream service image), override it in `build.yaml`:

```yaml
build_from:
  amd64: upstream/image:v1.2.3
  aarch64: upstream/image:v1.2.3
```

### Bashio installation in non-HA base images

Bashio is just shell scripts — no OS-specific compilation. Install it manually in the Dockerfile:

```dockerfile
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl jq \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /usr/lib/bashio \
    && curl -L -f -s "https://github.com/hassio-addons/bashio/archive/v${BASHIO_VERSION}.tar.gz" \
        | tar -xzf - --strip 1 -C /usr/lib/bashio \
    && ln -s /usr/lib/bashio/bashio /usr/bin/bashio
```

Note: `curl` is not present in all base images (e.g. `grafana/grafana-image-renderer`) — install it explicitly alongside `jq`.

### No S6-overlay in custom base images — use `#!/usr/bin/env bashio`

The `with-contenv` shebang (`#!/usr/bin/with-contenv bashio`) is an S6-overlay mechanism and will not work in non-HA base images. Use `#!/usr/bin/env bashio` instead. Bashio's own shebang handler sets `errexit`, `errtrace`, `nounset`, and `pipefail` automatically — no need to set them manually.

Also set `init: false` in `config.yaml` when not using S6.

### Overriding ENTRYPOINT in upstream images

When an upstream image bakes the binary into `ENTRYPOINT` (e.g. `ENTRYPOINT ["tini", "--", "/usr/bin/grafana-image-renderer"]`), overriding just `CMD` will pass your value as an argument to the binary, not replace the entrypoint. Override both:

```dockerfile
ENTRYPOINT ["tini", "--", "/run.sh"]
CMD ["server"]  # forwarded via exec "$@" in run.sh
```

In `run.sh`, use `exec /usr/bin/the-binary "$@"` to forward `CMD` args and replace the shell process so tini reaps the correct PID.

### Restore USER after root operations in Dockerfile

If an upstream image runs as a non-root user and you switch to `root` for installation steps, switch back before `ENTRYPOINT`. Some runtimes (notably Chromium-based ones) explicitly refuse to run as root.

```dockerfile
USER root
RUN # ... install things ...
USER 65532  # restore to upstream's non-root user
ENTRYPOINT [...]
```

### `ports` is only needed for host exposure, not add-on to add-on comms

All add-on containers share an internal Docker network managed by the Supervisor. They can reach each other by slug hostname (e.g. `http://grafana-image-renderer:8081`) without any `ports` declaration. Only declare `ports` if you need the service reachable from the host machine.

If the port is user-configurable via `options`, do not declare it in `ports` — the `ports` mapping is static and cannot reference option values.
