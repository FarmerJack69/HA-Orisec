/*
 * Orisec card: Home / Zones / Activity / Log / Keypad, laid out like the official app.
 *
 *   type: custom:orisec-keypad-card
 *   entity: sensor.orisec_cp50_keypad_display   # the integration's display sensor
 *   title: Alarm                                 # optional
 *   view: home                                   # optional start tab: home | zones | activity | log | keypad
 *   tabs: [home, zones, activity, log, keypad]   # optional, which tabs to show
 *   recent_minutes: 5                            # optional, how long a cleared zone stays blue
 *   notify: true                                 # optional: false hides the bell / zone Notify buttons
 *   pin: device                                  # optional: device (default) | none | ask | remember
 *       device   = each phone/screen chooses for itself in the card's settings (cog)
 *       none     = act as the user the integration is logged in as (no PIN)
 *       ask      = ask for a panel PIN on every Arm / Disarm / Reset / Omit (e.g. a wall screen)
 *       remember = use a PIN saved once on this device (like setting up the official app)
 *   (Activity shows the last 12 hours, grouped 5/10/30 min and 1/2/3/5/12 h, as the app.)
 *
 * Notifications (bell, top right): each HA user chooses which kinds of panel
 * event reach their phones, as the official app's per-phone settings; the
 * Notify button on a zone sends a notification when that zone goes active.
 *
 * Areas, zones and reset buttons are found automatically from the panel's
 * device. One press arms / disarms / resets, as in the app. PA is never offered.
 *
 * With a PIN the panel acts as that user: their areas and permissions, their
 * name in the panel log. The panel checks the PIN; HA never stores it. A PIN
 * "remembered" on a device is kept in that browser's local storage.
 */

const KEYS = [
  [{ k: "1", l: "1", s: "" }, { k: "2", l: "2", s: "abc" }, { k: "3", l: "3", s: "def" }, { k: "F", l: "Arm", c: "fn" }],
  [{ k: "4", l: "4", s: "ghi" }, { k: "5", l: "5", s: "jkl" }, { k: "6", l: "6", s: "mno" }, { k: "P", l: "Part", c: "fn" }],
  [{ k: "7", l: "7", s: "pqrs" }, { k: "8", l: "8", s: "tuv" }, { k: "9", l: "9", s: "wxyz" }, { k: "A", l: "Area", c: "fn" }],
  [{ k: "Y", l: "Enter ✓", c: "fn" }, { k: "0", l: "0", s: "␣" }, { k: "N", l: "Clear ✕", c: "fn" }, { k: "O", l: "Omit", c: "fn" }],
  [null, null, { k: "C", l: "♪ ?", c: "fn", t: "Chime / help" }, { k: "R", l: "←", c: "fn back", t: "Back" }],
  [{ k: "l", l: "◀", c: "nav", t: "Left" }, { k: "u", l: "▲", c: "nav", t: "Up" }, { k: "d", l: "▼", c: "nav", t: "Down" }, { k: "r", l: "▶", c: "nav", t: "Right" }],
];

const TABS = { home: "Home", zones: "Zones", activity: "Activity", log: "Log", keypad: "Keypad" };
// Event-log icons, matched on the panel's own event text (first match wins).
const LOG_ICONS = [
  [/Disarmed/, "mdi:lock-open-variant", "#2fbf4f"], [/Part Armed/, "mdi:lock", "#f08a24"],
  [/Armed/, "mdi:lock", "#e53935"], [/Remote Access|User|Access|Tag/, "mdi:account-outline", "#1ea0d8"],
  [/Exit|Entry/, "mdi:door-open", "#1ea0d8"], [/Reinstated/, "mdi:eye-outline", "#2fbf4f"],
  [/Omitted/, "mdi:eye-off-outline", "#f08a24"], [/Restore|Restored|Found|Reset/, "mdi:check-circle-outline", "#2fbf4f"],
  [/Alarm|Tamper|Lost|Fault|Fail|Lockout|Duress/, "mdi:alarm-light-outline", "#e53935"],
  [/AC |Power|Battery/, "mdi:power-plug-outline", "#f08a24"],
];
const logIcon = (t) => LOG_ICONS.find(([re]) => re.test(t || "")) || [0, "mdi:information-outline", "#9e9e9e"];
const LOG_PAGE = 10;
// Activity groups as in the app: minutes, then hours, back to 12 hours.
// Longest an empty screen from the panel is covered by the previous one (ms).
const BLANK_HOLD_MS = 1500;

const ACT_BUCKETS = [5, 10, 30, 60, 120, 180, 300, 720];
const ACT_WINDOW_MIN = ACT_BUCKETS[ACT_BUCKETS.length - 1];
const actLabel = (m) => (m < 60 ? `${m} Minutes` : m === 60 ? "1 Hour" : `${m / 60} Hours`);
const PARTS = [
  { n: 1, attr: "arm_home", svc: "alarm_arm_home" },
  { n: 2, attr: "arm_night", svc: "alarm_arm_night" },
  { n: 3, attr: "arm_vacation", svc: "alarm_arm_vacation" },
];
// Zone icons by zone type, after the official app.
const ZONE_ICONS = [
  [/^Final Exit/, "mdi:exit-run"], [/^Entry Route/, "mdi:door-open"], [/Fire/, "mdi:fire"],
  [/PA|Medical/, "mdi:shield-alert-outline"], [/Door Bell/, "mdi:gesture-tap"],
  [/Key/, "mdi:key-variant"], [/Flood/, "mdi:water-alert-outline"], [/^CO /, "mdi:molecule-co"],
  [/24 Hour|Log|Monitor|Warning|Fault|Auxiliary|Counter/, "mdi:clipboard-check-outline"],
  [/Tamper/, "mdi:shield-off-outline"],
];
const zoneIcon = (type) => (ZONE_ICONS.find(([re]) => re.test(type || "")) || [0, "mdi:walk"])[1];
const LOCK = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 2a5 5 0 0 0-5 5v3H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-1V7a5 5 0 0 0-5-5m0 2a3 3 0 0 1 3 3v3H9V7a3 3 0 0 1 3-3m0 9a2 2 0 0 1 1 3.7V19h-2v-2.3A2 2 0 0 1 12 13"/></svg>';
const UNLOCK = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M18 10h-8V7a3 3 0 0 1 5.8-1h2.1A5 5 0 0 0 8 7v3H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2m-6 3a2 2 0 0 1 1 3.7V19h-2v-2.3A2 2 0 0 1 12 13"/></svg>';
const RUN = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M13.5 5.5a2 2 0 1 0 0-4 2 2 0 0 0 0 4M9.8 8.9 7 23h2.1l1.8-8 2.1 2v6h2v-7.5l-2.1-2 .6-3A7.3 7.3 0 0 0 19 13v-2a5 5 0 0 1-4.3-2.4l-1-1.6a2 2 0 0 0-1.7-1c-.3 0-.5 0-.8.1L6 8.3V13h2V9.6z"/></svg>';
const RESET = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 4a8 8 0 0 0-7.4 5H2l3.5 4L9 9H6.8A6 6 0 1 1 6 13H4a8 8 0 1 0 8-9"/></svg>';

