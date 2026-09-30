"""
Shared backend logic for the Hatch controller (Vercel Python functions).

Everything the API endpoints need: PIN auth, Hatch REST (login, list/save
alarms), and real-time night-light control via a one-shot presigned MQTT publish.

Alarms are built by CLONING one of the account's real alarms as a template and
overriding only the fields we change, so any Hatch fields we haven't reverse-
engineered are preserved exactly as the app wrote them.

Secrets come from environment variables (set in Vercel, never committed):
    HATCH_EMAIL, HATCH_PASSWORD, HATCH_PIN
Optional:
    FRONTEND_ORIGIN   (CORS allow-origin; defaults to "*")
"""

import copy
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

API = "https://data.hatchbaby.com/"

# Value scales (from the device firmware conventions).
MAX_IOT = 65535
CUSTOM_COLOR_ID = 9999
NO_COLOR_ID = 9998
NO_SOUND_ID = 19998
# Placeholder content ids that appear in every alarm we've inspected:
SUNRISE_SOUND_CID = "6SgX9tcTUsXBGReeE48v89"   # "no sound" during the sunrise ramp
ALARM_COLOR_CID = "3A2yfBSI1hx6m4XXLjExAO"     # "keep color" during the alarm step

DAY_KEYWORDS = {"once": 0, "weekdays": 62, "weekends": 65, "everyday": 127}


def from_pct(p):
    return int(round(max(0, min(100, p)) / 100.0 * MAX_IOT)) & 0xFFFF


def to_pct(v):
    return round((v & 0xFFFF) / float(MAX_IOT) * 100)


def from_hex(v):  # v is 0-255
    return int(round(max(0, min(255, v)) / 255.0 * MAX_IOT)) & 0xFFFF


# ---------- auth / config ----------

def _env(name):
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing environment variable: {name}")
    return val


def check_pin(provided):
    """Constant-time PIN comparison. A small delay slows brute force."""
    expected = os.environ.get("HATCH_PIN", "")
    ok = bool(provided) and hmac.compare_digest(str(provided), str(expected))
    if not ok:
        time.sleep(0.5)
    return ok


def frontend_origin():
    return os.environ.get("FRONTEND_ORIGIN", "*")


# ---------- Hatch REST ----------

class AuthExpired(Exception):
    """Raised when a cached token is rejected (401/403) so we re-login once."""


def _raise_if_auth(r):
    if r.status_code in (401, 403):
        raise AuthExpired(f"Hatch auth rejected (HTTP {r.status_code})")


def login():
    email = _env("HATCH_EMAIL").strip()
    password = _env("HATCH_PASSWORD")
    # Small back-off in case cold starts collide on the login rate limit.
    for attempt in range(3):
        r = requests.post(API + "public/v1/login",
                          json={"email": email, "password": password}, timeout=30)
        if r.status_code == 429 and attempt < 2:
            time.sleep(1.5 * (attempt + 1))
            continue
        break
    if r.status_code != 200:
        raise RuntimeError(f"Hatch login HTTP {r.status_code}. Check HATCH_EMAIL/HATCH_PASSWORD in Vercel.")
    data = r.json()
    if "token" not in data:
        raise RuntimeError(
            f"Hatch login returned no token "
            f"(status={data.get('status')!r}, message={data.get('message')!r}). "
            f"Check the HATCH_EMAIL/HATCH_PASSWORD values in Vercel — no quotes or trailing spaces.")
    return data["token"]


# ---- token/device cache (survives across requests on a warm Vercel instance) ----
_TOKEN_TTL = 6 * 3600          # re-login at most every 6h; Hatch tokens outlive this
_cache = {"token": None, "ts": 0.0, "device": None}


def get_token(force=False):
    """Return a cached Hatch token, logging in only when missing/expired/forced."""
    now = time.time()
    if not force and _cache["token"] and (now - _cache["ts"] < _TOKEN_TTL):
        return _cache["token"]
    _cache["token"] = login()
    _cache["ts"] = now
    _cache["device"] = None     # a fresh login invalidates the cached device too
    return _cache["token"]


def invalidate_auth():
    _cache["token"] = None
    _cache["device"] = None


def _auth_headers(token):
    return {"X-HatchBaby-Auth": token, "User-Agent": "hatch_rest_api"}


