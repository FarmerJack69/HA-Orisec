# Changelog

Newest first. Versions match `version` in `custom_components/orisec/manifest.json`.

## 0.4.1 (Nov 2026)

- The keypad card now loads with its version in the address, so after an update a normal refresh picks up the new card. No dashboard resource is needed: if you added one for `orisec-keypad-card.js` by hand, remove it.
- Keypads, wireless expanders and external sounders link to the panel's device the way Home Assistant 2026.8 and later expect. This clears a deprecation warning that would have become an error in Home Assistant 2027.8. Older Home Assistant versions keep working as before.

## 0.4.0 (Nov 2026)

First public release. Everything in the README's *What you get* list, used daily on a CP50 (firmware 9.10) over USB, a CM2 and an ESP32 serial bridge.

- Integration icon and logo (`brand/`, Home Assistant 2026.3+).
- Diagnostics now leave out the host address, panel serial and area names, so the file is safe to attach to a public issue.
- HACS packaging.