const CSS = `
  :host { display: block; }
  ha-card { padding: 12px; container-type: inline-size; }
  .head { display: flex; justify-content: space-between; align-items: center; margin: 0 2px 8px; gap: 8px; }
  .title { font-size: 1.05em; font-weight: 500; }
  .badge { font-size: .8em; padding: 3px 10px; border-radius: 12px; background: var(--secondary-background-color); white-space: nowrap; }
  .badge.armed { background: var(--error-color, #db4437); color: #fff; }
  .badge.part { background: #e8891c; color: #fff; }
  .badge.triggered { background: #b00020; color: #fff; animation: blink 1s steps(2) infinite; }
  .badge.unavailable { background: var(--warning-color, #ffa600); color: #000; }
  @keyframes blink { 50% { opacity: .35; } }

  .tabs { display: flex; gap: 6px; margin-bottom: 10px; }
  .tabs button { flex: 1; min-height: 36px; font-size: .9em; border-radius: 8px; }
  .tabs button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  .view { display: none; }
  .view.on { display: block; }

  button { font: inherit; color: var(--primary-text-color); background: var(--card-background-color);
           border: 2px solid var(--divider-color, #444); border-radius: 10px; cursor: pointer;
           display: flex; align-items: center; justify-content: center; gap: .35em;
           user-select: none; -webkit-tap-highlight-color: transparent; touch-action: manipulation;
           transition: transform .06s, background .06s; }
  button:active, button.hit { transform: scale(.95); }
  button:disabled { opacity: .4; cursor: default; }

  /* ---- home ---- */
  /* As the app: up to 3 areas per row; a 4th and 5th wrap to a centred row below. */
  .strip { display: flex; flex-wrap: wrap; justify-content: center; gap: 10px 8px; padding: 8px; border: 2px solid var(--divider-color, #444); border-radius: 10px; margin-bottom: 12px; }
  .strip > div { flex: 0 0 calc((100% - 16px) / 3); text-align: center; font-size: .85em; min-width: 0; }
  .strip span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pill { height: 8px; border-radius: 4px; margin-top: 6px; }
  .c-disarmed { --c: #2fbf4f; } .c-full { --c: #e53935; } .c-part { --c: #f08a24; }
  .c-busy { --c: #f0c020; } .c-alarm { --c: #e53935; } .c-down { --c: #888; }
  .pill { background: var(--c); }
  .c-alarm .pill, .c-alarm.ring, .c-busy .pill, .c-busy.ring { animation: blink 1s steps(2) infinite; }

  .acts { display: flex; flex-wrap: wrap; justify-content: center; gap: 10px; margin: 4px 0 14px; }
  .act { flex-direction: column; width: clamp(70px, 22cqw, 110px); min-height: clamp(70px, 22cqw, 110px); padding: 6px; font-size: .85em; text-align: center; line-height: 1.15; }
  .act svg { width: 44%; }
  .act.quick svg { color: #2fbf4f; }
  .act.full svg { color: #e53935; } .act.part svg { color: #f08a24; } .act.off svg { color: #2fbf4f; } .act.reset svg { color: #2fbf4f; }
  .act.small { width: clamp(52px, 15cqw, 72px); min-height: clamp(52px, 15cqw, 72px); font-size: .7em; }

  .area { border: 2px solid var(--divider-color, #444); border-radius: 10px; padding: 8px 10px; margin-bottom: 8px; }
  .area { text-align: center; }
  .area.alarm { border-color: #e53935; }
  .area.eng { border-color: #f08a24; }  /* app: engineer reset needed */
  .solo .status.eng { border: 2px solid #f08a24; border-radius: 10px; padding: 6px 10px; }
  .area .name { font-weight: 600; font-size: 1.05em; }
  .area .mid { display: flex; align-items: center; gap: 12px; margin: 6px 0; min-height: 44px; }
  .area .inl { flex: 1; display: flex; flex-wrap: wrap; gap: 8px; }
  .ring { width: 40px; height: 40px; border-radius: 50%; border: 4px solid var(--c); flex: none; box-sizing: border-box; }
  .area .status { font-size: .95em; opacity: .9; }
  .area.closed { display: flex; align-items: center; gap: 12px; cursor: pointer; }
  .area.closed .txt { flex: 1; min-width: 0; }
  .chev { font-size: 1.6em; opacity: .7; width: 32px; text-align: center; cursor: pointer; }
  .act.small svg { width: 62%; }

  .solo { text-align: center; margin: 6px 0 14px; }
  .solo .ring { width: clamp(140px, 48cqw, 220px); height: clamp(140px, 48cqw, 220px); border-width: 14px;
                margin: 0 auto 12px; display: grid; place-items: center; }
  .solo .ring span { font-size: 1.15em; }
  .solo .status { border: 2px solid var(--divider-color, #444); border-radius: 10px; padding: 8px; }

  /* ---- zones / activity ---- */
  .ftabs { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
  .ftabs button { flex: 1 1 0; min-width: 0; padding: 8px 6px; font-size: .85em; border-radius: 8px; }
  .ftabs button.on { border-color: var(--primary-color); color: var(--primary-color); }
  .zone { display: flex; align-items: center; gap: 12px; border: 2px solid var(--divider-color, #444); border-radius: 10px;
          padding: 8px 12px; margin-bottom: 6px; }
  .zone.recent { border-color: #1ea0d8; }
  .zone.active { border-color: #f08a24; }
  .zone.tamper { border-color: #e53935; }
  .zone ha-icon { --mdc-icon-size: 32px; color: #2fbf4f; flex: none; }
  .zone.active ha-icon { color: #f08a24; }
  .zone.tamper ha-icon { color: #e53935; }
  .zone .zt { flex: 1; text-align: center; }
  .zone .zn { font-weight: 500; }
  .zone .zs { font-size: .9em; opacity: .85; }
  .zone .when { font-size: .75em; opacity: .6; white-space: nowrap; }
  .zone .zbtn { flex: none; min-width: 72px; padding: 10px 12px; border-radius: 8px; font-size: .95em;
                background: var(--secondary-background-color, #2b2f33); border: 2px solid #111; }
  .zone .zbtn.on { border-color: #f08a24; }  /* app: omitted = orange outline */
  .zone .zbtn.busy { opacity: .5; }
  .zone .zbtn.ghost { visibility: hidden; }  /* keeps text centred when a zone has no Omit */
  .group { border: 2px solid var(--divider-color, #444); border-radius: 10px; padding: 8px; margin-bottom: 10px; }
  .group h4 { margin: 2px 0 8px; text-align: center; font-weight: 500; }
  .empty { text-align: center; opacity: .7; padding: 16px; }
  .ldate { text-align: center; opacity: .8; margin: 6px 0; }
  .ev { display: flex; align-items: center; gap: 12px; border: 2px solid var(--divider-color, #444); border-radius: 10px;
        padding: 8px 12px; margin-bottom: 6px; }
  .ev ha-icon { --mdc-icon-size: 30px; flex: none; }
  .ev .et { flex: 1; text-align: center; }
  .ev .en { font-weight: 500; }
  .ev .ew { font-size: .9em; opacity: .85; }
  .ev .ei { align-self: flex-end; font-size: .8em; opacity: .7; }
  .more { width: 100%; padding: 10px; border-radius: 10px; margin-top: 4px; }
  .tabs button.busy, .more.busy { animation: pulse 1s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: .55; } }

  /* ---- keypad ---- */
  .lcd { container-type: inline-size; position: relative; background: #0018c8; color: #fff; border-radius: 8px;
         border: 3px solid #1b1b1b; aspect-ratio: 128 / 44; overflow: hidden;
         font-family: "DejaVu Sans Mono", "Consolas", monospace; }
  .lcd.off { background: #1e2a44; color: #9aa7c7; }
  .row { position: absolute; left: 3%; right: 3%; height: 25%; }
  .seg { position: absolute; top: 50%; transform: translateY(-50%); white-space: pre; font-size: 5.6cqw; line-height: 1; }
  .seg .small { font-size: 3.6cqw; }
  .seg.center { left: 50% !important; transform: translate(-50%, -50%); }
  .seg.right { left: auto !important; right: 0; }
  .seg .alt { display: inline-grid; }
  .seg .alt > * { grid-area: 1 / 1; }
  .seg .alt .a1 { animation: altA 2s steps(1) infinite; }
  .seg .alt .a2 { animation: altB 2s steps(1) infinite; }
  @keyframes altA { 0% { opacity: 1; } 50% { opacity: 0; } }
  @keyframes altB { 0% { opacity: 0; } 50% { opacity: 1; } }
  .seg ha-icon.st { --mdc-icon-size: 4.6cqw; margin-right: .3cqw; vertical-align: -.5cqw; }
  .seg .inv { background: #fff; color: #0018c8; padding: 0 2px; }
  .bar { position: absolute; top: 30%; height: 40%; border: 1px solid #fff; left: 10%; right: 10%; }
  .bar > div { height: 100%; background: #fff; }
  .msg { position: absolute; inset: 0; display: grid; place-items: center; font-size: 5cqw; text-align: center; padding: 0 8%; }
  .pad { display: grid; grid-template-columns: repeat(4, 1fr); gap: clamp(6px, 2cqw, 12px); margin-top: 12px; }
  .pad button { min-height: clamp(44px, 13cqw, 76px); }
  .pad button:active, .pad button.hit { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .num .big { font-size: clamp(20px, 7cqw, 40px); font-weight: 500; }
  .num .sub { font-size: clamp(10px, 3cqw, 16px); opacity: .7; align-self: flex-end; margin-bottom: .6em; }
  .fn { font-size: clamp(13px, 3.8cqw, 20px); font-weight: 500; }
  .back { font-size: clamp(22px, 7cqw, 38px); }
  .nav { font-size: clamp(18px, 5.5cqw, 30px); }
  .gap { visibility: hidden; }
  /* ---- PIN pad / settings overlay ---- */
  ha-card { position: relative; }
  .cog { width: 34px; height: 34px; padding: 6px; border-radius: 50%; border-width: 1px; flex: none; }
  .cog svg { width: 100%; height: 100%; opacity: .75; }
  .head .right { display: flex; align-items: center; gap: 8px; }
  /* A modal <dialog> in the browser's top layer: centred on the screen whatever the
     card's height or scroll position, scrolling inside itself when taller than the screen. */
  dialog.ovl { position: fixed; inset: 0; margin: auto; box-sizing: border-box;
         width: min(560px, calc(100vw - 24px)); max-width: none; max-height: calc(100dvh - 24px); height: fit-content;
         border: 1px solid var(--divider-color, rgba(127,127,127,.3)); color: var(--primary-text-color);
         background: var(--card-background-color, var(--ha-card-background, #1c1c1c));
         border-radius: var(--ha-card-border-radius, 12px); padding: 20px 16px; overflow: auto;
         container-type: inline-size; font-family: var(--paper-font-body1_-_font-family, inherit); }
  dialog.ovl[open] { display: flex; flex-direction: column; align-items: center; gap: 12px; }
  dialog.ovl::backdrop { background: rgba(0, 0, 0, .55); }
  .ovl h3 { margin: 0; font-weight: 500; text-align: center; }
  .ovl .note { font-size: .9em; opacity: .8; text-align: center; max-width: 420px; }
  .ovl .err { color: var(--error-color, #db4437); min-height: 1.2em; text-align: center; }
  .dots { display: flex; gap: 12px; min-height: 22px; }
  .dots span { width: 16px; height: 16px; border-radius: 50%; border: 2px solid var(--primary-text-color); }
  .dots span.f { background: var(--primary-text-color); }
  .ppad { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; width: min(100%, 330px); }
  .ppad button { min-height: clamp(52px, 14cqw, 76px); font-size: clamp(20px, 6cqw, 32px); }
  .ppad button.ok { border-color: #2fbf4f; color: #2fbf4f; }
  .ppad button:active { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .ovl .cancel { padding: 10px 28px; border-radius: 10px; }
  .modes { display: flex; flex-direction: column; gap: 8px; width: min(100%, 420px); }
  .modes button { padding: 12px; border-radius: 10px; flex-direction: column; align-items: stretch; text-align: left; }
  .modes button.on { border-color: var(--primary-color); }
  .modes b { font-weight: 500; }
  .modes small { opacity: .75; }
  .bridge { display: flex; flex-direction: column; align-items: center; gap: 8px; width: min(100%, 420px); }
  .brows { display: flex; flex-direction: column; gap: 6px; width: 100%; }
  .brow { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: 10px;
    border: 1px solid var(--divider-color, rgba(127,127,127,.3)); }
  .brow span { flex: 1; } .brow b { font-weight: 500; }
  .brow.bad { border-color: var(--warning-color, #ff9800); }
  .sysdev { display: flex; flex-direction: column; align-items: center; gap: 8px; width: min(100%, 420px); }
  .drow { display: grid; grid-template-columns: 24px 1fr auto; gap: 2px 10px; align-items: center; padding: 10px 12px;
    border-radius: 10px; border: 1px solid var(--divider-color, rgba(127,127,127,.3)); width: 100%; box-sizing: border-box; }
  .drow b { font-weight: 500; } .drow small { grid-column: 2 / 4; opacity: .75; }
  .drow .vals { display: flex; gap: 12px; white-space: nowrap; }
  .zone .zbtn.nt { min-width: 64px; margin-left: -4px; }
  .zone .zbtn.nt.on { border-color: #1ea0d8; color: #1ea0d8; }  /* notify on */
  dialog.ovl.nset[open] { justify-content: flex-start; align-items: stretch; }
  .nset h3 { margin-top: 4px; }
  .nset .sec { font-weight: 500; margin: 6px 0 2px; opacity: .85; }
  .tgl { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .tgl.one { grid-template-columns: 1fr; }
  .tgl button { display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; border-radius: 10px; text-align: left; }
  .tgl button i { width: 38px; height: 22px; border-radius: 11px; background: var(--divider-color, #555); position: relative; flex: none; font-style: normal; }
  .tgl button i::after { content: ""; position: absolute; top: 2px; left: 2px; width: 18px; height: 18px; border-radius: 50%; background: #fff; transition: left .15s; }
  .tgl button.on i { background: #2fbf4f; }
  .tgl button.on i::after { left: 18px; }
  .tgl button[disabled] { opacity: .45; }
  .nset select { padding: 8px; border-radius: 8px; background: var(--card-background-color); color: var(--primary-text-color); border: 1px solid var(--divider-color, #555); }
  .nset .row2 { display: flex; gap: 10px; justify-content: center; margin-top: 6px; }
  .nset .row2 button { padding: 10px 24px; border-radius: 10px; }
  .nset .row2 .save { border-color: #2fbf4f; color: #2fbf4f; }
  .bell { width: 34px; height: 34px; padding: 5px; border-radius: 50%; border-width: 1px; flex: none; }
  .bell ha-icon { --mdc-icon-size: 22px; opacity: .8; }
  .bell.on ha-icon { color: #1ea0d8; opacity: 1; }
  .toast { text-align: center; color: var(--error-color, #db4437); font-size: .9em; min-height: 1.2em; margin-top: 6px; }
`;

