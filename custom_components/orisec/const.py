"""Constants for the Orisec integration."""

DOMAIN = "orisec"
MANUFACTURER = "Orisec"

CONF_POLL_INTERVAL = "poll_interval"
DEFAULT_POLL_SECONDS = 2
REPLY_TIMEOUT = 2.5  # seconds to wait for the first reply datagram
REPLY_SETTLE = 0.15  # extra time to collect further datagrams of the same reply
UNAVAILABLE_AFTER_SECONDS = 10  # silence before entities go unavailable (UDP blips are normal)
ZONE_NAME_BATCH = 15
EVENT_EVERY = 2  # check the panel log for new entries every N polls
EVENT_ORISEC = "orisec_event"  # bus event for each new log entry / zone going active
NOTIFY_STORE = "orisec_notify"  # per-user notification settings
CONF_CLOCK_SYNC = "clock_sync"  # keep the panel clock in step with HA
CONF_CLOCK_TOLERANCE = "clock_tolerance"  # seconds of drift before an automatic sync
DEFAULT_CLOCK_SYNC = True
DEFAULT_CLOCK_TOLERANCE = 5
CLOCK_SYNC_MIN_GAP = 3600  # at most one automatic sync an hour
FLAG_EVERY = 30  # read the per-zone 1150 setting every N polls (it rarely changes)

SERVICE_SEND_KEY = "send_key"
SERVICE_OMIT_ZONE = "omit_zone"
ATTR_OMIT = "omit"
ATTR_KEY = "key"
ATTR_KEYS = "keys"

CARD_URL = "/orisec_files/orisec-keypad-card.js"

CONF_TRANSPORT = "transport"
CONF_DEVICE = "device"
CONF_BAUD = "baud"
DEFAULT_BAUD = 115200
BAUD_RATES = [9600, 19200, 38400, 57600, 115200]