def get_device(token):
    r = requests.get(API + "service/app/iotDevice/v2/fetch",
                     headers=_auth_headers(token),
                     params={"iotProducts": ["restoreV5", "restoreV4", "restoreIot"]},
                     timeout=30)
    _raise_if_auth(r)
    r.raise_for_status()
    payload = r.json()["payload"]
    if not payload:
        raise RuntimeError("No Restore device found on this account.")
    d = payload[0]
    return {"mac": d["macAddress"], "thing": d["thingName"], "name": d.get("name")}


def get_device_cached(token):
    if not _cache["device"]:
        _cache["device"] = get_device(token)
    return _cache["device"]


def list_alarms(token, mac):
    r = requests.get(API + "service/app/routine/v3/fetch",
                     headers=_auth_headers(token),
                     params={"macAddress": mac, "types": ["alarm"]}, timeout=30)
    _raise_if_auth(r)
    r.raise_for_status()
    return r.json()["payload"]


def _confirm_data_version(token, mac, payload):
    if payload.get("confirmDataVersion") and payload.get("dataVersion"):
        c = requests.post(API + "service/app/v2/dataVersion",
                          headers=_auth_headers(token),
                          json={"dataVersion": payload["dataVersion"], "macAddress": mac,
                                "success": True, "returnAllRoutines": False}, timeout=30)
        if c.status_code != 200:
            raise RuntimeError(f"dataVersion HTTP {c.status_code}: {c.text[:500]}")


def _friendly_routine_error(message):
    """Turn Hatch's validation codes into something a person can act on."""
    msg = str(message or "")
    if "DontOverlapSteps" in msg or "overlap" in msg.lower():
        return ("This alarm overlaps another alarm on the same day. The Hatch can "
                "only run one alarm at a time, so pick a different time or turn off "
                "the alarm it conflicts with.")
    return f"Hatch rejected the alarm: {msg}"


def create_or_edit_routine(token, mac, routine):
    """Create (id=-1) or edit (real id) one alarm — the call the app uses."""
    r = requests.post(API + "service/app/routine/v2/createOrEdit",
                      headers=_auth_headers(token), json=routine, timeout=30)
    _raise_if_auth(r)
    if r.status_code != 200:
        raise RuntimeError(f"createOrEdit HTTP {r.status_code}: {r.text[:500]}")
    body = r.json()
    if body.get("status") not in (None, "success"):
        raise RuntimeError(_friendly_routine_error(body.get("message")))
    payload = body.get("payload") or {}
    _confirm_data_version(token, mac, payload)
    return payload


def bulk_save_routines(token, mac, routines):
    """Save several routines at once (used for delete via active=false)."""
    r = requests.post(API + "service/app/routine/v1/bulkCreateOrEdit",
                      headers=_auth_headers(token), json={"routines": routines}, timeout=30)
    _raise_if_auth(r)
    if r.status_code != 200:
        raise RuntimeError(f"bulkCreateOrEdit HTTP {r.status_code}: {r.text[:500]}")
    body = r.json()
    if body.get("status") not in (None, "success"):
        raise RuntimeError(_friendly_routine_error(body.get("message")))
    payload = body.get("payload") or {}
    _confirm_data_version(token, mac, payload)
    return payload


def edit_multiple(token, mac, mrds):
    """Minimal partial edit (the mechanism the official app/library use).

    `mrds` are small deltas ({id, active, enabled, ...}) with no steps, so there
    is nothing to malform — a reliable fallback for deleting/flagging alarms
    whose stored step shape we don't reproduce exactly.
    """
    r = requests.post(API + "service/app/routine/v2/editMultiple",
                      headers=_auth_headers(token),
                      json={"mrds": mrds, "type": "alarm"}, timeout=30)
    _raise_if_auth(r)
    if r.status_code != 200:
        raise RuntimeError(f"editMultiple HTTP {r.status_code}: {r.text[:500]}")
    body = r.json()
    if body.get("status") not in (None, "success"):
        raise RuntimeError(_friendly_routine_error(body.get("message")))
    payload = body.get("payload") or {}
    _confirm_data_version(token, mac, payload)
    return payload


# ---------- alarm shaping ----------

def _sunrise_step(alarm):
    for s in alarm.get("steps", []):
        if str(s.get("name", "")).lower() == "sunrise":
            return s
    return alarm["steps"][0]


def _alarm_step(alarm):
    for s in alarm.get("steps", []):
        if str(s.get("name", "")).lower() == "alarm":
            return s
    return alarm["steps"][-1]


