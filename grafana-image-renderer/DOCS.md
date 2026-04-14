# Home Assistant App: Grafana Image Renderer

Runs the [Grafana Image Renderer](https://github.com/grafana/grafana-image-renderer) as a
standalone service, enabling your Grafana instance to render panels and dashboards as PNG/PDF.

## Configuration

| Option               | Required | Default                                        | Description                                    |
| -------------------- | -------- | ---------------------------------------------- | ---------------------------------------------- |
| `auth_token`         | No       | `"-"`                                          | Shared secret between Grafana and the renderer |
| `port`               | No       | `8081`                                         | Port the renderer listens on                   |
| `browser_max_height` | No       | `1080`                                         | Maximum viewport height (px)                   |
| `browser_max_width`  | No       | `1920`                                         | Maximum viewport width (px)                    |
| `browser_flags`      | No       | `--disable-dev-shm-usage,--disable-extensions` | Chromium flags                                 |

## Grafana setup

Configure your Grafana instance to use this renderer by setting the following environment
variables (or their equivalents in `grafana.ini`):

```
GF_RENDERING_SERVER_URL=http://<addon-hostname>:8081/render
GF_RENDERING_CALLBACK_URL=http://<grafana-hostname>:3000/
GF_RENDERING_AUTH_TOKEN=<same token as auth_token above>
```

Replace `<addon-hostname>` with the hostname of this addon on your HA network. If both Grafana
and this renderer are running as HA addons, the hostname is typically the addon slug
(e.g. `grafana-image-renderer` or with underscores depending on your setup).

## Health check

The renderer exposes a health check at `/healthz` on the configured port.
