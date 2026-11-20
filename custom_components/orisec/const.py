"""Constants for the Orisec integration."""

DOMAIN = "orisec"
MANUFACTURER = "Orisec"

CONF_POLL_INTERVAL = "poll_interval"
# Poll interval limits per link. USB and the serial bridge ran 7 hours at 0.2 s with
# one missed reply in 31,000 polls; the CM2 (UDP) is untested below 1 s.
MIN_POLL_SECONDS = 0.2  # USB / serial bridge
MIN_POLL_SECONDS_UDP = 1.0  # CM2
DEFAULT_POLL_SECONDS = 1  # USB / serial bridge
DEFAULT_POLL_SECONDS_UDP = 2  # CM2


def poll_limits(transport: str | None) -> tuple[float, float]:
    """(minimum, default) poll interval in seconds for a link ("udp" = CM2, the default)."""
    if transport in ("serial", "tcp"):
        return MIN_POLL_SECONDS, DEFAULT_POLL_SECONDS
    return MIN_POLL_SECONDS_UDP, DEFAULT_POLL_SECONDS_UDP


def poll_seconds(transport: str | None, saved: float | None) -> float:
    """The interval to poll at: the saved option, or the link's default, never below its minimum."""
    low, default = poll_limits(transport)
    return max(low, float(saved if saved is not None else default))
REPLY_TIMEOUT = 2.5  # seconds to wait for the first reply datagram
REPLY_SETTLE = 0.15  # extra time to collect further datagrams of the same reply
UNAVAILABLE_AFTER_SECONDS = 10  # silence before entities go unavailable (UDP blips are normal)
ZONE_NAME_BATCH = 15
EVENT_ORISEC = "orisec_event"  # bus event for each new log entry / zone going active
NOTIFY_STORE = "orisec_notify"  # per-user notification settings
CONF_CLOCK_SYNC = "clock_sync"  # keep the panel clock in step with HA
CONF_CLOCK_TOLERANCE = "clock_tolerance"  # seconds of drift before an automatic sync
DEFAULT_CLOCK_SYNC = True
DEFAULT_CLOCK_TOLERANCE = 5
CLOCK_SYNC_MIN_GAP = 3600  # at most one automatic sync an hour

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