def alarm_summary(alarm):
    """Compact view for the frontend (times, color, sound, days, enabled)."""
    sr, al = _sunrise_step(alarm), _alarm_step(alarm)
    lead = (sr["color"].get("duration") or 0) // 60
    return {
        "id": alarm.get("id"),
        "name": alarm.get("name"),
        "enabled": alarm.get("enabled"),
        "daysOfWeek": alarm.get("daysOfWeek", 0),
        "startTime": alarm.get("startTime"),
        "leadMinutes": lead,
        "color": {"hatchId": sr["color"].get("id"), "contentfulId": sr["color"].get("contentfulId")},
        "sound": {"hatchId": al["sound"].get("id"), "contentfulId": al["sound"].get("contentfulId")},
        "volume": to_pct(al["sound"].get("v", 0)),
    }


def _days_of_week(p):
    if "daysOfWeek" in p:
        return int(p["daysOfWeek"])
    if "days" in p:
        return DAY_KEYWORDS.get(str(p["days"]).lower(), 0)
    return 0


def build_alarm_routine(mac, params, existing=None):
    """Build the createOrEdit routine body (matches the Hatch app's request).

    New alarm -> id=-1 and step ids=0. Edit -> the real routine id and step ids.
    Two steps: a Sunrise light ramp (sunrise duration = leadMinutes) and the
    Alarm sound. Intensity is fixed at 100% per project spec.
    """
    lead = params.get("leadMinutes")
    lead = int(lead) if lead is not None else 20   # 0 is valid (no sunrise ramp)
    dur = lead * 60
    color = params["color"]
    sound = params["sound"]
    vol = int(params.get("volume", 50))

    sunrise_step_id, alarm_step_id = 0, 0
    if existing is not None:
        se, ae = _sunrise_step(existing), _alarm_step(existing)
        sunrise_step_id = se.get("id", 0)
        alarm_step_id = ae.get("id", 0)

    return {
        "id": existing["id"] if existing is not None else -1,
        "type": "alarm",
        "name": params.get("name", "Alarm"),
        "enabled": bool(params.get("enabled", False)),
        "active": True,
        "daysOfWeek": _days_of_week(params),
        "macAddress": mac,
        "startTime": params["startTime"],
        "displayOrder": (existing or {}).get("displayOrder", 0),
        "button0": False, "button1": False, "button2": False,
        "followBySleepScene": False,
        "steps": [
            {
                "name": "Sunrise", "type": "basic", "enabled": True,
                "respectColorUrl": False, "id": sunrise_step_id,
                "color": {
                    "id": color["hatchId"], "contentfulId": color["contentfulId"],
                    "r": 0, "g": 0, "b": 0, "w": 0, "i": MAX_IOT,
                    "duration": dur, "ignore": False, "until": "duration",
                },
                "sound": {
                    "id": NO_SOUND_ID, "contentfulId": SUNRISE_SOUND_CID, "mute": False,
                    "duration": dur, "v": MAX_IOT, "ignore": True, "until": "duration",
                },
            },
            {
                "name": "Alarm", "type": "basic", "enabled": True,
                "respectColorUrl": False, "id": alarm_step_id,
                # Paint the chosen color at ring time too, so it shows even when
                # there's no sunrise ramp (lead=0) to have set it during the ramp.
                "color": {
                    "id": color["hatchId"], "contentfulId": color["contentfulId"],
                    "r": 0, "g": 0, "b": 0, "w": 0, "i": MAX_IOT,
                    "duration": 3600, "ignore": False, "until": "duration",
                },
                "sound": {
                    "id": sound["hatchId"], "contentfulId": sound["contentfulId"],
                    "url": sound.get("url"), "mute": False, "duration": 3600,
                    "v": from_pct(vol), "ignore": False, "until": "duration",
                },
            },
        ],
    }


def routine_from_existing(existing, mac, active=True, enabled=None):
    """Return the fetched alarm verbatim with only active/enabled overridden.

    Used for enable/disable and delete (active=False). Sending the routine back
    exactly as Hatch stored it — instead of rebuilding its steps — is what the
    app does and is the only way older/oddly-shaped alarms deactivate reliably.
    """
    r = copy.deepcopy(existing)
    r["active"] = active
    if enabled is not None:
        r["enabled"] = bool(enabled)
    r.setdefault("type", "alarm")
    r["macAddress"] = mac  # createOrEdit/bulk require it; per-routine fetch omits it
    return r


def delete_delta(existing):
    """Minimal deactivation delta for editMultiple (no steps)."""
    return {
        "id": existing["id"], "name": existing.get("name"),
        "active": False, "enabled": False,
        "displayOrder": existing.get("displayOrder"),
        "startTime": existing.get("startTime"), "endTime": existing.get("endTime"),
    }


