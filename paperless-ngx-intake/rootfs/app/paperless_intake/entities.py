"""The app's own numbers as Home Assistant entities: what each one is, and the discovery message.

The names and entity ids are fixed: Home Assistant takes an entity's id from the first
discovery message it sees, and a later rename would split its history in VictoriaMetrics.
Each value is read from one JSON state message (report.py builds it). Nothing here holds
document text: the lists name a scan's file or a document's id in Paperless, and the
app's own reason in plain words.
"""

from __future__ import annotations

from . import __version__

DEVICE_ID = "paperless_ngx_scanner"
DEVICE_NAME = "Paperless-ngx Scanner"
DISCOVERY_TOPIC = f"homeassistant/device/{DEVICE_ID}/config"
STATE_TOPIC = f"{DEVICE_ID}/state"
AVAILABILITY_TOPIC = f"{DEVICE_ID}/availability"
ONLINE, OFFLINE = "online", "offline"
SOURCE = "https://github.com/JaneJeon/homeassistant-apps/tree/main/paperless-ngx-intake"

IDLE, SCANNING, REREADING, WAITING = "Idle", "Scanning", "Re-reading", "Waiting"
STATUSES = [IDLE, SCANNING, REREADING, WAITING]

# key: (platform, name, entity id, options). The key is also the field in the state message.
ENTITIES: dict[str, tuple[str, str, str, dict]] = {
    "status": ("sensor", "Status", "sensor.paperless_ngx_scanner_status",
               {"device_class": "enum", "options": STATUSES, "attributes": True}),
    "paused": ("binary_sensor", "Paused", "binary_sensor.paperless_ngx_scanner_paused",
               {"device_class": "problem", "attributes": True}),
    "paperless_reachable": ("binary_sensor", "Paperless reachable",
                            "binary_sensor.paperless_ngx_scanner_paperless_reachable",
                            {"device_class": "connectivity"}),
    "scans_held": ("sensor", "Scans held", "sensor.paperless_ngx_scanner_scans_held",
                   {"unit_of_measurement": "scans", "state_class": "measurement", "attributes": True}),
    "scans_retrying": ("sensor", "Scans retrying", "sensor.paperless_ngx_scanner_scans_retrying",
                       {"unit_of_measurement": "scans", "state_class": "measurement"}),
    "scans_failed": ("sensor", "Scans failed", "sensor.paperless_ngx_scanner_scans_failed",
                     {"unit_of_measurement": "scans", "state_class": "measurement"}),
    "last_scan": ("sensor", "Last scan", "sensor.paperless_ngx_scanner_last_scan",
                  {"device_class": "timestamp"}),
    "google_pages_today": ("sensor", "Google pages today", "sensor.paperless_ngx_scanner_google_pages_today",
                           {"unit_of_measurement": "pages", "state_class": "total_increasing", "attributes": True}),
    "google_pages_this_month": ("sensor", "Google pages this month",
                                "sensor.paperless_ngx_scanner_google_pages_this_month",
                                {"unit_of_measurement": "pages", "state_class": "total_increasing"}),
    "stored_to_reread": ("sensor", "Stored scans to re-read", "sensor.paperless_ngx_scanner_stored_to_reread",
                         {"unit_of_measurement": "scans", "state_class": "measurement"}),
    "stored_set_aside": ("sensor", "Stored scans set aside", "sensor.paperless_ngx_scanner_stored_set_aside",
                         {"unit_of_measurement": "scans", "state_class": "measurement", "attributes": True}),
}


def discovery() -> dict:
    """One device discovery message for every entity. Sent retained, so Home Assistant has it after its own restart."""
    components = {}
    for key, (platform, name, entity_id, options) in ENTITIES.items():
        options = dict(options)
        component = {
            "p": platform,
            "name": name,
            "unique_id": f"{DEVICE_ID}_{key}",
            "default_entity_id": entity_id,
            # A JSON null renders as None, which Home Assistant shows as unknown.
            "value_template": "{{ value_json.%s }}" % key,
        }
        if platform == "binary_sensor":
            component["value_template"] = ("{{ 'None' if value_json.%s is none else ('ON' if value_json.%s else 'OFF') }}"
                                           % (key, key))
        if options.pop("attributes", False):
            component["json_attributes_topic"] = STATE_TOPIC
            component["json_attributes_template"] = "{{ value_json.%s_attributes | tojson }}" % key
        component.update(options)
        components[key] = component
    return {
        "dev": {"ids": [DEVICE_ID], "name": DEVICE_NAME, "mf": "JaneJeon/homeassistant-apps",
                "mdl": "Home Assistant app", "sw": __version__},
        "o": {"name": "paperless-ngx-scanner", "sw": __version__, "url": SOURCE},
        "cmps": components,
        "state_topic": STATE_TOPIC,
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_available": ONLINE,
        "payload_not_available": OFFLINE,
        "qos": 1,
    }
