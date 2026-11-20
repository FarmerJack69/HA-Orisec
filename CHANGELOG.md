# Changelog

Newest first. Versions match `version` in `custom_components/orisec/manifest.json`.

## 0.4.3 (Nov 2026)

A more responsive keypad and faster polling over USB and the serial bridge.

**Polling**
- **The poll interval now has a limit for each connection type** (Configure):
  - USB and serial bridge: 0.2 to 60 seconds, default **1 s** (was 2 s). On a CP50 over the bridge, 0.2 s ran for over 7 hours with 1 missed reply in 31,000 polls.
  - CM2: 1 to 60 seconds, default 2 s, as before. A setting below 1 s is raised to 1 s on the CM2, for example after switching from the bridge with Reconfigure.
  - If you set an interval yourself, it is kept. If you never changed it, USB and bridge installs now poll every 1 s instead of 2 s.
- **Intervals below 1 second poll at the rate set.** Home Assistant's own scheduler would poll back to back for most of each second at those settings (20 times a second over USB), so the integration now runs its own loop for them and waits the interval after each poll.
- **Quicker replies over USB and the serial bridge:** a read finishes as soon as every part of the answer has arrived, instead of always waiting 0.15 s for more. On the bridge this took the typical reply from about 0.42 s to 0.26 s. The CM2 keeps the wait, as its replies can arrive in several pieces.
- **Background reads no longer hold up the display:** the panel log check and the rotating extra reads (zone timers, wireless details, network devices, panel power) run after each poll's data is published, on their own timers.
- **Poll statistics in diagnostics** (`poll_stats`): polls per second, misses, re-logins and reply times, to help judge how fast your connection can go.

**Keypad card**
- **Haptic feedback** on key presses and actions in the Home Assistant Companion app (iOS and Android), with phone vibration as a fallback in other browsers.
- **Instant code entry:** each digit shows its asterisk straight away, as the wall keypad does, including the first digit pressed on the home screen ("Enter Passcode:" appears at once). The panel's own display takes over as it catches up.
- **The clock ticks smoothly**, every second, like the wall keypad: the card keeps time itself and corrects itself from the panel at each poll, instead of jumping whenever a poll arrives. No jump when you reopen the app.
- **No blank flash between screens:** the panel can send an empty screen for a moment while it changes mode (for example on logging off the engineer menu); the wall keypad never shows it, and now the card doesn't either.

## 0.4.2 (Nov 2026)

- The first setup screen (USB / Network / Serial bridge) always shows its labels. Before, the three buttons could show blank, with just an arrow, if your browser still had text cached from an older Orisec integration. Reconfigure gets the same fix.
- README: the keypad card is one card, **Orisec alarm**, with tabs. The section now says how to add it and how to refresh the Companion app.

## 0.4.1 (Nov 2026)

- The keypad card now loads with its version in the address, so after an update a normal refresh picks up the new card. No dashboard resource is needed: if you added one for `orisec-keypad-card.js` by hand, remove it.
- Keypads, wireless expanders and external sounders link to the panel's device the way Home Assistant 2026.8 and later expect. This clears a deprecation warning that would have become an error in Home Assistant 2027.8. Older Home Assistant versions keep working as before.

## 0.4.0 (Nov 2026)

First public release. Everything in the README's *What you get* list, used daily on a CP50 (firmware 9.10) over USB, a CM2 and an ESP32 serial bridge.

- Integration icon and logo (`brand/`, Home Assistant 2026.3+).
- Diagnostics now leave out the host address, panel serial and area names, so the file is safe to attach to a public issue.
- HACS packaging.
