# ESP32 serial bridge

A cheap ESP32 on the panel's COM port, running ESPHome, turns the panel's serial port into a TCP socket on your network. The Orisec integration's **Serial bridge** connection talks to it. Use it when USB to the HA host isn't practical and you'd rather not depend on a CM2.

`orisec-bridge.yaml` is all the firmware you need: ESPHome builds it from this file. It is a generic UART-to-TCP bridge ([oxan/esphome-stream-server](https://github.com/oxan/esphome-stream-server)) with a few diagnostic sensors.

## What you need
- An ESP32 dev board (classic ESP32 / WROOM-32, e.g. "ESP32 DevKit V1") and a USB cable for the first flash.
- Three wires to the panel COM port, and a 5 V USB supply for the ESP32.
- ESPHome: the ESPHome Device Builder app in Home Assistant, or `pip install esphome`.

## Wiring

| Panel COM port | ESP32 |
|---|---|
| T (transmit) | GPIO16 (RX2) |
| R (receive) | GPIO17 (TX2) |
| 0V | GND |
| +12V | **not connected**: power the ESP32 from USB |

The author's CP50 works wired directly. The ESP32's pins are 3.3 V, though, so if your panel's COM port idles at 5 V on T, put a level shifter or a resistor divider on GPIO16. If nothing comes through, T and R are probably swapped.

## Panel setting
Set the COM port you wired to **Mode 0 (W-RKP/UDL)**, at **115200** baud. If you use another speed, change `uart_baud` in the YAML to match.

## First flash
1. Copy `orisec-bridge.yaml` into your ESPHome config folder (in HA: `/config/esphome/`).
2. Add these to ESPHome's `secrets.yaml`:
   ```yaml
   wifi_ssid: "YourWiFi"
   wifi_password: "your-wifi-password"
   ota_password: "pick-a-password"
   orisec_bridge_ap_password: "pick-a-password"   # fallback hotspot, 8+ characters
   ```
   In the ESPHome Device Builder, **Secrets** (top right) opens this file.
3. Plug the ESP32 into the computer and **Install → Plug into this computer** (or `esphome run orisec-bridge.yaml`). Later updates go over Wi-Fi.
4. Give the ESP32 a fixed IP address on your router (DHCP reservation).

### Optional: encryption
The bridge works without it. Encryption protects the link between Home Assistant and the bridge's own diagnostics (Wi-Fi signal, uptime, client count) and its Restart button, so nobody else on your network can read them or restart it. It doesn't cover the panel link on port 6638, which the integration protects with its user-code login, and firmware updates are already protected by `ota_password`.

To turn it on:
1. Make a key. It isn't something you look up: it's a random 32-byte key in base64 (44 characters ending in `=`) that you create once. Easiest: open the [ESPHome API docs page](https://esphome.io/components/api/), which shows a freshly generated key under `encryption:` each time it loads, and copy it. Or run `openssl rand -base64 32` in Home Assistant's **Terminal & SSH** app.
2. Add it to `secrets.yaml` as `orisec_bridge_api_key: "<the key>"`.
3. Uncomment the two `encryption:` lines under `api:` in `orisec-bridge.yaml` and install.

If the bridge is already in Home Assistant, HA asks for the new key (with the ESPHome Device Builder app it usually picks it up by itself).

## Connect the integration
Add the Orisec integration (or **Reconfigure** an existing one), choose **Serial bridge**, and enter the ESP32's IP address and port **6638**.

## Adopt the bridge in Home Assistant (recommended)
HA usually discovers the bridge: accept it under **Settings → Devices & services**. If you turned encryption on and it asks for the key, paste `orisec_bridge_api_key` from `secrets.yaml`. The bridge then shows Wi-Fi signal, uptime and **Panel link connected** in HA, and the keypad card's settings page (cog) shows a **Serial bridge** section with the Wi-Fi strength, link state and uptime.

For the card to find the bridge, the Orisec integration's host and the ESPHome device's host must be the same: both the IP address, or both the same hostname.

## Troubleshooting
- **Panel link connected** goes off: the integration lost its TCP connection (Wi-Fi or the ESP32). **Uptime** going back to zero means the ESP32 restarted. It only says whether *any* client is connected, so for "is HA talking to the panel" watch the alarm entity going unavailable instead.
- A **Panel link clients** count of 2 or more has two causes:
  - **A stale connection** (harmless, and cleared by itself). If Wi-Fi drops (a scheduled router reboot, say), the integration reconnects, but its old connection never closes properly. The bridge checks connections that have gone quiet and drops a dead one within about a minute, so the count settles back to 1. (Bridges flashed before this was added kept them until restarted.)
  - **A second real client** (a problem). Something else, such as UDL or another HA, is connected too. Both clients' requests reach the panel and their replies get mixed up, so keep it to one.
- Short dropouts while Wi-Fi restarts are normal: the integration marks the panel unavailable after about 10 seconds of silence and logs back in by itself when the link returns.
- Keep `reboot_timeout: 0s` under `api:`. Otherwise the ESP32 reboots every 15 minutes when nothing connects to its ESPHome API.
