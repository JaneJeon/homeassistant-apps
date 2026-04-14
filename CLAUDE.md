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