const PIN_MODES = { none: "No PIN", ask: "Ask every time", remember: "Remember PIN" };
const PIN_HELP = {
  none: "Arm and disarm as the user the integration is logged in as.",
  ask: "Ask for a panel PIN on every Arm, Disarm, Reset and Omit. Good for a shared screen.",
  remember: "Enter a PIN once; this device then always acts as that user, like the official app.",
};
const PIN_MAX_FAILS = 3;       // refused PINs in a row before this device waits
const PIN_LOCK_SECONDS = 60;
const EXIT_REUSE_SECONDS = 120; // a PIN that started an exit timer also covers Quick Arm / cancel this long
const GEAR = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 15.5A3.5 3.5 0 1 1 12 8.5a3.5 3.5 0 0 1 0 7m7.4-2.5.1-1-.1-1 2.1-1.6-2-3.5-2.5 1a7.4 7.4 0 0 0-1.7-1L15 3h-4l-.4 2.6c-.6.3-1.2.6-1.7 1l-2.5-1-2 3.5L6.6 11l-.1 1 .1 1-2.1 1.6 2 3.5 2.5-1c.5.4 1.1.7 1.7 1L11 21h4l.4-2.6c.6-.3 1.2-.6 1.7-1l2.5 1 2-3.5z"/></svg>';

const pad2 = (n) => String(n).padStart(2, "0");

class OrisecKeypadCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity) throw new Error("Set 'entity' to the Orisec keypad display sensor");
    this._config = config;
    const tabs = (config.tabs || Object.keys(TABS)).filter((t) => TABS[t]);
    this._tabs = tabs.length ? tabs : Object.keys(TABS);
    this._view = this._tabs.includes(config.view) ? config.view : this._tabs[0];
    this._hidden = new Set(); // Zones tab: areas toggled off (as the app, all start selected)
    this._open = {};
    this._built = false;
  }

  getCardSize() { return 10; }

  static getStubConfig(hass) {
    const e = Object.keys(hass.states).find((id) => id.startsWith("sensor.") && hass.states[id].attributes.layout);
    return { entity: e || "" };
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (!this._built) this._build();
    this._render();
    if (first) {
      this._startTimers();
      this._loadNotify();  // bell state and the zones' Notify buttons
    }
  }

  connectedCallback() { if (this._hass) this._startTimers(); }
  disconnectedCallback() { clearInterval(this._tick); clearInterval(this._clkTick); this._tick = this._clkTick = null; }

  _startTimers() {
    if (!this._tick) this._tick = setInterval(() => this._refreshTimes(), 15000);
    // The panel only sends "show the time here"; like the wall keypad, the card ticks it.
    if (!this._clkTick) this._clkTick = setInterval(() => this._tickClock(), 200);
  }

  // A light tap: the Companion app turns this into a native haptic (iOS and Android);
  // other browsers fall back to the vibration API where there is one.
  _haptic(kind = "light") {
    window.dispatchEvent(new CustomEvent("haptic", { detail: kind }));
    const app = window.externalApp || (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.externalBus);
    if (!app && navigator.vibrate) navigator.vibrate(kind === "medium" ? 30 : 15);
  }

  // ------------------------------------------------------------- discovery
  _discover() {
    const h = this._hass;
    const reg = h.entities || {};
    const dev = reg[this._config.entity] && reg[this._config.entity].device_id;
    const mine = (id) => (dev ? reg[id] && reg[id].device_id === dev : id.includes("orisec"));
    const ids = Object.keys(h.states).filter(mine);
    const areas = ids.filter((id) => id.startsWith("alarm_control_panel.") && h.states[id].attributes.area)
      .map((id) => h.states[id]).sort((a, b) => a.attributes.area - b.attributes.area);
    const zones = ids.filter((id) => id.startsWith("binary_sensor.") && h.states[id].attributes.zone)
      .map((id) => h.states[id]).sort((a, b) => a.attributes.zone - b.attributes.zone);
    const resets = {};
    ids.filter((id) => id.startsWith("button.") && h.states[id].attributes.area)
      .forEach((id) => { resets[h.states[id].attributes.area] = id; });
    return { areas, zones, resets };
  }

  // ------------------------------------------------------------- build
  _build() {
    const root = this.shadowRoot || this.attachShadow({ mode: "open" });
    const rows = KEYS.map((row) => row.map((b) => {
      if (!b) return `<button class="gap" tabindex="-1" aria-hidden="true"></button>`;
      const inner = b.s !== undefined ? `<span class="big">${b.l}</span><span class="sub">${b.s}</span>` : b.l;
      return `<button class="${b.c || "num"}" data-k="${b.k}" title="${b.t || b.l}" aria-label="${b.t || b.l}">${inner}</button>`;
    }).join("")).join("");
    const tabs = this._tabs.length > 1
      ? `<div class="tabs">${this._tabs.map((t) => `<button data-tab="${t}">${TABS[t]}</button>`).join("")}</div>` : "";
    root.innerHTML = `<style>${CSS}</style>
      <ha-card>
        <div class="head"><span class="title"></span>
          <span class="right"><span class="badge"></span>${this._config.notify !== false
            ? `<button class="bell" data-nset title="Alarm notifications" aria-label="Alarm notifications"><ha-icon icon="mdi:bell-outline"></ha-icon></button>` : ""}${this._cfgPin() === "device" || this._bridge() || this._devices().length
            ? `<button class="cog" data-settings title="Settings" aria-label="Settings">${GEAR}</button>` : ""}</span></div>
        ${tabs}
        <div class="view" data-v="home"></div>
        <div class="view" data-v="zones"></div>
        <div class="view" data-v="activity"></div>
        <div class="view" data-v="log"></div>
        <div class="view" data-v="keypad"><div class="lcd"></div><div class="pad">${rows}</div></div>
        <div class="toast"></div>
        <dialog class="ovl"></dialog>
      </ha-card>`;
    root.addEventListener("click", (ev) => this._click(ev));
    this._built = true;
    this._showView();
  }

  _click(ev) {
    const t = ev.target.closest("[data-tab],[data-k],[data-act],[data-open],[data-filter],[data-omit],[data-more],[data-pin],[data-pinx],[data-settings],[data-mode],[data-nset],[data-ntg],[data-nsave],[data-nzone]");
    if (!t || t.disabled) return;
    if (t.dataset.pin !== undefined) { this._padKey(t.dataset.pin); return; }
    if (t.dataset.pinx) { this._padAct(t.dataset.pinx); return; }
    if (t.dataset.settings !== undefined) { this._openSettings(); return; }
    if (t.dataset.nset !== undefined) { this._openNotify(); return; }
    if (t.dataset.ntg) { this._notifyToggle(t.dataset.ntg); return; }
    if (t.dataset.nsave !== undefined) { this._notifySave(); return; }
    if (t.dataset.nzone) { this._zoneNotify(Number(t.dataset.nzone), t); return; }
    if (t.dataset.mode) { this._chooseMode(t.dataset.mode); return; }
    if (t.dataset.tab) {
      if (t.dataset.tab === "log" && this._view === "log") { this._loadLog(true); return; }
      this._view = t.dataset.tab;
      if (this._view === "zones") { this._hidden.clear(); this._loadNotify(); }  // the app opens Zones with every area selected
      this._showView(); return;
    }
    if (t.dataset.more !== undefined) { this._loadLog(true); return; }
    if (t.dataset.filter !== undefined) {
      const a = Number(t.dataset.filter);
      if (this._hidden.has(a)) this._hidden.delete(a); else this._hidden.add(a);
      this._lastZones = null; this._render(); return;
    }
    if (t.dataset.open) { const a = t.dataset.open; this._open[a] = this._open[a] !== true; this._lastHome = null; this._render(); return; }
    if (t.dataset.k) { this._press(t); return; }
    if (t.dataset.omit) { this._omit(t); return; }
    if (t.dataset.act) this._action(t);
  }

  _showView() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === this._view));
    root.querySelectorAll(".view").forEach((v) => v.classList.toggle("on", v.dataset.v === this._view));
    if (this._view === "activity") this._loadActivity();
    if (this._view === "log") this._loadLog(false);
    this._lastHome = this._lastZones = null;
    this._render();
  }

  // ------------------------------------------------------------- actions
  _toast(msg) {
    const el = this.shadowRoot.querySelector(".toast");
    el.textContent = msg || "";
    clearTimeout(this._toastT);
    if (msg) this._toastT = setTimeout(() => { el.textContent = ""; }, 4000);
  }

  async _press(btn) {
    btn.classList.add("hit");
    setTimeout(() => btn.classList.remove("hit"), 140);
    this._haptic();
    this._echoKey(btn.dataset.k);
    const st = this._hass.states[this._config.entity];
    const data = { key: btn.dataset.k };
    if (st && st.attributes.panel_entry_id) data.config_entry_id = st.attributes.panel_entry_id;
    try { await this._hass.callService("orisec", "send_key", data); }
    catch (err) { this._toast(String(err.message || err)); }
  }

  // Omit / reinstate one zone (the app's per-zone Omit button). The integration
  // confirms from the zone status, so a refusal comes back as an error toast.
  _omit(btn) {
    if (btn.classList.contains("busy")) return;
    const z = this._hass.states[btn.dataset.omit];
    // The omit command only changes a manual omit; a part-set omit lasts until disarm.
    const a = (z && z.attributes) || {};
    const manual = a.manual_omitted !== undefined ? a.manual_omitted : a.omitted;
    const label = `${manual ? "Reinstate" : "Omit"} ${z ? this._zoneName(z) : "zone"}`;
    this._run(label, async (code) => {
      btn.classList.add("busy");
      try {
        await this._hass.callService("orisec", "omit_zone",
          { entity_id: btn.dataset.omit, omit: !manual, ...(code ? { code } : {}) });
      } finally {
        btn.classList.remove("busy");
        this._lastZones = null;
        this._render();
      }
    });
  }

  _action(btn) {
    const [kind, svc, targets] = btn.dataset.act.split("|");
    const ids = targets.split(",");
    // Quick Arm, or Disarm to cancel, during the exit timer just started with a PIN.
    const exiting = ids.every((id) => (this._hass.states[id] || {}).state === "arming");
    const followup = exiting && (btn.classList.contains("quick") || svc === "alarm_disarm");
    this._run(btn.getAttribute("aria-label") || "Confirm", async (code) => {
      try {
        if (kind === "button") {
          if (code) await this._hass.callService("orisec", "reset", { entity_id: ids, code });
          else await this._hass.callService("button", "press", { entity_id: ids });
        } else {
          await this._hass.callService("alarm_control_panel", svc, { entity_id: ids, ...(code ? { code } : {}) });
        }
      } finally {
        this._lastHome = null;
        this._render();
      }
    }, followup);
  }

  // ------------------------------------------------------------- PIN
  // Per-device setting, kept in this browser's local storage (one key set per display entity).
  _cfgPin() { return ["none", "ask", "remember"].includes(this._config.pin) ? this._config.pin : "device"; }
  _key(k) { return `orisec-card:${this._config.entity}:${k}`; }
  _get(k) { try { return localStorage.getItem(this._key(k)); } catch (e) { return null; } }
  _set(k, v) {
    try { if (v === null || v === undefined) localStorage.removeItem(this._key(k)); else localStorage.setItem(this._key(k), String(v)); }
    catch (e) { /* storage unavailable: setting won't persist */ }
  }
  _pinMode() {
    const c = this._cfgPin();
    if (c !== "device") return c;
    const m = this._get("mode");
    return PIN_MODES[m] ? m : "none";
  }
  _lockLeft() { return Math.max(0, Math.ceil((Number(this._get("lock") || 0) - Date.now()) / 1000)); }
  _pinFailed() {
    const n = Number(this._get("fails") || 0) + 1;
    if (n >= PIN_MAX_FAILS) { this._set("fails", 0); this._set("lock", Date.now() + PIN_LOCK_SECONDS * 1000); }
    else this._set("fails", n);
  }
  _isPinRefused(err) { return /PIN refused|4 to 8 digits/i.test(String((err && err.message) || err)); }

  // Run an action that changes the panel, with a PIN if this device needs one.
  // followup: Quick Arm / cancel during an exit timer. In Ask mode it reuses the PIN that
  // started the exit (held in memory only, for EXIT_REUSE_SECONDS) instead of asking again.
  _run(label, fn, followup) {
    this._haptic("medium");
    const mode = this._pinMode();
    if (mode === "ask") {
      const r = this._exitPin;
      if (followup && r && Date.now() < r.until) { this._exec(fn, r.code, "ask"); return; }
      this._askPin(label, (code) => this._exec(fn, code, "ask"));
      return;
    }
    if (mode === "remember") {
      const code = this._get("code");
      if (!code) { this._toast("No PIN saved on this device: set one in the settings (cog)"); return; }
      this._exec(fn, code, "remember");
      return;
    }
    this._exec(fn, undefined, "none");
  }

  async _exec(fn, code, mode) {
    if (code && this._lockLeft()) { this._toast(`Too many wrong PINs. Try again in ${this._lockLeft()} s`); return; }
    try {
      await fn(code);
      if (code) {
        this._set("fails", 0);
        this._exitPin = { code, until: Date.now() + EXIT_REUSE_SECONDS * 1000 };
      }
    } catch (err) {
      if (code && this._isPinRefused(err)) {
        this._pinFailed();
        this._toast(mode === "remember" ? "The saved PIN was refused: set it again in the settings (cog)" : "PIN refused");
      } else this._toast(String(err.message || err));
    }
  }

  _ovl() { return this.shadowRoot.querySelector(".ovl"); }
  _closeOvl() {
    const o = this._ovl();
    if (o.open) o.close();
    o.innerHTML = ""; o.className = "ovl"; this._padCb = null; this._pin = ""; this._bridgeLast = null; this._devLast = null;
  }
  _showOvl(o) {
    if (o.open) return;
    if (!o._wired) {
      o._wired = true;
      o.addEventListener("cancel", (ev) => { ev.preventDefault(); this._closeOvl(); });  // Esc
      o.addEventListener("click", (ev) => {  // tap on the dimmed backdrop closes it
        if (ev.target !== o) return;
        const r = o.getBoundingClientRect();
        if (ev.clientX < r.left || ev.clientX > r.right || ev.clientY < r.top || ev.clientY > r.bottom) this._closeOvl();
      });
    }
    try { o.showModal(); } catch (e) { o.setAttribute("open", ""); }
    o.scrollTop = 0;
  }

  // The PIN pad. cb(code) runs when OK is pressed with 4 to 8 digits.
  _askPin(title, cb, note) {
    this._pin = "";
    this._padCb = cb;
    const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9"].map((n) => `<button data-pin="${n}">${n}</button>`).join("");
    const o = this._ovl();
    o.innerHTML = `<h3>${this._esc(title)}</h3>
      ${note ? `<div class="note">${this._esc(note)}</div>` : ""}
      <div class="dots"></div><div class="err"></div>
      <div class="ppad">${keys}<button data-pinx="clear" aria-label="Clear">✕</button><button data-pin="0">0</button>
        <button class="ok" data-pinx="ok" aria-label="OK">✓</button></div>
      <button class="cancel" data-pinx="cancel">Cancel</button>`;
    this._showOvl(o);
    this._padShow();
  }

  _padShow(err) {
    const o = this._ovl();
    o.querySelector(".dots").innerHTML = Array.from({ length: Math.max(4, this._pin.length) },
      (_, i) => `<span class="${i < this._pin.length ? "f" : ""}"></span>`).join("");
    const left = this._lockLeft();
    o.querySelector(".err").textContent = left ? `Too many wrong PINs. Try again in ${left} s` : (err || "");
  }

  _padKey(d) {
    this._haptic();
    if (this._pin.length < 8) this._pin += d;
    this._padShow();
  }

  _padAct(a) {
    if (a === "cancel") { this._closeOvl(); return; }
    if (a === "clear") { this._pin = ""; this._padShow(); return; }
    if (this._lockLeft()) { this._padShow(); return; }
    if (this._pin.length < 4) { this._padShow("A PIN is 4 to 8 digits"); return; }
    const code = this._pin, cb = this._padCb;
    this._closeOvl();
    if (cb) cb(code);
  }

  // Check a PIN with the panel without doing anything (logs in as that user and back).
  async _checkPin(code) {
    const st = this._hass.states[this._config.entity];
    const data = { code };
    if (st && st.attributes.panel_entry_id) data.config_entry_id = st.attributes.panel_entry_id;
    const res = await this._hass.callWS({ type: "call_service", domain: "orisec", service: "test_user_login",
      service_data: data, return_response: true });
    const r = (res && res.response) || {};
    if (!r.accepted) { this._pinFailed(); return null; }
    this._set("fails", 0);
    return r.user_number || 0;
  }

  _openSettings() {
    const mode = this._pinMode();
    const user = this._get("user");
    const o = this._ovl();
    const pin = this._cfgPin() === "device" ? `<h3>PIN on this device</h3>
      <div class="modes">${Object.entries(PIN_MODES).map(([m, l]) => `<button class="${m === mode ? "on" : ""}" data-mode="${m}">
        <b>${l}${m === "remember" && mode === "remember" && user ? ` (user ${this._esc(user)})` : ""}</b><small>${PIN_HELP[m]}</small></button>`).join("")}</div>
      <div class="note">Only this phone or screen is changed. Changing it away from a PIN mode needs a valid PIN.</div>
      <div class="err"></div>` : "";
    o.innerHTML = `${pin}${this._bridge() ? `<div class="bridge">${this._bridgeHtml()}</div>` : ""}${this._devices().length
      ? `<div class="sysdev">${this._devicesHtml()}</div>` : ""}
      <button class="cancel" data-pinx="cancel">Close</button>`;
    this._showOvl(o);
    this._padCb = null;
  }

  _chooseMode(m) {
    const cur = this._pinMode();
    const apply = (code, user) => {
      this._set("mode", m);
      if (m === "remember") { this._set("code", code); this._set("user", user); }
      else { this._set("code", null); this._set("user", null); }
      this._toast(m === "remember" ? `PIN saved: this device now acts as user ${user}` : `This device: ${PIN_MODES[m]}`);
    };
    const verify = (title, note) => this._askPin(title, async (code) => {
      if (this._lockLeft()) { this._toast(`Too many wrong PINs. Try again in ${this._lockLeft()} s`); return; }
      try {
        const user = await this._checkPin(code);
        if (user === null) { this._toast("PIN refused"); return; }
        apply(code, user);
      } catch (err) { this._toast(String(err.message || err)); }
    }, note);
    if (m === "remember") { verify("PIN to remember", "This device will act as the panel user with this PIN."); return; }
    if (m === cur) { this._closeOvl(); return; }
    if (cur === "none") { apply(); this._closeOvl(); return; }
    verify("Enter a PIN to change this", "Any valid panel PIN.");
  }

  // ------------------------------------------------------------- serial bridge
  // The display sensor's "bridge" attribute names the ESPHome bridge's entities
  // (only for the serial-bridge transport, and only when the ESP32 is adopted in HA).
  _bridge() {
    const st = this._hass && this._hass.states[this._config.entity];
    return (st && st.attributes.bridge) || null;
  }

  _bridgeHtml() {
    const b = this._bridge();
    if (!b) return "";
    const s = (id) => (id && this._hass.states[id]) || null;
    const ok = (x) => x && x.state !== "unavailable" && x.state !== "unknown";
    const wifi = s(b.wifi), up = s(b.uptime), link = s(b.link), cl = s(b.clients);
    const rows = [];
    if (b.wifi) {
      const dbm = ok(wifi) ? Math.round(Number(wifi.state)) : null;
      const [bars, word] = dbm === null ? ["off-outline", "No reading"]
        : dbm >= -55 ? [4, "Excellent"] : dbm >= -67 ? [3, "Good"] : dbm >= -75 ? [2, "Fair"] : [1, "Weak"];
      rows.push(["mdi:wifi-strength-" + bars, "Wi-Fi", dbm === null ? word : `${word} (${dbm} dBm)`, dbm !== null && dbm < -75]);
    }
    if (b.link) {
      const on = ok(link) && link.state === "on";
      const n = ok(cl) ? Math.round(Number(cl.state)) : null;
      rows.push([on ? "mdi:lan-connect" : "mdi:lan-disconnect", "Panel link",
        !ok(link) ? "Unknown" : on ? (n && n > 1 ? `Connected (${n} clients)` : "Connected") : "Disconnected", !on]);
    }
    if (b.uptime) {
      const sec = ok(up) ? Number(up.state) : null;
      rows.push(["mdi:timer-outline", "Up for", sec === null ? "Unknown" : this._dur(sec), false]);
    }
    const bridgeUp = rows.length && !(b.wifi && !ok(wifi) && b.uptime && !ok(up));
    return `<h3>Serial bridge</h3>
      <div class="brows">${rows.map(([i, l, v, bad]) => `<div class="brow${bad ? " bad" : ""}"><ha-icon icon="${i}"></ha-icon>
        <span>${l}</span><b>${this._esc(v)}</b></div>`).join("")}</div>
      <div class="note">${this._esc(b.name || "ESP32 bridge")}${bridgeUp ? "" : " is offline"}</div>`;
  }

  // ------------------------------------------------------------- network devices
  // The display sensor's "devices" attribute: keypads, expanders and sounders on
  // the panel's network.
  _devices() {
    const st = this._hass && this._hass.states[this._config.entity];
    return (st && st.attributes.devices) || [];
  }

  _devicesHtml() {
    const icons = { panel: "mdi:shield-home-outline", keypad: "mdi:dialpad", expander: "mdi:access-point", sounder: "mdi:bullhorn-outline" };
    const n = (v, dp) => (v === null || v === undefined ? null : Number(v).toFixed(dp));
    const rows = this._devices().map((d) => {
      const vals = [];
      if (d.light !== null && d.light !== undefined) vals.push(`<span title="Light level"><ha-icon icon="mdi:brightness-6" style="--mdc-icon-size:16px"></ha-icon> ${d.light}%</span>`);
      if (n(d.voltage, 1)) vals.push(`<span title="Voltage">${n(d.voltage, 1)} V</span>`);
      if (n(d.battery, 1)) vals.push(`<span title="Battery"><ha-icon icon="mdi:battery" style="--mdc-icon-size:16px"></ha-icon> ${n(d.battery, 1)} V</span>`);
      if (n(d.current, 0)) vals.push(`<span title="Current">${n(d.current, 0)} mA</span>`);
      const e = [d.poll_errors ? `${d.poll_errors} poll` : "", d.crc_errors ? `${d.crc_errors} CRC` : ""].filter(Boolean);
      const errs = e.length ? ` · ${e.join(", ")} error${(d.poll_errors || 0) + (d.crc_errors || 0) > 1 ? "s" : ""}` : "";
      // Short names so a row fits a phone; the model only when it adds something.
      const name = d.kind === "sounder" ? String(d.name).replace("External sounder", "Sounder") : d.name;
      const model = d.model && !String(d.name).startsWith(d.model) ? `${this._esc(d.model)} · ` : "";
      return `<div class="drow"><ha-icon icon="${icons[d.kind] || "mdi:chip"}"></ha-icon>
        <b>${this._esc(name)}</b><span class="vals">${vals.join("")}</span>
        <small>${model}software ${this._esc(d.software)}${n(d.aux_current, 0) ? ` · aux ${n(d.aux_current, 0)} mA` : ""}${errs}</small></div>`;
    });
    return `<h3>System</h3><div class="brows">${rows.join("")}</div>`;
  }

  _dur(sec) {
    const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : m ? `${m} min` : `${Math.floor(sec)} s`;
  }

  // ------------------------------------------------------------- notifications
  // Settings live in the integration, per HA user (like the app's per phone).
  async _notifyCall(service, extra) {
    const st = this._hass.states[this._config.entity];
    const data = { ...(extra || {}) };
    if (st && st.attributes.panel_entry_id) data.config_entry_id = st.attributes.panel_entry_id;
    const res = await this._hass.callWS({ type: "call_service", domain: "orisec", service, service_data: data, return_response: true });
    return (res && res.response) || null;
  }

  async _loadNotify(userId) {
    if (this._config.notify === false) return null;
    try {
      this._nv = await this._notifyCall("get_notify_settings", userId ? { user_id: userId } : {});
      if (!userId) this._nmine = this._nv;
    } catch (err) { this._nv = null; }
    this._paintBell(); this._lastZones = null; this._render();
    return this._nv;
  }

  _nz() { const v = this._nmine; return v ? [v.settings.enabled, v.settings.zones, v.settings.categories] : null; }

  _paintBell() {
    const b = this.shadowRoot.querySelector(".bell");
    if (!b) return;
    const on = !!(this._nmine && this._nmine.settings.enabled);
    b.classList.toggle("on", on);
    b.querySelector("ha-icon").setAttribute("icon", on ? "mdi:bell-ring-outline" : "mdi:bell-outline");
  }

  _zoneNotifyBtn(z) {
    if (this._config.notify === false || !this._nmine) return "";
    const s = this._nmine.settings;
    const on = s.enabled && s.categories.includes("zones") && s.zones.includes(z.attributes.zone);
    return `<button class="zbtn nt ${on ? "on" : ""}" data-nzone="${z.attributes.zone}" title="Notify me when this zone is active">Notify</button>`;
  }

  async _zoneNotify(zone, btn) {
    const s = this._nmine && this._nmine.settings;
    if (!s) return;
    const on = s.enabled && s.categories.includes("zones") && s.zones.includes(zone);
    const zones = on ? s.zones.filter((x) => x !== zone) : [...new Set([...s.zones, zone])];
    const cats = s.categories.includes("zones") || on ? s.categories : [...s.categories, "zones"];
    btn.classList.add("busy");
    try {
      const enable = !on ? { enabled: true } : {};
      this._nmine = await this._notifyCall("set_notify_settings", { zones, categories: cats, ...enable, ...this._urlDefault() });
      if (!on && !this._nmine.phones.length) this._toast("No phone with the HA app is linked to your user, so nothing can be sent yet");
    } catch (err) { this._toast(String(err.message || err)); }
    this._paintBell(); this._lastZones = null; this._render();
  }

  _urlDefault() {
    // First time only: tapping a notification opens the dashboard it was set up from.
    return this._nmine && !this._nmine.settings.url ? { url: window.location.pathname } : {};
  }

  async _openNotify(userId) {
    const o = this._ovl();
    o.className = "ovl nset"; this._showOvl(o);
    o.innerHTML = `<h3>Alarm notifications</h3><div class="note">Loading…</div>`;
    const v = await this._loadNotify(userId);
    if (!v) { o.innerHTML = `<h3>Alarm notifications</h3><div class="err">Couldn't load the settings.</div><div class="row2"><button data-pinx="cancel">Close</button></div>`; return; }
    this._ndraft = JSON.parse(JSON.stringify(v.settings));
    this._paintNotify();
  }

  _paintNotify() {
    const v = this._nv, s = this._ndraft, o = this._ovl();
    const tg = (key, label, on, dis) => `<button class="${on ? "on" : ""}" data-ntg="${key}" ${dis ? "disabled" : ""}><span>${this._esc(label)}</span><i></i></button>`;
    const users = v.users && v.users.length > 1
      ? `<select data-nuser>${v.users.map((u) => `<option value="${u.user_id}" ${u.user_id === v.user_id ? "selected" : ""}>${this._esc(u.name)}${u.set_up ? "" : " (not set up)"}</option>`).join("")}</select>`
      : `<div class="note">${this._esc(v.user_name)}</div>`;
    const cats = Object.entries(v.categories).map(([k, l]) => tg(`c:${k}`, l, s.categories.includes(k), !s.enabled)).join("");
    const areas = v.areas.length > 1
      ? `<div class="sec">Areas</div><div class="tgl">${v.areas.map((a) => tg(`a:${a.area}`, a.name, !s.areas.length || s.areas.includes(a.area), !s.enabled)).join("")}</div>` : "";
    const phones = v.phones.length
      ? `<div class="sec">Send to</div><div class="tgl">${v.phones.map((ph) => tg(`d:${ph.id}`, ph.name, !s.devices.length || s.devices.includes(ph.id), !s.enabled)).join("")}</div>`
      : `<div class="note">No phone with the Home Assistant app is linked to this user yet, so nothing can be sent. Log in to the HA app on the phone as this user.</div>`;
    const nz = s.zones.length ? `${s.zones.length} zone${s.zones.length > 1 ? "s" : ""} chosen (Notify on the Zones tab)` : "Choose zones with Notify on the Zones tab";
    o.innerHTML = `<h3>Alarm notifications</h3>${users}
      <div class="tgl one">${tg("enabled", "Send me alarm notifications", s.enabled)}</div>
      <div class="sec">Notifications</div><div class="tgl">${cats}</div>
      <div class="note">${nz}</div>${areas}${phones}
      <div class="tgl one">${tg("critical", "Priority and Alarms as critical alerts (sound even on silent)", s.critical, !s.enabled)}</div>
      <div class="err"></div>
      <div class="row2"><button data-pinx="cancel">Cancel</button><button class="save" data-nsave>Save</button></div>`;
    const sel = o.querySelector("[data-nuser]");
    if (sel) sel.addEventListener("change", () => this._openNotify(sel.value));
  }

  _notifyToggle(key) {
    const s = this._ndraft, v = this._nv;
    const flip = (arr, x) => (arr.includes(x) ? arr.filter((y) => y !== x) : [...arr, x]);
    if (key === "enabled" || key === "critical") s[key] = !s[key];
    else if (key.startsWith("c:")) s.categories = flip(s.categories, key.slice(2));
    else if (key.startsWith("a:")) {
      const all = v.areas.map((a) => a.area);
      const cur = s.areas.length ? s.areas : all;
      const next = flip(cur, Number(key.slice(2)));
      s.areas = next.length === all.length ? [] : next;  // all ticked = every area (incl. future ones)
    } else if (key.startsWith("d:")) {
      const all = v.phones.map((p) => p.id);
      const cur = s.devices.length ? s.devices : all;
      const next = flip(cur, key.slice(2));
      s.devices = next.length === all.length ? [] : next;
    }
    this._paintNotify();
  }

  async _notifySave() {
    const s = this._ndraft, v = this._nv;
    const data = { user_id: v.user_id, enabled: s.enabled, categories: s.categories, areas: s.areas,
      zones: s.zones, devices: s.devices, critical: s.critical, url: s.url || window.location.pathname };
    try {
      const res = await this._notifyCall("set_notify_settings", data);
      if (this._nmine && res.user_id === this._nmine.user_id) this._nmine = res;
      this._closeOvl();
      this._toast(s.enabled ? `Notifications saved for ${v.user_name}` : `Notifications off for ${v.user_name}`);
    } catch (err) {
      const e = this._ovl().querySelector(".err");
      if (e) e.textContent = String(err.message || err);
    }
    this._paintBell(); this._lastZones = null; this._render();
  }

  // ------------------------------------------------------------- helpers
  _cls(st) {
    if (!st || st.state === "unavailable" || st.state === "unknown") return "c-down";
    if (st.state === "triggered") return "c-alarm";
    if (st.state === "arming") return "c-disarmed";   // app: still green while the exit timer runs
    if (st.state === "pending") return "c-busy";
    if (st.state === "armed_away") return "c-full";
    if (st.state.startsWith("armed_")) return "c-part";
    return "c-disarmed";
  }

  _activeIn(area, zones) {
    return zones.some((z) => z.state === "on" && (z.attributes.areas || []).includes(area));
  }

  _status(st, zones) {
    if (!st || st.state === "unavailable" || st.state === "unknown") return "Not responding";
    const a = st.attributes;
    switch (st.state) {
      case "triggered": return "Alarm Active";
      case "arming": return "Disarmed, Exit Timer Running";
      case "pending": return "Entry Timer Running";
      case "armed_away": return "Full Armed";
      case "disarmed": {
        let s = a.ready ? "Disarmed, Ready To Arm" : "Disarmed, Not Ready";
        if (this._activeIn(a.area, zones)) s += ", Zones Active";
        if (a.alarm_active) s += ", Alarm Active";
        if (a.engineer_required) s += ", Call Engineer to Reset";  // the app's wording
        return s;
      }
      default: return a.part_set ? `${a.part_set} Set` : "Part Armed";
    }
  }

  // A real alarm, live or latched after disarm. Bell/strobe alone also flash on a successful set.
  _needsReset(st) {
    return st.state === "triggered" || !!st.attributes.alarm_active;
  }

  _btn(cls, icon, label, act, small) {
    const lbl = small ? "" : `<span>${this._esc(label)}</span>`;
    return `<button class="act ${cls}${small ? " small" : ""}" data-act="${this._esc(act)}" title="${this._esc(label)}" aria-label="${this._esc(label)}">${icon}${lbl}</button>`;
  }

  // Buttons for a set of areas. Arm buttons act on the disarmed ones; Disarm on the set ones.
  _actions(areas, resets, small) {
    const disarmed = areas.filter((s) => s.state === "disarmed");
    const set = areas.filter((s) => s.state !== "disarmed" && s.state !== "unavailable" && s.state !== "unknown");
    const out = [];
    // As the app: while any area has an alarm latched, offer only Disarm and Reset.
    const latched = areas.some((s) => this._needsReset(s));
    if (disarmed.length && !latched) {
      out.push(this._btn("full", LOCK, "Full Arm", `svc|alarm_arm_away|${disarmed.map((s) => s.entity_id)}`, small));
      PARTS.forEach((p) => {
        const who = disarmed.filter((s) => (s.attributes.part_set_names || {})[p.attr]);
        if (who.length) {
          out.push(this._btn("part", LOCK, who[0].attributes.part_set_names[p.attr],
            `svc|${p.svc}|${who.map((s) => s.entity_id)}`, small));
        }
      });
    }
    if (set.length) out.push(this._btn("off", UNLOCK, "Disarm", `svc|alarm_disarm|${set.map((s) => s.entity_id)}`, small));
    // Quick Arm: repeat the set type in progress, which skips the exit timer.
    const quick = {};
    areas.filter((s) => s.state === "arming" && s.attributes.quick_arm)
      .forEach((s) => { (quick[s.attributes.quick_arm] = quick[s.attributes.quick_arm] || []).push(s.entity_id); });
    Object.entries(quick).forEach(([svc, ids]) => out.push(this._btn("quick", RUN, "Quick Arm", `svc|${svc}|${ids}`, small)));
    const rs = areas.filter((s) => this._needsReset(s) && resets[s.attributes.area]).map((s) => resets[s.attributes.area]);
    if (rs.length) out.push(this._btn("reset", RESET, "Reset", `button|press|${rs}`, small));
    return out.join("");
  }

  // ------------------------------------------------------------- render
  _render() {
    if (!this._hass || !this._built) return;
    const root = this.shadowRoot;
    const st = this._hass.states[this._config.entity];
    const down = !st || st.state === "unavailable" || st.state === "unknown";
    const d = this._discover();
    this._d = d;

    root.querySelector(".title").textContent = this._config.title || "Alarm";
    const bel = root.querySelector(".ovl .bridge");
    if (bel) { const h = this._bridgeHtml(); if (h !== this._bridgeLast) { bel.innerHTML = h; this._bridgeLast = h; } }
    const del = root.querySelector(".ovl .sysdev");
    if (del) { const h = this._devicesHtml(); if (h !== this._devLast) { del.innerHTML = h; this._devLast = h; } }
    this._renderBadge(d, down);
    if (this._view === "home") this._renderHome(d);
    if (this._view === "zones") this._renderZones(d);
    if (this._view === "activity") this._maybeReloadActivity(d);
    this._renderKeypad(st, down);
  }

  _renderBadge(d, down) {
    const badge = this.shadowRoot.querySelector(".badge");
    let label = "", cls = "";
    if (down) { label = "Not responding"; cls = "unavailable"; }
    else if (d.areas.some((s) => s.state === "triggered")) { label = "ALARM"; cls = "triggered"; }
    else if (d.areas.some((s) => s.attributes.alarm_active)) { label = "Alarm Active"; cls = "armed"; }
    else if (d.areas.length === 1) {
      const s = d.areas[0];
      label = { disarmed: "Disarmed", arming: "Exit", pending: "Entry", armed_away: "Full armed" }[s.state]
        || (s.attributes.part_set || "Part armed");
      cls = s.state === "armed_away" ? "armed" : s.state.startsWith("armed_") ? "part" : "";
    } else if (d.areas.length) {
      const n = d.areas.filter((s) => s.state !== "disarmed").length;
      label = n ? `${n} of ${d.areas.length} set` : "All disarmed";
      cls = n ? "armed" : "";
    }
    badge.textContent = label;
    badge.style.display = label ? "" : "none";
    badge.className = "badge " + cls;
  }

  _renderHome(d) {
    const zonesKey = d.zones.map((z) => z.state).join("");
    const key = JSON.stringify([d.areas.map((s) => [s.state, s.attributes]), zonesKey, this._open]);
    if (key === this._lastHome) return;
    this._lastHome = key;
    const el = this.shadowRoot.querySelector('[data-v="home"]');
    if (!d.areas.length) { el.innerHTML = `<div class="empty">No alarm areas found for this panel.</div>`; return; }

    if (d.areas.length === 1) {
      const s = d.areas[0];
      const big = { disarmed: "Disarmed", arming: "Exit Mode", pending: "Entry Mode", armed_away: "Full Armed", triggered: "ALARM" }[s.state]
        || (s.state.startsWith("armed_") ? (s.attributes.part_set || "Part Armed") : "Not responding");
      el.innerHTML = `<div class="solo"><div class="ring ${this._cls(s)}"><span>${this._esc(big)}</span></div>
        <div class="status${s.attributes.engineer_required ? " eng" : ""}">${this._esc(this._status(s, d.zones))}</div></div>
        <div class="acts">${this._actions(d.areas, d.resets)}</div>`;
      return;
    }

    const strip = d.areas.map((s) => `<div class="${this._cls(s)}"><span>${this._esc(s.attributes.friendly_name ? this._areaName(s) : "")}</span><div class="pill"></div></div>`).join("");
    const rows = d.areas.map((s) => {
      const a = s.attributes.area;
      const open = this._open[a] === true;  // as the app: rows start closed; the chevron opens them
      // As the app: orange outline while an engineer reset is needed; red while an alarm is latched.
      const alarm = this._needsReset(s) ? " alarm" : s.attributes.engineer_required ? " eng" : "";
      if (!open) {
        // As the app: one compact line (ring, name and status, chevron). Tap anywhere to open.
        return `<div class="area closed${alarm}" data-open="${a}" role="button" aria-label="Show buttons for ${this._esc(this._areaName(s))}">
            <div class="ring ${this._cls(s)}"></div>
            <div class="txt"><div class="name">${this._esc(this._areaName(s))}</div>
              <div class="status">${this._esc(this._status(s, d.zones))}</div></div>
            <div class="chev">\u2304</div></div>`;
      }
      return `<div class="area${alarm}"><div class="name">${this._esc(this._areaName(s))}</div>
          <div class="mid"><div class="ring ${this._cls(s)}"></div>
          <div class="inl">${this._actions([s], d.resets, true)}</div>
          <div class="chev" data-open="${a}" role="button" aria-label="Hide buttons">\u2303</div></div>
          <div class="status">${this._esc(this._status(s, d.zones))}</div></div>`;
    }).join("");
    el.innerHTML = `<div class="strip n${d.areas.length}">${strip}</div><div class="acts">${this._actions(d.areas, d.resets)}</div>${rows}`;
  }

  _areaName(s) {
    const fn = s.attributes.friendly_name || "";
    const dev = this._hass.devices && this._hass.entities && this._hass.entities[s.entity_id]
      && this._hass.devices[this._hass.entities[s.entity_id].device_id];
    const dn = dev && (dev.name_by_user || dev.name);
    return dn && fn.startsWith(dn + " ") ? fn.slice(dn.length + 1) : fn;
  }

  _zoneName(z) {
    const n = this._areaName(z);
    return `${z.attributes.zone}: ${n}`;
  }

  _zoneStatus(z) {
    if (z.state === "unavailable" || z.state === "unknown") return ["Not responding", "down"];
    const a = z.attributes;
    // Zone health first (as the app), with " – Omitted" added while omitted.
    const st = a.tamper ? ["Tamper", "tamper"] : a.alarmed ? ["Alarmed", "tamper"]
      : z.state === "on" ? ["Active", "active"] : ["OK", this._recent(z) ? "recent" : ""];
    // Wireless signal, as the app shows it: "OK (61%)", two-way devices "OK (73%, 84%)".
    if (a.signal != null) st[0] += ` (${a.signal}%${a.signal2 != null ? `, ${a.signal2}%` : ""})`;
    return a.omitted ? [`${st[0]} – Omitted`, st[1]] : st;
  }

  _renderZones(d) {
    const key = JSON.stringify([d.zones.map((z) => [z.state, z.attributes.tamper, z.attributes.omitted, z.attributes.alarmed, z.attributes.can_omit, z.attributes.since || z.last_changed, z.attributes.activations, z.attributes.signal, z.attributes.signal2]), [...this._hidden], d.areas.length, this._nz()]);
    if (key === this._lastZones) return;
    this._lastZones = key;
    const el = this.shadowRoot.querySelector('[data-v="zones"]');
    const filters = d.areas.length > 1
      ? `<div class="ftabs">${d.areas.map((s) =>
          `<button data-filter="${s.attributes.area}" class="${this._hidden.has(s.attributes.area) ? "" : "on"}" aria-pressed="${!this._hidden.has(s.attributes.area)}">${this._esc(this._areaName(s))}</button>`).join("")}</div>`
      : "";
    // Zones in any selected area (a zone can be in several).
    const zs = d.areas.length > 1
      ? d.zones.filter((z) => (z.attributes.areas || []).some((a) => !this._hidden.has(a)))
      : d.zones;
    el.innerHTML = filters + (zs.length ? zs.map((z) => {
      const [txt, cls] = this._zoneStatus(z);
      return `<div class="zone ${cls}" data-zone="${z.entity_id}"><ha-icon icon="${zoneIcon(z.attributes.zone_type)}"></ha-icon>
        <div class="zt"><div class="zn">${this._esc(this._zoneName(z))}</div><div class="zs">${txt}</div>
          <div class="when">${z.attributes.activations === 0 && z.state === "off"
            // Never active since the panel started: its timer counts from start-up, so "6d ago" would mislead.
            ? "No activity · Count 0"
            : `<span data-since="${this._since(z)}">${this._ago(this._since(z))}</span>${z.attributes.activations != null ? ` · Count ${z.attributes.activations}` : ""}`}</div></div>
        ${z.attributes.can_omit || z.attributes.omitted
          ? `<button class="zbtn ${z.attributes.omitted ? "on" : ""}" data-omit="${z.entity_id}">Omit</button>`
          : `<button class="zbtn ghost" tabindex="-1" aria-hidden="true" disabled>Omit</button>`}${this._zoneNotifyBtn(z)}</div>`;
    }).join("") : `<div class="empty">${d.areas.length > 1 ? "No zones in the selected areas." : "No zones."}</div>`);
  }

  // Cleared within the last `recent_minutes` (default 5): blue surround, as in the app.
  _recent(z) {
    const mins = this._config.recent_minutes !== undefined ? Number(this._config.recent_minutes) : 5;
    return Date.now() - new Date(this._since(z)).getTime() < mins * 60000;
  }

  // When the zone really last changed: the integration keeps this across HA restarts
  // (last_changed resets to the restart time).
  _since(z) {
    return z.attributes.since || z.last_changed;
  }

  _ago(iso) {
    const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  _refreshTimes() {
    if (!this._built) return;
    this.shadowRoot.querySelectorAll("[data-since]").forEach((el) => { el.textContent = this._ago(el.dataset.since); });
    this.shadowRoot.querySelectorAll("[data-mmss]").forEach((el) => { el.textContent = this._mmss(Number(el.dataset.mmss)); });
    if (this._view === "activity") this._renderActivity();
    this.shadowRoot.querySelectorAll(".zone.recent[data-zone]").forEach((el) => {
      const z = this._hass.states[el.dataset.zone];
      if (z && !this._recent(z)) el.classList.remove("recent");
    });
  }

  // ------------------------------------------------------------- activity
  _maybeReloadActivity(d) {
    const key = d.zones.map((z) => z.state + this._since(z)).join("|");
    if (key !== this._actKey) { this._actKey = key; this._loadActivity(); }
  }

  // ------------------------------------------------------------- event log
  // Opening the tab reads the newest 10; each further press of Log (or the
  // button at the bottom) adds the next 10 underneath, as the official app.
  async _loadLog(more) {
    if (this._logBusy) return;
    if (!more) { this._log = []; this._logEnd = false; }
    if (this._logEnd) { this._renderLog(); return; }
    this._logBusy = true;
    this._renderLog();
    const st = this._hass.states[this._config.entity];
    const data = { start: (this._log || []).length + 1, count: LOG_PAGE };
    if (st && st.attributes.panel_entry_id) data.config_entry_id = st.attributes.panel_entry_id;
    try {
      const res = await this._hass.callWS({ type: "call_service", domain: "orisec", service: "get_events",
        service_data: data, return_response: true });
      const evs = (res && res.response && res.response.events) || [];
      this._log = (this._log || []).concat(evs);
      if (evs.length < LOG_PAGE) this._logEnd = true;
      this._logErr = null;
    } catch (err) { this._logErr = String(err.message || err); }
    this._logBusy = false;
    this._renderLog();
  }

  _renderLog() {
    const el = this.shadowRoot.querySelector('[data-v="log"]');
    if (!el) return;
    // Loading shows on the Log tab itself too, so it's visible wherever you've scrolled.
    const tab = this.shadowRoot.querySelector('[data-tab="log"]');
    if (tab) { tab.textContent = this._logBusy ? "Log…" : TABS.log; tab.classList.toggle("busy", !!this._logBusy); }
    const log = this._log || [];
    let html = "", lastDate = null;
    log.forEach((e) => {
      const d = `${e.day} ${e.date}`.trim();
      if (d && d !== lastDate) { html += `<div class="ldate">${this._esc(d)}</div>`; lastDate = d; }
      const [, icon, color] = logIcon(e.text);
      const when = e.date ? `${e.day} ${e.date} - ${e.time}` : "";
      html += `<div class="ev"><ha-icon icon="${icon}" style="color:${color}"></ha-icon>
        <div class="et"><div class="en">${this._esc(e.text)}${e.area ? ` (${this._esc(e.area)})` : ""}</div>
        <div class="ew">${this._esc(when)}</div></div><div class="ei">${pad2(e.index)}</div></div>`;
    });
    if (this._logErr) html += `<div class="empty">Couldn't read the log: ${this._esc(this._logErr)}</div>`;
    else if (this._logBusy && !log.length) html += `<div class="empty">Reading the panel's log…</div>`;
    else if (!log.length) html += `<div class="empty">No events stored.</div>`;
    if (log.length && !this._logEnd)
      html += this._logBusy ? `<button class="more busy" disabled>Reading…</button>`
        : `<button class="more" data-more>Show ${LOG_PAGE} more</button>`;
    el.innerHTML = `<div class="ldate"><b>Event Log 1 .. ${log.length || LOG_PAGE}</b></div>` + html;
  }

  async _loadActivity() {
    const d = this._d || this._discover();
    if (!d.zones.length || this._loading) return;
    this._loading = true;
    const start = new Date(Date.now() - ACT_WINDOW_MIN * 60 * 1000).toISOString();
    try {
      const res = await this._hass.callWS({
        type: "history/history_during_period", start_time: start,
        entity_ids: d.zones.map((z) => z.entity_id),
        minimal_response: true, no_attributes: true, significant_changes_only: false,
      });
      const ev = [];
      Object.entries(res || {}).forEach(([eid, rows]) => {
        let prev = null;
        rows.forEach((r) => {
          const s = r.s !== undefined ? r.s : r.state;
          const t = (r.lu !== undefined ? r.lu * 1000 : new Date(r.last_updated || r.last_changed).getTime());
          if (s === "on" && prev !== "on" && t >= Date.now() - ACT_WINDOW_MIN * 60 * 1000) ev.push({ eid, t });
          prev = s;
        });
      });
      this._events = ev.sort((a, b) => b.t - a.t);
    } catch (err) {
      this._events = null;
      this._actErr = String(err.message || err);
    }
    this._loading = false;
    this._renderActivity();
  }

  _mmss(t) {
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    return `${pad2(Math.floor(s / 3600))}:${pad2(Math.floor(s / 60) % 60)}.${pad2(s % 60)}`;
  }

  _stamp(t) {
    const d = new Date(t);
    return `${pad2(d.getDate())}/${pad2(d.getMonth() + 1)}/${String(d.getFullYear()).slice(2)} ${pad2(d.getHours())}.${pad2(d.getMinutes())}.${pad2(d.getSeconds())}`;
  }

  _renderActivity() {
    const el = this.shadowRoot.querySelector('[data-v="activity"]');
    if (!el || this._view !== "activity") return;
    if (this._events === undefined) { el.innerHTML = `<div class="empty">Loading…</div>`; return; }
    if (this._events === null) { el.innerHTML = `<div class="empty">Couldn't load history: ${this._esc(this._actErr)}</div>`; return; }
    const now = Date.now();
    const buckets = ACT_BUCKETS.map((m) => [m, []]);
    this._events.forEach((e) => {
      const m = (now - e.t) / 60000;
      const b = buckets.find(([lim]) => m <= lim);
      if (b) b[1].push(e);
    });
    const any = buckets.some(([, l]) => l.length);
    if (!any) { el.innerHTML = `<div class="empty">No zone activity in the last ${ACT_WINDOW_MIN / 60} hours.</div>`; return; }
    el.innerHTML = buckets.filter(([, l]) => l.length).map(([lim, list]) => `<div class="group"><h4>Activity Within the Last ${actLabel(lim)}</h4>${
      list.map((e) => {
        const z = this._hass.states[e.eid];
        return `<div class="zone"><ha-icon icon="${zoneIcon(z && z.attributes.zone_type)}"></ha-icon>
          <div class="zt"><div class="zn">${this._esc(z ? this._zoneName(z) : e.eid)}</div>
          <div class="zs" data-mmss="${e.t}">${this._mmss(e.t)}</div><div class="zs">${this._stamp(e.t)}</div></div></div>`;
      }).join("")}</div>`).join("");
  }

  // ------------------------------------------------------------- keypad
  _renderKeypad(st, down) {
    const root = this.shadowRoot;
    root.querySelectorAll(".pad button[data-k]").forEach((b) => { b.disabled = down; });
    const lcd = root.querySelector(".lcd");
    lcd.classList.toggle("off", down);
    if (!st) { lcd.innerHTML = `<div class="msg">Entity ${this._esc(this._config.entity)} not found</div>`; this._last = null; return; }
    if (down) { lcd.innerHTML = `<div class="msg">Panel not responding</div>`; this._last = null; this._clkOff = null; return; }
    const layout = st.attributes.layout || [];
    if (this._holdBlank(layout, st)) return;
    this._clockSeen(layout, st);
    this._echoSeen(layout);
    // The clock is ticked locally, so its text doesn't count as a screen change.
    const key = JSON.stringify(layout, (k, v) => (v && v.clock ? { ...v, text: "" } : v)) + "|" + this._echoStars();
    if (key === this._last) return;
    this._last = key;
    const echo = this._echoLine(layout);
    lcd.innerHTML = layout.map((line, i) => {
      const top = `top:${i * 25}%`;
      if (line.progress) {
        const [, max, val] = line.progress;
        return `<div class="row" style="${top}"><div class="bar"><div style="width:${Math.min(100, (val / (max || 1)) * 100)}%"></div></div></div>`;
      }
      if (echo && echo.home && i >= 1) {
        // A digit on the home screen starts a code: show the code screen straight away.
        const txt = i === 1 ? "Enter Passcode:" : i === 2 ? "*".repeat(echo.stars) : "";
        return `<div class="row" style="${top}">${txt ? `<span class="seg center" style="left:50%">${txt}</span>` : ""}</div>`;
      }
      if (echo && echo.line === i) {
        // Code entry: show the asterisks typed but not yet echoed by the panel straight away.
        return `<div class="row" style="${top}"><span class="seg center" style="left:50%">${"*".repeat(echo.stars)}</span></div>`;
      }
      // A positioned segment, plus any that follow straight on from it (a style
      // change mid-text, e.g. the highlighted letter in the character picker).
      const groups = [];
      (line.segments || []).forEach((s) => {
        if (s.cont && groups.length) groups[groups.length - 1].parts.push(s);
        else groups.push({ first: s, parts: [s] });
      });
      const segs = groups.map(({ first, parts }) => {
        const inner = parts.map((s) => {
          if (s.icon && s.icon.startsWith("?")) return `<ha-icon class="st" icon="mdi:help-box-outline" title="Unknown status icon 0x${this._esc(s.icon.slice(1))}"></ha-icon>`;
          if (s.icon && s.icon.includes("|")) {
            // An icon the wall keypad animates between two pictures (e.g. envelope / SIM card).
            const [i1, i2] = s.icon.split("|");
            return `<span class="alt"><ha-icon class="st a1" icon="${this._esc(i1)}"></ha-icon><ha-icon class="st a2" icon="${this._esc(i2)}"></ha-icon></span>`;
          }
          if (s.icon) return `<ha-icon class="st" icon="${this._esc(s.icon)}"></ha-icon>`;
          const c = [s.inverted ? "inv" : "", s.size === 3 ? "small" : "", s.clock ? "clk" : ""].join(" ").trim();
          const text = s.clock ? (this._clockText() || s.text) : s.text;
          return `<span${c ? ` class="${c}"` : ""}>${this._esc(text)}</span>`;
        }).join("");
        return `<span class="seg ${first.align}" style="left:${(first.x / 128) * 100}%">${inner}</span>`;
      }).join("");
      return `<div class="row" style="${top}">${segs}</div>`;
    }).join("");
  }

  // ---- blank screen hold: the panel can send an empty screen for a moment while it
  // changes mode (e.g. logging off the engineer menu). The wall keypad never shows it,
  // so keep the last screen for up to BLANK_HOLD_MS; a screen still blank after that
  // is shown as it is.
  _holdBlank(layout, st) {
    const blank = !layout.some((ln) => ln.progress || (ln.segments || []).some((s) => s.icon || (s.text || "").trim()));
    if (!blank) { this._blankSince = null; clearTimeout(this._blankTimer); this._blankTimer = null; return false; }
    const lcd = this.shadowRoot.querySelector(".lcd");
    if (!lcd || !lcd.querySelector(".seg, .bar")) return false;  // nothing shown to hold
    const now = Date.now();
    if (this._blankSince == null) this._blankSince = now;
    const left = BLANK_HOLD_MS - (now - this._blankSince);
    if (left <= 0) return false;
    if (!this._blankTimer) {
      this._blankTimer = setTimeout(() => {
        this._blankTimer = null;
        const st2 = this._hass && this._hass.states[this._config.entity];
        if (st2 && st2.state !== "unavailable" && st2.state !== "unknown") this._renderKeypad(st2, false);
      }, left + 20);
    }
    return true;
  }

  // ---- local clock: estimate panel time minus phone time from each poll and tick it.
  // A reading is the panel's whole seconds, a little old by the time it arrives, so it
  // can only be behind the true time: follow the newest reading up at once, and only
  // come down when readings stay over a second lower (or jump: the clock was set).
  _clockSeen(layout, st) {
    let seg = null;
    layout.forEach((ln) => (ln.segments || []).forEach((s) => { if (s.clock) seg = s; }));
    if (!seg) { this._clkOff = null; return; }
    if (st.last_updated === this._clkStamp) return;
    this._clkStamp = st.last_updated;
    const m = /^(\d\d):(\d\d)\.(\d\d)$/.exec(seg.text || "");
    if (!m) return;
    // Time the reading from when HA stored it, so a stale state (e.g. the app's cached
    // one on reopening) still lands in the right place. Fall back to the phone's clock
    // if the two clocks disagree.
    const now = Date.now() / 1000, lu = Date.parse(st.last_updated) / 1000;
    const ref = lu > 0 && lu <= now + 5 && now - lu < 300 ? lu : now;
    const off = (+m[1] * 3600 + +m[2] * 60 + +m[3]) - ref;
    if (this._clkOff == null) { this._clkOff = off; return; }
    const d = ((((off - this._clkOff + 43200) % 86400) + 86400) % 86400) - 43200;
    if (d > 0 || d < -3) this._clkOff += d;
    else if (d < -1.2) this._clkOff += d + 1;
  }

  _clockText() {
    if (this._clkOff == null) return "";
    const t = ((Math.floor(Date.now() / 1000 + this._clkOff) % 86400) + 86400) % 86400;
    const p2 = (n) => String(n).padStart(2, "0");
    return `${p2(Math.floor(t / 3600))}:${p2(Math.floor(t / 60) % 60)}.${p2(t % 60)}`;
  }

  _tickClock() {
    if (this._view !== "keypad" || this._clkOff == null || !this.shadowRoot) return;
    const text = this._clockText();
    this.shadowRoot.querySelectorAll(".lcd .clk").forEach((el) => { if (el.textContent !== text) el.textContent = text; });
  }

  // ---- local echo: on the code entry screen, a digit shows its asterisk at once (the
  // wall keypad does the same); the panel's own asterisks take over as they arrive.
  _echoLine(layout) {
    const e = this._echo;
    if (e && e.home && e.pending) return { home: true, stars: e.pending };
    const i = layout.findIndex((ln) => /pass ?code|code:/i.test((ln.segments || []).map((s) => s.text).join("")));
    if (i < 0 || i + 1 >= layout.length) return null;
    const next = (layout[i + 1].segments || []).map((s) => s.text).join("").trim();
    if (next && !/^\*+$/.test(next)) return null;
    const stars = this._echoStars();
    return stars > next.length ? { line: i + 1, stars } : null;
  }

  _echoKey(k) {
    const e = this._echo || (this._echo = { base: 0, pending: 0, at: 0 });
    if (/^[0-9]$/.test(k) && this._echoable) {
      if (!e.pending || e.home) { e.base = this._panelStars; e.pending = e.home ? e.pending : 0; e.home = false; }
      e.pending += 1; e.at = Date.now();
    } else if (/^[0-9]$/.test(k) && (this._homeScreen || e.home)) {
      if (!e.home) { e.home = true; e.base = 0; e.pending = 0; }
      e.pending += 1; e.at = Date.now();
    } else {
      e.pending = 0;
    }
    this._last = null;
    this._renderKeypad(this._hass.states[this._config.entity], false);
  }

  _echoSeen(layout) {
    const i = layout.findIndex((ln) => /pass ?code|code:/i.test((ln.segments || []).map((s) => s.text).join("")));
    const next = i >= 0 && i + 1 < layout.length ? (layout[i + 1].segments || []).map((s) => s.text).join("").trim() : null;
    this._echoable = next !== null && (!next || /^\*+$/.test(next));
    this._panelStars = this._echoable ? next.length : 0;
    // The home screen is the one with the clock (menus don't show it).
    this._homeScreen = !this._echoable && layout.some((ln) => (ln.segments || []).some((s) => s.clock));
    const e = this._echo;
    if (!e || !e.pending) return;
    if (Date.now() - e.at > 3000) { e.pending = 0; e.home = false; return; }
    if (e.home) {
      if (this._homeScreen) return;  // the panel hasn't switched to the code screen yet
      if (!this._echoable) { e.pending = 0; e.home = false; return; }
      e.home = false; e.base = 0;  // now on the code screen: its asterisks count from zero
    }
    // Drop the echo off the code screen, or once the panel has caught up.
    if (!this._echoable || this._panelStars >= e.base + e.pending) { e.pending = 0; e.home = false; }
  }

  _echoStars() {
    const e = this._echo;
    return e && e.pending ? e.base + e.pending : 0;
  }

  _esc(t) {
    return String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
}

if (!customElements.get("orisec-keypad-card")) {
  customElements.define("orisec-keypad-card", OrisecKeypadCard);
}
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "orisec-keypad-card")) window.customCards.push({
  type: "orisec-keypad-card",
  name: "Orisec alarm",
  description: "Orisec alarm: areas, zones, activity and the full virtual keypad",
  preview: false,
});
