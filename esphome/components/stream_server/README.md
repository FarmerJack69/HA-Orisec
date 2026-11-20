# stream_server (Orisec bridge copy)

A copy of [oxan/esphome-stream-server](https://github.com/oxan/esphome-stream-server) (GPL-3.0, see `LICENSE.txt`; upstream commit `9e05b0a`), changed so that clients whose connection has died are dropped:

- **TCP keepalive** on every client: a client silent for 30 s is probed every 5 s and dropped after 3 unanswered probes. A live client answers and is never touched, so a deliberate second client (UDL, say) keeps working.
- **Any socket error other than "try again" counts as a disconnect.** Upstream only treated a reset as a disconnect, so a connection that failed another way stayed in the list for good.

Without these, a Wi-Fi drop (a scheduled router reboot, for example) left the integration's old connection behind, and "Panel link clients" crept up by one each time.

Everything else, including the YAML options and sensors, is unchanged.