# ---------- real-time night-light (presigned MQTT) ----------

def _aws_bits(token):
    tr = requests.get(API + "service/app/restPlus/token/v1/fetch",
                      headers=_auth_headers(token), timeout=30)
    _raise_if_auth(tr)
    t = tr.json()["payload"]
    cog = requests.post(f"https://cognito-identity.{t['region']}.amazonaws.com",
                        headers={"content-type": "application/x-amz-json-1.1",
                                 "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity"},
                        data=json.dumps({"IdentityId": t["identityId"],
                                         "Logins": {"cognito-identity.amazonaws.com": t["token"]}}),
                        timeout=30).json()
    return t["endpoint"].replace("https://", ""), t["region"], cog["Credentials"]


def _sign(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _presign_iot_wss_path(host, region, creds):
    service, algorithm = "iotdevicegateway", "AWS4-HMAC-SHA256"
    now = datetime.now(timezone.utc)
    amz_date, datestamp = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    q = {"X-Amz-Algorithm": algorithm,
         "X-Amz-Credential": f"{creds['AccessKeyId']}/{scope}",
         "X-Amz-Date": amz_date, "X-Amz-SignedHeaders": "host"}
    canonical_qs = "&".join(f"{k}={quote(q[k], safe='')}" for k in sorted(q))
    canonical_request = (f"GET\n/mqtt\n{canonical_qs}\nhost:{host}\n\nhost\n"
                         f"{hashlib.sha256(b'').hexdigest()}")
    sts = (f"{algorithm}\n{amz_date}\n{scope}\n"
           f"{hashlib.sha256(canonical_request.encode()).hexdigest()}")
    k = _sign(_sign(_sign(_sign(("AWS4" + creds["SecretKey"]).encode(), datestamp),
                          region), service), "aws4_request")
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    canonical_qs += f"&X-Amz-Signature={sig}"
    canonical_qs += f"&X-Amz-Security-Token={quote(creds['SessionToken'], safe='')}"
    return f"/mqtt?{canonical_qs}"


def _mqtt_publish(thing, host, region, creds, desired):
    import paho.mqtt.client as mqtt
    path = _presign_iot_wss_path(host, region, creds)
    done = {"ok": False, "err": None}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"webctl-{int(time.time()*1000)}", transport="websockets")
    client.tls_set()
    client.ws_set_options(path=path)

    def on_connect(c, u, flags, rc, props):
        if rc != 0:
            done["err"] = f"connect rc={rc}"; c.disconnect(); return
        info = c.publish(f"$aws/things/{thing}/shadow/update",
                         json.dumps({"state": {"desired": desired}}), qos=1)
        info.wait_for_publish(timeout=10)
        done["ok"] = True; c.disconnect()

    client.on_connect = on_connect
    client.connect(host, 443, keepalive=30)
    client.loop_start()
    for _ in range(120):
        if done["ok"] or done["err"]:
            break
        time.sleep(0.1)
    client.loop_stop()
    if done["err"]:
        raise RuntimeError(f"MQTT publish failed: {done['err']}")


def nightlight_desired(on):
    # Light and sound are independent facets of the shadow's "current" object.
    # ON sets the color + playing:remote (turning on the light never disturbs a
    # playing sound). OFF clears ONLY the color — never playing:none — so the
    # sound keeps going if it's on.
    if not on:
        return {"current": {"color": {"id": NO_COLOR_ID, "r": 0, "g": 0, "b": 0, "w": 0}}}
    # Warm white ~2700K at 100% brightness.
    return {"current": {"srId": 0, "step": 0, "playing": "remote", "color": {
        "id": CUSTOM_COLOR_ID, "r": from_hex(255), "g": from_hex(169),
        "b": from_hex(87), "w": from_hex(0), "i": from_pct(100)}}}


def sound_desired(on, sound=None, volume=40):
    # ON: play the chosen sound indefinitely (device loops it) at the given
    # volume, without touching the color. OFF: clear ONLY the sound field so the
    # night-light stays on if it's on.
    if not on:
        return {"current": {"sound": {"id": NO_SOUND_ID, "mute": True}}}
    return {"current": {"playing": "remote", "sound": {
        "id": sound["hatchId"], "url": sound.get("url"), "mute": False,
        "v": from_pct(volume), "duration": 0, "until": "indefinite"}}}


def set_nightlight(on):
    def go(token, dev):
        host, region, creds = _aws_bits(token)
        _mqtt_publish(dev["thing"], host, region, creds, nightlight_desired(on))
        return {"ok": True, "on": on}
    return _with_auth(go)


