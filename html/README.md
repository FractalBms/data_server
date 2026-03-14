# HTML Control Page

Browser-based UI for controlling the battery data generator and viewing live sample data.

## What It Does

- Start / Stop data generation
- Edit and apply configuration (sites, racks, modules, cells, interval, topic mode) without restarting the generator
- Live stats: messages/sec, total published, loop time
- Live sample data table showing the latest cell measurements

## Prerequisites

The generator must be running first:

```bash
cd source/generator
pip install paho-mqtt websockets pyyaml
python generator.py
```

The generator starts a WebSocket server on `ws://localhost:8765` by default (configurable in `config.yaml`).

## Serving the HTML Page

Browsers block `file://` WebSocket connections in some cases, so serve the page over HTTP.

### Option 1 — Python (no install needed)

```bash
cd html
python -m http.server 8080
```

Then open: http://localhost:8080

### Option 2 — Node.js `npx serve`

```bash
cd html
npx serve -p 8080
```

Then open: http://localhost:8080

### Option 3 — VS Code Live Server extension

Right-click `index.html` in VS Code → **Open with Live Server**

## Connecting to a Remote Generator

If the generator is running on a different host, edit the `WS_URL` constant near the top of the `<script>` block in `index.html`:

```js
const WS_URL = 'ws://your-host:8765';
```

## WebSocket Port

Default port is `8765`. Change it in `source/generator/config.yaml`:

```yaml
websocket:
  host: 0.0.0.0
  port: 8765
```