def set_sound_now(on, sound=None, volume=40):
    if on and (not sound or not sound.get("hatchId") or not sound.get("url")):
        raise RuntimeError("A sound (with id and url) is required to start playback.")
    def go(token, dev):
        host, region, creds = _aws_bits(token)
        _mqtt_publish(dev["thing"], host, region, creds, sound_desired(on, sound, volume))
        return {"ok": True, "on": on}
    return _with_auth(go)


# ---------- high-level operations used by the API ----------

def _find(alarms, alarm_id):
    a = next((x for x in alarms if str(x.get("id")) == str(alarm_id)), None)
    if a is None:
        raise RuntimeError(f"Alarm {alarm_id} not found.")
    return a


def _active(alarms):
    # Alarms are removed by setting active=false; only surface active ones.
    return [a for a in alarms if a.get("active", True)]


def _summary_payload(dev_name, alarms):
    return {"device": dev_name, "alarms": [alarm_summary(a) for a in _active(alarms)]}


def _list_until(token, mac, predicate, tries=5, delay=1.0):
    """Re-fetch alarms until `predicate(active_alarms)` holds (or we run out).

    Hatch's createOrEdit/bulk endpoints are eventually consistent with the
    v3/fetch list, so an immediate fetch can miss a just-made change. Retrying
    with the same token (no extra login) lets the write propagate.
    """
    alarms = list_alarms(token, mac)
    for _ in range(tries - 1):
        if predicate(_active(alarms)):
            break
        time.sleep(delay)
        alarms = list_alarms(token, mac)
    return alarms


def _with_auth(fn):
    """Run fn(token, device) using the cached token; re-login once on 401/403.

    This is what keeps a burst of actions (e.g. deleting several alarms) from
    logging in repeatedly and tripping Hatch's login rate limit — one warm
    instance reuses a single token for up to _TOKEN_TTL.
    """
    for attempt in range(2):
        token = get_token(force=(attempt == 1))
        try:
            dev = get_device_cached(token)
            return fn(token, dev)
        except AuthExpired:
            invalidate_auth()
            if attempt == 1:
                raise
    raise RuntimeError("Authentication failed after retry.")


def op_list_alarms():
    return _with_auth(lambda token, dev:
                      _summary_payload(dev["name"], list_alarms(token, dev["mac"])))


def op_mutate_alarm(body):
    return _with_auth(lambda token, dev: _mutate(body, token, dev))


def _mutate(body, token, dev):
    action = body.get("action")
    mac = dev["mac"]

    if action == "create":
        alarm = body.get("alarm", {})
        routine = build_alarm_routine(mac, alarm)
        create_or_edit_routine(token, mac, routine)
        # New alarm has no id yet; wait until one with our name+startTime appears.
        name, start = routine["name"], routine["startTime"]
        alarms = _list_until(
            token, mac,
            lambda al: any(a.get("name") == name and a.get("startTime") == start for a in al))
    elif action == "update":
        existing = _find(list_alarms(token, mac), body["id"])
        routine = build_alarm_routine(mac, body.get("alarm", {}), existing=existing)
        create_or_edit_routine(token, mac, routine)
        aid, start = existing["id"], routine["startTime"]
        alarms = _list_until(
            token, mac,
            lambda al: any(str(a.get("id")) == str(aid) and a.get("startTime") == start for a in al))
    elif action == "setEnabled":
        want = bool(body.get("enabled"))
        existing = _find(list_alarms(token, mac), body["id"])
        routine = routine_from_existing(existing, mac, enabled=want)
        create_or_edit_routine(token, mac, routine)
        aid = existing["id"]
        alarms = _list_until(
            token, mac,
            lambda al: any(str(a.get("id")) == str(aid) and a.get("enabled") == want for a in al))
    elif action == "delete":
        aid = body["id"]
        existing = _find(list_alarms(token, mac), aid)
        gone = lambda al: not any(str(a.get("id")) == str(aid) for a in al)
        # Primary: hand the alarm back verbatim with active=false (what the app does).
        bulk_save_routines(token, mac, [routine_from_existing(existing, mac, active=False)])
        alarms = _list_until(token, mac, gone)
        # Fallback: minimal editMultiple delta if the alarm is somehow still active.
        if not gone(_active(alarms)):
            edit_multiple(token, mac, [delete_delta(existing)])
            alarms = _list_until(token, mac, gone)
    else:
        raise RuntimeError(f"Unknown action: {action!r}")

    return _summary_payload(dev["name"], alarms)
