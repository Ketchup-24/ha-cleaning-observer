"""Cleaning Observer for AppDaemon.

A passive, config-driven observer for households running one or more robot
vacuums/mops through Home Assistant. It NEVER commands any robot - it only
watches. It:
- reads Home Assistant entity state,
- optionally GETs a robot's rest980 mission endpoint (any robot configured
  with a rest980_state_url, not just a hardcoded one),
- publishes sensor.<prefix>_* observer sensors, one set per configured
  robot, and
- learns per-route timing/history into a JSON data file beside this file,
  so a dashboard can show a realistic "X minutes remaining" instead of a
  guess, and so battery/recharge behavior gets more accurate over time.

Every robot is described entirely by apps.yaml's "robots:" mapping (see
CleaningObserver._robot_config) - there is no hardcoded assumption about
how many robots exist or what they're called. Two independent axes
describe each robot:
- "platform" (currently "dreame" or "rest980") - which integration/API
  shape it exposes, e.g. whether it has a rest980 mission endpoint at all.
- "can_mop" - whether it's mop-capable at all, independent of platform.

Two real households' configs (see this repo's README for both) show why
that separation matters: one runs two robots on two floors, one-of-each
platform (a Dreame combo unit and a Roomba on rest980); the other runs two
robots sharing one floor, both on rest980, split by role (vacuum vs. mop)
instead of by floor. Neither shape is hardcoded - both are just different
values under the same "robots:" key.
"""

from __future__ import annotations

import json
import os
import statistics
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import appdaemon.plugins.hass.hassapi as hass

VERSION = "1.0.0"
TERMINAL_RESULTS = {"Successful", "Failed", "Cancelled"}

# Each platform's own integration already translates its numeric fault code
# into a friendly string, on a different attribute name per platform:
# dreame_vacuum's "error" (see dreame/types.py DreameVacuumErrorCode -> the
# "no_error" translation string is "No error") and roomba_rest980's
# "error_msg" (see LegacyCompatibility.py's errorMappings - code 0 -> "n-a").
# Reading these directly means a stalled/interrupted run gets the ROBOT'S
# OWN explanation instead of the dashboard guessing from elapsed-vs-active
# time alone (see _refresh_robot below and dashboard-cleaning's Cleaning
# history card, which prefers this field when present).
#
# Phase 2 generalization: these are no longer keyed by robot NAME (that
# assumed exactly two robots called "up"/"down") - each robot now carries
# its own "error_attribute" / "no_fault_values" in apps.yaml's robots:
# block, defaulting to these module constants when not overridden. This is
# what lets an arbitrary-named robot (e.g. Hornby's "vacuum"/"mop") supply
# its own fault-attribute name instead of the code guessing from a name.
DEFAULT_ERROR_ATTRIBUTE = "error"
DEFAULT_NO_FAULT_VALUES = {
    "no error", "n-a", "none", "", "unknown",
    "unknown roomba error", "unknown ready status",
}


class CleaningObserver(hass.Hass):
    """Passive observer for an arbitrary set of cleaning robots."""

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        self.mode = "observe"  # invariant: never commands robots

        self.poll_interval = int(self.args.get("poll_interval_seconds", 15))
        self.completion_grace = int(self.args.get("completion_grace_seconds", 300))
        self.uncertain_completion_timeout = int(
            self.args.get("uncertain_completion_timeout_seconds", 1800)
        )
        self.max_runs = int(self.args.get("max_runs", 500))
        self.min_learning_seconds = int(self.args.get("min_learning_seconds", 90))
        self.http_timeout = float(self.args.get("rest980_timeout_seconds", 3.0))
        self.battery_reserve_percent = float(self.args.get("battery_reserve_percent", 12))
        # Ported from Stumpy: HA's generic "docked" state can arrive a beat
        # before the robot has actually finished internal housekeeping (bin
        # evac, mission commit). Require this many continuous seconds of
        # docked_wait before advertising the robot as genuinely ready for a
        # new job - advisory only, this observer never sends commands
        # either way, but the control-layer job tracker (Phase 1 step 5)
        # waits on this before flipping a job's result to Pending.
        self.new_job_settle_seconds = int(self.args.get("new_job_settle_seconds", 20))

        self.job_entities = {
            "active": self.args.get("job_active_entity", "input_boolean.clean_job_active"),
            "method": self.args.get("job_method_entity", "input_text.clean_job_method"),
            "rooms": self.args.get("job_rooms_entity", "input_text.clean_job_rooms"),
            "started": self.args.get("job_started_entity", "input_datetime.clean_job_started"),
        }
        self.custom_method_entity = self.args.get(
            "custom_clean_method_entity", "input_select.clean_preview_method"
        )

        # Phase 2 generalization: arbitrary robot count/names, config-driven.
        # Previously this was a hardcoded {"up": ..., "down": ...} literal -
        # now every robot (any name) is defined entirely under apps.yaml's
        # robots: mapping. Baird's own config keeps using the keys "up"/
        # "down", so every sensor name and iteration order below stays
        # byte-identical to before - the change is "stop hardcoding exactly
        # these two Python dict keys," not "rename Baird's keys."
        robots_raw = self.args.get("robots", {}) or {}
        self.robots: Dict[str, Dict[str, Any]] = {
            name: self._robot_config(name, cfg) for name, cfg in robots_raw.items()
        }
        if not self.robots:
            self.log("No robots configured under 'robots:' in apps.yaml - "
                     "observer has nothing to watch.", level="WARNING")

        # Custom-Clean preview rooms, per robot: id -> {name, entity}
        self.draft_rooms: Dict[str, Dict[str, Dict[str, str]]] = {}
        for robot, cfg in self.robots.items():
            self.draft_rooms[robot] = {}
            for room_id, dcfg in cfg.get("draft_rooms_raw", {}).items():
                if isinstance(dcfg, dict) and dcfg.get("entity"):
                    self.draft_rooms[robot][str(room_id)] = {
                        "entity": str(dcfg["entity"]),
                        "name": str(dcfg.get("name", f"Room {room_id}")),
                    }

        self.sensor_prefix = str(
            self.args.get("sensor_prefix", "sensor.cleaning")
        ).rstrip("_")
        self.sensor_ids = {
            k: f"{self.sensor_prefix}_{k}"
            for k in ("status", *self.robots.keys(), "progress", "preview",
                      "preview_order", "history", "diagnostics")
        }
        # Cosmetic-only: what "friendly_name" attributes are prefixed with.
        # An existing install can set this to keep its historical branding
        # (e.g. "Baird Cleaning") after adopting this generic package.
        self.friendly_name_prefix = str(
            self.args.get("friendly_name_prefix", "Cleaning")
        ).strip() or "Cleaning"

        self.data_file = str(
            self.args.get("data_file")
            or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "cleaning_observer_data.json")
        )
        self.data = self._load_data()
        self.active_runs: Dict[str, Dict[str, Any]] = self.data.setdefault("active_runs", {})
        self.preview_order: Dict[str, List[str]] = self.data.setdefault("preview_order", {})
        for robot in self.robots:
            self.preview_order.setdefault(robot, [])
            self.preview_order[robot] = [
                r for r in self.preview_order[robot] if r in self.draft_rooms[robot]
            ]

        self.rest980_cache: Dict[str, Dict[str, Any]] = {}
        self.rest980_status: Dict[str, Dict[str, Any]] = {}
        self.settled_since: Dict[str, datetime] = {}
        self.last_exception: Optional[str] = None

        # Listen only. No call_service / fire_event control paths anywhere.
        for robot, cfg in self.robots.items():
            self.listen_state(self._robot_changed, cfg["entity"], robot=robot)
            if cfg.get("result_entity"):
                self.listen_state(self._robot_changed, cfg["result_entity"], robot=robot)
        for entity in self.job_entities.values():
            if entity:
                self.listen_state(self._helper_changed, entity)
        if self.custom_method_entity:
            self.listen_state(self._helper_changed, self.custom_method_entity)
        for robot in self.robots:
            for room_id, rcfg in self.draft_rooms[robot].items():
                self.listen_state(self._draft_changed, rcfg["entity"],
                                  robot=robot, room_id=room_id)

        self.run_every(self._tick, "now", self.poll_interval)
        self.log("=" * 60)
        self.log(f"{self.friendly_name_prefix} Observer {VERSION} — OBSERVE ONLY")
        for robot, cfg in self.robots.items():
            suffix = f"  rest980={cfg['rest980_state_url']}" if cfg.get("rest980_state_url") else ""
            self.log(f"  {robot:<4}: {cfg['entity']}{suffix}")
        self.log(f"  data: {self.data_file}")
        self.log("=" * 60)

    def _robot_config(self, name: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize one robots.<name> apps.yaml block into internal config.

        Phase 2: no more Baird-specific hardcoded entity-name fallbacks here
        - every value comes from apps.yaml, matching the "shared, generic
        core" goal. Baird's own apps.yaml simply spells out every value
        explicitly (see apps.yaml's robots: block), so behavior is unchanged.
        """
        cfg = raw or {}
        can_mop = bool(cfg.get("can_mop", False))
        # platform is its own axis, separate from role/name (see the plan
        # notes in cleaning_history.yaml / this session's design doc): a
        # robot with a rest980_state_url is rest980-platform even if nothing
        # else says so explicitly, but an explicit "platform:" always wins.
        platform = cfg.get("platform") or ("rest980" if cfg.get("rest980_state_url") else "dreame")
        no_fault = cfg.get("no_fault_values")
        return {
            "entity": cfg.get("entity"),
            "readiness_entity": cfg.get("readiness_entity"),
            "result_entity": cfg.get("result_entity"),
            "plan_room_ids_entity": cfg.get("plan_room_ids_entity"),
            "plan_rooms_entity": cfg.get("plan_rooms_entity"),
            "rest980_state_url": cfg.get("rest980_state_url"),
            "expected_pmap_id": cfg.get("expected_pmap_id"),
            "expected_user_pmapv_id": cfg.get("expected_user_pmapv_id"),
            "expected_selected_map": cfg.get("expected_selected_map"),
            "expected_region_ids": [str(x) for x in (cfg.get("expected_region_ids") or [])],
            "can_mop": can_mop,
            "platform": platform,
            "error_attribute": cfg.get("error_attribute", DEFAULT_ERROR_ATTRIBUTE),
            "no_fault_values": (
                {str(v).strip().lower() for v in no_fault} if no_fault else None
            ),
            "mop_confirm_attribute": cfg.get("mop_confirm_attribute") or "cleaning_mode",
            "native_progress_attribute": cfg.get("native_progress_attribute"),
            "icon": cfg.get("icon") or (
                "mdi:robot-vacuum-variant" if can_mop else "mdi:robot-vacuum"),
            "room_names": {str(k): str(v) for k, v in (cfg.get("room_names") or {}).items()},
            "draft_rooms_raw": cfg.get("draft_rooms") or {},
        }

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load_data(self) -> Dict[str, Any]:
        base = {"schema_version": 1, "observer_version": VERSION, "runs": [],
                "active_runs": {}, "preview_order": {}}
        try:
            if not os.path.exists(self.data_file):
                return base
            with open(self.data_file, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("root not an object")
            for k, v in base.items():
                loaded.setdefault(k, v)
            loaded["observer_version"] = VERSION
            return loaded
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not load observer data: {exc}", level="WARNING")
            base["load_error"] = str(exc)
            return base

    def _save_data(self) -> None:
        self.data["observer_version"] = VERSION
        self.data["active_runs"] = self.active_runs
        self.data["preview_order"] = self.preview_order
        self.data["runs"] = self.data.get("runs", [])[-self.max_runs:]
        try:
            tmp = f"{self.data_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.data_file)
        except Exception as exc:  # noqa: BLE001
            self.last_exception = f"save_data: {exc}"
            self.log(self.last_exception, level="WARNING")

    # ------------------------------------------------------------------
    # callbacks
    # ------------------------------------------------------------------
    def _tick(self, **kwargs: Any) -> None:
        del kwargs
        try:
            now = self._utc_now()
            for robot, cfg in self.robots.items():
                if cfg.get("rest980_state_url"):
                    self._poll_rest980(robot, now)
            for robot in self.robots:
                self._refresh_robot(robot, now)
            self._publish_all(now)
            self._save_data()
        except Exception as exc:  # noqa: BLE001
            self.last_exception = f"tick: {type(exc).__name__}: {exc}"
            self.log(self.last_exception, level="ERROR")

    def _robot_changed(self, entity, attribute, old, new, kwargs):
        del entity, attribute, old, new
        now = self._utc_now()
        self._refresh_robot(kwargs["robot"], now)
        self._publish_all(now)
        self._save_data()

    def _helper_changed(self, *a, **k):
        self._publish_all(self._utc_now())

    def _draft_changed(self, entity, attribute, old, new, kwargs):
        del entity, attribute, old
        robot = kwargs.get("robot", "")
        room_id = str(kwargs.get("room_id", ""))
        state = str(new or "").strip().lower()
        if robot in self.preview_order and room_id:
            if state == "on" and room_id not in self.preview_order[robot]:
                self.preview_order[robot].append(room_id)
            elif state == "off":
                self.preview_order[robot] = [
                    r for r in self.preview_order[robot] if r != room_id
                ]
        self._publish_all(self._utc_now())
        self._save_data()

    # ------------------------------------------------------------------
    # rest980 (down only)
    # ------------------------------------------------------------------
    def _poll_rest980(self, robot: str, now: datetime) -> None:
        url = self.robots[robot].get("rest980_state_url")
        if not url:
            self.rest980_status[robot] = {"enabled": False, "ok": None,
                                          "last_poll": self._iso(now)}
            return
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": f"CleaningObserver/{VERSION}"}, method="GET")
            with urllib.request.urlopen(req, timeout=self.http_timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("rest980 response not an object")
            self.rest980_cache[robot] = payload
            self.rest980_status[robot] = {"enabled": True, "ok": True,
                                          "last_poll": self._iso(now), "error": None}
        except Exception as exc:  # noqa: BLE001
            self.rest980_status[robot] = {"enabled": True, "ok": False,
                                          "last_poll": self._iso(now), "error": str(exc)}

    def _mission_fields(self, robot: str) -> Dict[str, Any]:
        payload = self.rest980_cache.get(robot, {}) or {}
        mission = payload.get("cleanMissionStatus") or {}
        if not isinstance(mission, dict):
            mission = {}
        return {
            "cycle": mission.get("cycle"),
            "phase": mission.get("phase"),
            "error": mission.get("error"),
            "not_ready": mission.get("notReady"),
            "initiator": mission.get("initiator"),
            "mission_minutes": mission.get("mssnM"),
            "recharge_minutes": mission.get("rechrgM"),
            "battery": payload.get("batPct"),
        }

    def _is_rest980(self, robot: str) -> bool:
        return self.robots.get(robot, {}).get("platform") == "rest980"

    def _mission_or_empty(self, robot: str) -> Dict[str, Any]:
        """_mission_fields(robot) if this robot has rest980 mission data, else {}.

        Phase 2: this used to be inlined as `if robot == "down"` at every
        call site (four of them) - genuinely a PLATFORM check (rest980 vs.
        Dreame), not a role/name check, so it now branches on the robot's
        configured "platform" instead of its literal name. Centralized here
        so all four call sites can't drift from each other.
        """
        return self._mission_fields(robot) if self._is_rest980(robot) else {}

    def _map_validation(self, robot: str) -> Dict[str, Any]:
        cfg = self.robots[robot]
        checks: Dict[str, bool] = {}
        observed: Dict[str, Any] = {}

        if self._is_rest980(robot):
            payload = self.rest980_cache.get(robot, {}) or {}
            pmaps = payload.get("pmaps") or []
            live_pmap_id, live_ver = None, None
            if pmaps and isinstance(pmaps[0], dict):
                live_pmap_id = next(iter(pmaps[0].keys()), None)
                live_ver = next(iter(pmaps[0].values()), None)
            cmd = payload.get("lastCommand") or {}
            observed = {
                "live_pmap_id": live_pmap_id,
                "live_user_pmapv_id": live_ver,
                "last_command_pmap_id": cmd.get("pmap_id"),
                "last_command_user_pmapv_id": cmd.get("user_pmapv_id"),
            }
            exp_map = cfg.get("expected_pmap_id")
            exp_ver = cfg.get("expected_user_pmapv_id")
            checks["pmap_id"] = (not exp_map) or (live_pmap_id in (None, exp_map))
            checks["user_pmapv_id"] = (not exp_ver) or (live_ver in (None, exp_ver))
        else:  # dreame-style platform
            live_map = self._attr(cfg["entity"], "selected_map")
            room_ids = {str(r.get("id")) for r in
                        (self._attr(cfg["entity"], "rooms") or {}).get(live_map or "", [])}
            observed = {"selected_map": live_map, "room_ids": sorted(room_ids)}
            exp_map = cfg.get("expected_selected_map")
            exp_regions = set(cfg.get("expected_region_ids") or [])
            checks["selected_map"] = (not exp_map) or (live_map in (None, exp_map))
            checks["room_ids"] = (not exp_regions) or exp_regions.issubset(room_ids or exp_regions)

        return {
            "state": "ok" if all(checks.values()) else "attention",
            "checks": checks,
            "observed": observed,
            "expected": {
                "pmap_id": cfg.get("expected_pmap_id"),
                "user_pmapv_id": cfg.get("expected_user_pmapv_id"),
                "selected_map": cfg.get("expected_selected_map"),
                "region_ids": cfg.get("expected_region_ids"),
            },
        }

    # ------------------------------------------------------------------
    # run tracking
    # ------------------------------------------------------------------
    def _refresh_robot(self, robot: str, now: datetime) -> None:
        cfg = self.robots[robot]
        raw = self._state(cfg["entity"]) or "unknown"
        mission = self._mission_or_empty(robot)
        phase = self._classify_phase(robot, raw, mission)
        battery = self._battery_percent(cfg["entity"], mission)
        active = self.active_runs.get(robot)

        # Settle tracking runs regardless of whether a job is currently
        # active - a robot can be sitting docked with nothing in progress
        # and we still want to know how long it's been genuinely settled,
        # for the control-layer job tracker to consult before starting a
        # new job on it. Ported from Stumpy.
        if phase == "docked_wait":
            self.settled_since.setdefault(robot, now)
        else:
            self.settled_since.pop(robot, None)

        if active:
            self._backfill_room_context(robot, active)
            self._account_time(active, now)
            prev = active.get("phase", "unknown")
            # A docked wait that later returns to cleaning was actually a
            # recharge pause, not idle dock time - reclassify the time
            # already bucketed into docked_wait_seconds as charging_seconds
            # before resetting it. Ported from Stumpy (Hornby's sister
            # observer), which already got this right: previously Baird only
            # incremented recharge_count here, leaving the elapsed time
            # permanently misattributed to docked_wait_seconds - a bucket
            # _battery_prediction() never reads (it reads charging_seconds),
            # so every recharge cycle was silently invisible to the
            # typical_recharge_seconds learning below.
            if phase == "running" and prev == "docked_wait":
                dock_wait = float(active.get("docked_wait_seconds", 0.0))
                if dock_wait > 0:
                    active["charging_seconds"] = float(
                        active.get("charging_seconds", 0.0)) + dock_wait
                    active["docked_wait_seconds"] = 0.0
                active["recharge_count"] = int(active.get("recharge_count", 0)) + 1
                active["last_recharge_detected_at"] = self._iso(now)
            if phase == "charging" and prev != "charging":
                active["recharge_count"] = int(active.get("recharge_count", 0)) + 1
                active["last_recharge_detected_at"] = self._iso(now)
            active["phase"] = phase
            active["last_raw_state"] = raw
            active["last_battery"] = battery
            active["last_seen_at"] = self._iso(now)
            if phase == "running":
                active["seen_cleaning"] = True
                active["docked_since"] = None
            elif phase in ("docked_wait", "charging", "finishing_at_dock") and not active.get("docked_since"):
                active["docked_since"] = self._iso(now)

            result = self._state(cfg.get("result_entity"))
            if result:
                active["latest_ha_result"] = result

            # Snapshot the robot's own fault attribute. Kept as "the most
            # recent non-trivial value seen", not "the first" - if the robot
            # clears one fault and hits a different one before finalizing,
            # the later, more relevant one wins.
            no_fault = cfg.get("no_fault_values") or DEFAULT_NO_FAULT_VALUES
            err = self._attr(cfg["entity"], cfg.get("error_attribute", DEFAULT_ERROR_ATTRIBUTE))
            if err and str(err).strip().lower() not in no_fault:
                active["last_robot_error"] = str(err)

            # Did mopping actually happen this run, not just "was it in the
            # plan"? A preset's job_method (e.g. Essentials' "Vacuum & Mop")
            # only reflects the PLAN - Essentials always plans to mop Kitchen
            # regardless of whether the tank had water that day. Confirmed
            # live: a run with a dry tank the whole time still records
            # job_method "Vacuum & Mop", making a water-tank fault look
            # relevant to a run it never actually affected. Gated on the
            # robot's own can_mop config, not its name - a vacuum-only robot
            # never has a meaningful mop_confirm_attribute reading anyway.
            if cfg.get("can_mop"):
                cmode = str(self._attr(cfg["entity"], cfg["mop_confirm_attribute"]) or "").lower()
                no_active_fault = not err or str(err).strip().lower() in no_fault
                if "mop" in cmode and no_active_fault:
                    active["mop_confirmed"] = True

            if self._finalize_from_result(active, result, phase):
                self._finalize(robot, active, now, str(result).lower().replace(" ", "_"),
                               "ha_result")
                return
            if self._finalize_from_idle(active, phase, now):
                self._finalize(robot, active, now, "completed_observed", "idle_grace")
                return
            if self._finalize_uncertain(active, phase, now):
                self._finalize(robot, active, now, "completed_uncertain", "timeout")
                return
        elif phase == "running":
            self.active_runs[robot] = self._new_run(robot, now, raw, battery)
            self.log(f"Observer: {robot} cleaning started "
                     f"(battery={battery if battery is not None else '?'}%).")

    def _backfill_room_context(self, robot: str, active: Dict[str, Any]) -> None:
        """Fill in room/method context once the robot's own attributes catch up.

        A run is created the instant the vacuum entity flips to "cleaning",
        which can be a second or two before the Dreame's active_segments (or
        the Roomba's rest980 lastCommand) actually reflect the job. Re-check
        on every poll while the room list is still empty so every trigger
        path (map tap, preset button, or a manual start from the robot's own
        app) ends up with correct history/estimate context - no HA-side
        helper automation required.
        """
        if active.get("room_ids"):
            return
        cfg = self.robots[robot]
        room_names = cfg.get("room_names", {})
        if self._is_rest980(robot):
            cmd = (self.rest980_cache.get(robot, {}) or {}).get("lastCommand") or {}
            regs = cmd.get("regions") or []
            ids = [str(r.get("region_id")) for r in regs
                   if isinstance(r, dict) and r.get("region_id") is not None]
            if not ids:
                return
            names = ", ".join(room_names.get(i, f"Room {i}") for i in ids)
            method = "Vacuum"
        else:  # dreame-style platform
            segs = self._attr(cfg["entity"], "active_segments") or []
            ids = [str(s) for s in segs]
            if not ids:
                return
            names = ", ".join(room_names.get(i, f"Room {i}") for i in ids)
            cmode = str(self._attr(cfg["entity"], "cleaning_mode") or "").lower()
            method = "Vacuum & Mop" if "mop" in cmode and "sweep" in cmode else (
                "Mop" if "mop" in cmode else "Vacuum")
        active["room_ids"] = ids
        active["plan_rooms"] = names
        active["job_rooms"] = names
        active["job_method"] = method
        active["route_key"] = self._route_key(robot, ids, method, names)

    def _new_run(self, robot, now, raw, battery) -> Dict[str, Any]:
        cfg = self.robots[robot]
        route_ids = self._parse_ids(self._state(cfg.get("plan_room_ids_entity")))
        route_rooms = self._clean(self._state(cfg.get("plan_rooms_entity")))

        # rest980-platform robots: no HA helper carries the region list, but
        # the rest980 lastCommand does. Use it when the plan helper is empty.
        # (Previously this had its own inline duplicate of the room-names
        # dict already used by _backfill_room_context - now both read the
        # same single source of truth: cfg["room_names"].)
        if self._is_rest980(robot) and not route_ids:
            cmd = (self.rest980_cache.get(robot, {}) or {}).get("lastCommand") or {}
            regs = cmd.get("regions") or []
            ids = [str(r.get("region_id")) for r in regs
                   if isinstance(r, dict) and r.get("region_id") is not None]
            if ids:
                route_ids = ids
                room_names = cfg.get("room_names", {})
                route_rooms = route_rooms or ", ".join(room_names.get(i, f"Room {i}") for i in ids)

        method = self._clean(self._state(self.job_entities.get("method")))
        if not method and not cfg.get("can_mop"):
            method = "Vacuum"  # a vacuum-only robot has nothing else to call it
        job_rooms = route_rooms or self._clean(self._state(self.job_entities.get("rooms")))
        return {
            "run_id": f"{robot}-{now.strftime('%Y%m%dT%H%M%SZ')}",
            "robot": robot,
            "started_at": self._iso(now),
            "last_accounted_at": self._iso(now),
            "last_seen_at": self._iso(now),
            "phase": "running",
            "seen_cleaning": True,
            "start_battery": battery,
            "last_battery": battery,
            "cleaning_seconds": 0.0, "paused_seconds": 0.0, "charging_seconds": 0.0,
            "returning_seconds": 0.0, "docked_wait_seconds": 0.0, "other_seconds": 0.0,
            "recharge_count": 0,
            "docked_since": None,
            "job_active_at_start": self._state(self.job_entities.get("active")) == "on",
            "result_at_start": self._state(cfg.get("result_entity")),
            "latest_ha_result": self._state(cfg.get("result_entity")),
            "job_method": method,
            "job_rooms": job_rooms,
            "room_ids": route_ids,
            "plan_rooms": route_rooms,
            "route_key": self._route_key(robot, route_ids, method, route_rooms or job_rooms),
            "eligible_for_learning": False,
        }

    def _account_time(self, active, now) -> None:
        last = self._parse_iso(active.get("last_accounted_at"))
        if last is None:
            active["last_accounted_at"] = self._iso(now)
            return
        secs = max(0.0, (now - last).total_seconds())
        bucket = {"running": "cleaning_seconds", "paused": "paused_seconds",
                  "charging": "charging_seconds", "returning": "returning_seconds",
                  "docked_wait": "docked_wait_seconds",
                  # Same time bucket as docked_wait (dock-side, not actively
                  # cleaning) - "finishing_at_dock" is a more precise PHASE
                  # label for display, not a different kind of elapsed time.
                  "finishing_at_dock": "docked_wait_seconds"}.get(
            str(active.get("phase", "other")), "other_seconds")
        active[bucket] = float(active.get(bucket, 0.0)) + secs
        active["last_accounted_at"] = self._iso(now)

    def _finalize_from_result(self, active, result, phase) -> bool:
        if result not in TERMINAL_RESULTS:
            return False
        changed = result != active.get("result_at_start")
        job_active = self._state(self.job_entities.get("active")) == "on"
        if not (changed or job_active or active.get("job_active_at_start")):
            return False
        if result == "Successful" and not active.get("seen_cleaning"):
            return False
        return True

    def _finalize_from_idle(self, active, phase, now) -> bool:
        if phase != "docked_wait" or not active.get("seen_cleaning"):
            return False
        if self._state(self.job_entities.get("active")) == "on":
            return False
        ds = self._parse_iso(active.get("docked_since"))
        return ds is not None and (now - ds).total_seconds() >= self.completion_grace

    def _finalize_uncertain(self, active, phase, now) -> bool:
        if phase not in ("docked_wait", "charging"):
            return False
        ds = self._parse_iso(active.get("docked_since"))
        return ds is not None and (now - ds).total_seconds() >= self.uncertain_completion_timeout

    def _finalize(self, robot, active, now, outcome, source) -> None:
        self._account_time(active, now)
        active["ended_at"] = self._iso(now)
        active["outcome"] = outcome
        active["completion_source"] = source
        active["end_battery"] = self._battery_percent(
            self.robots[robot]["entity"], self._mission_or_empty(robot))
        active["battery_drop"] = self._drop(active.get("start_battery"),
                                            active.get("end_battery"))
        started = self._parse_iso(active.get("started_at"))
        active["elapsed_seconds"] = (now - started).total_seconds() if started else None
        active["eligible_for_learning"] = bool(
            outcome in ("successful", "completed_observed")
            and float(active.get("cleaning_seconds", 0.0)) >= self.min_learning_seconds
        )
        self.data.setdefault("runs", []).append(active.copy())
        self.data["runs"] = self.data["runs"][-self.max_runs:]
        self.active_runs.pop(robot, None)
        self.log(f"Observer: {robot} run finalized outcome={outcome} "
                 f"cleaning={round(float(active.get('cleaning_seconds', 0))/60, 1)}min")

    # ------------------------------------------------------------------
    # classification / estimates
    # ------------------------------------------------------------------
    def _classify_phase(self, robot, raw, mission) -> str:
        r = str(raw or "unknown").strip().lower()
        is_rest980 = self._is_rest980(robot)
        if is_rest980:
            cycle = str(mission.get("cycle") or "").strip().lower()
            phase = str(mission.get("phase") or "").strip().lower()
            if cycle == "evac" or phase == "evac":
                return "emptying_bin"
            if cycle == "dock":
                return "returning"
        if r == "cleaning":
            return "running"
        if r == "paused":
            return "paused"
        if r in ("returning", "returning_to_dock", "returning_paused"):
            return "returning"
        if r in ("washing", "drying"):
            return "servicing"
        if r == "error":
            return "error"
        if r in ("unknown", "unavailable"):
            return r
        if is_rest980:
            phase = str(mission.get("phase") or "").strip().lower()
            cycle = str(mission.get("cycle") or "").strip().lower()
            if phase == "run":
                return "running"
            if "charge" in phase and cycle == "clean":
                return "charging"
            if phase and ("dock" in phase or phase.startswith("hm")):
                return "returning"
            # rest980 exposes dock-side phases HA's vacuum entity collapses
            # into plain "docked" - keep "dockend" distinct so the dashboard
            # can explain why the robot isn't ready for a new job yet rather
            # than showing a plain "Docked" that looks fully idle. Ported
            # from Stumpy, including its own explicit uncertainty: we have
            # not proven exactly which firmware phase corresponds to Smart
            # Map persistence on this hardware - "finishing at dock" is a
            # safe, generic description of dock-side work still happening.
            if r in ("docked", "idle") and phase == "dockend":
                return "finishing_at_dock"
        if r in ("docked", "idle"):
            return "docked_wait"
        return r or "unknown"

    def _native_progress(self, robot: str) -> Optional[float]:
        """Read this robot's native progress attribute, if it has one.

        Phase 2: previously hardcoded to "only up exposes this" - now any
        robot can advertise a native_progress_attribute in its config
        (Baird's "up" sets it to "cleaning_progress", matching the Dreame's
        own attribute; a robot with no such attribute simply omits the key).
        """
        cfg = self.robots.get(robot, {})
        attr_name = cfg.get("native_progress_attribute")
        if not attr_name:
            return None
        val = self._attr(cfg["entity"], attr_name)
        try:
            p = float(val)
            if 0 <= p <= 100:
                return p
        except (TypeError, ValueError):
            pass
        return None

    def _estimate_for_active(self, active) -> Dict[str, Any]:
        robot = str(active.get("robot") or "")
        est = self._estimate_for_plan(
            robot, [str(x) for x in (active.get("room_ids") or [])],
            active.get("job_method"),
            active.get("plan_rooms") or active.get("job_rooms"))
        estimated = est.get("estimated_seconds")
        cleaned = float(active.get("cleaning_seconds", 0.0))

        native = self._native_progress(robot)
        if native is not None:
            est["progress_percent"] = round(native)
            if estimated:
                est["remaining_seconds"] = max(0.0, float(estimated) * (1 - native / 100.0))
            elif native > 0:
                est["remaining_seconds"] = max(0.0, cleaned * (100.0 - native) / native)
            else:
                est["remaining_seconds"] = None
            est["progress_source"] = "robot"
        elif estimated:
            est["progress_percent"] = round(min(99.0, max(0.0, cleaned / float(estimated) * 100.0)))
            est["remaining_seconds"] = max(0.0, float(estimated) - cleaned)
            est["progress_source"] = "estimate"
        else:
            est["progress_percent"] = None
            est["remaining_seconds"] = None
            est["progress_source"] = None

        cur_batt = self._battery_percent(
            self.robots[robot]["entity"], self._mission_or_empty(robot)
        ) if robot in self.robots else None
        est.update(self._battery_prediction(robot, est.get("remaining_seconds"), cur_batt))
        return est

    def _estimate_for_plan(self, robot, room_ids, method, room_text) -> Dict[str, Any]:
        eligible = [r for r in self.data.get("runs", [])
                    if r.get("robot") == robot and r.get("eligible_for_learning") is True
                    and float(r.get("cleaning_seconds", 0.0)) > 0]
        route_key = self._route_key(robot, room_ids, method, room_text)
        exact = [float(r["cleaning_seconds"]) for r in eligible
                 if route_key and r.get("route_key") == route_key]
        source, samples = None, []
        if exact:
            source, samples = "exact_route", exact
        elif room_ids:
            wanted = sorted(str(x) for x in room_ids)
            same = [float(r["cleaning_seconds"]) for r in eligible
                    if sorted(str(x) for x in (r.get("room_ids") or [])) == wanted]
            if same:
                source, samples = "same_rooms", same
            else:
                cnt = [float(r["cleaning_seconds"]) for r in eligible
                       if len(r.get("room_ids") or []) == len(room_ids) and room_ids]
                if cnt:
                    source, samples = "same_room_count", cnt
                else:
                    per = [float(r["cleaning_seconds"]) / len(r.get("room_ids") or [1])
                           for r in eligible if len(r.get("room_ids") or []) > 0]
                    if per:
                        source = "per_room_fallback"
                        samples = [statistics.median(per) * max(1, len(room_ids))]
        if not samples:
            return {"estimated_seconds": None, "sample_count": 0,
                    "confidence": "learning", "source": None}
        estimated = float(statistics.median(samples))
        n = len(samples)
        if source == "exact_route":
            conf = "high" if n >= 6 else "medium" if n >= 3 else "low"
        elif source == "same_rooms":
            conf = "medium" if n >= 3 else "low"
        elif source == "same_room_count":
            conf = "medium" if n >= 6 else "low"
        else:
            conf = "low"
        return {"estimated_seconds": estimated, "sample_count": n,
                "confidence": conf, "source": source}

    def _battery_prediction(self, robot, remaining_seconds, current_battery) -> Dict[str, Any]:
        rates, recharge = [], []
        for r in self.data.get("runs", []):
            if r.get("robot") != robot or r.get("eligible_for_learning") is not True:
                continue
            cs = float(r.get("cleaning_seconds", 0.0))
            bd = r.get("battery_drop")
            if cs > 0 and bd is not None and float(bd) > 0 and int(r.get("recharge_count", 0)) == 0:
                rates.append(float(bd) / (cs / 60.0))
            if int(r.get("recharge_count", 0)) > 0 and float(r.get("charging_seconds", 0.0)) > 0:
                recharge.append(float(r.get("charging_seconds", 0.0)))
        rate = statistics.median(rates) if rates else None
        typ_recharge = statistics.median(recharge) if recharge else None
        needed, likely = None, False
        if rate is not None and remaining_seconds is not None:
            needed = rate * (float(remaining_seconds) / 60.0)
            if current_battery is not None:
                likely = float(current_battery) - self.battery_reserve_percent < needed
        return {
            "battery_percent_per_cleaning_minute": round(rate, 3) if rate is not None else None,
            "estimated_battery_needed_percent": round(needed, 1) if needed is not None else None,
            "recharge_likely": bool(likely),
            "typical_recharge_seconds": typ_recharge,
        }

    def _draft_ids(self, robot: str) -> List[str]:
        selected = {rid for rid, c in self.draft_rooms[robot].items()
                    if (self._state(c.get("entity")) or "").lower() == "on"}
        ordered = [r for r in self.preview_order.get(robot, []) if r in selected]
        for rid in self.draft_rooms[robot]:
            if rid in selected and rid not in ordered:
                ordered.append(rid)
        if selected:
            self.preview_order[robot] = list(ordered)
        return ordered

    def _draft_names(self, robot, ids) -> List[str]:
        return [self.draft_rooms[robot].get(str(r), {}).get("name", f"Room {r}") for r in ids]

    # ------------------------------------------------------------------
    # publish
    # ------------------------------------------------------------------
    def _publish_all(self, now) -> None:
        self._publish_status(now)
        for robot in self.robots:
            self._publish_robot(robot, now)
        self._publish_progress(now)
        self._publish_preview(now)
        self._publish_history(now)
        self._publish_diagnostics(now)

    def _publish_status(self, now) -> None:
        missing = self._missing_entities()
        rest_err = [n for n, s in self.rest980_status.items()
                    if s.get("enabled") and s.get("ok") is False]
        state = "healthy" if not missing and not rest_err else "degraded"
        self.set_state(self.sensor_ids["status"], state=state, attributes={
            "friendly_name": f"{self.friendly_name_prefix} Observer", "icon":
            "mdi:eye-check-outline" if state == "healthy" else "mdi:eye-alert-outline",
            "version": VERSION, "mode": "observe", "robot_commands_enabled": False,
            "missing_entities": missing, "rest980_errors": rest_err,
            "last_exception": self.last_exception, "last_update": self._iso(now),
        })

    def _publish_robot(self, robot, now) -> None:
        cfg = self.robots[robot]
        raw = self._state(cfg["entity"]) or "unknown"
        mission = self._mission_or_empty(robot)
        phase = self._classify_phase(robot, raw, mission)
        active = self.active_runs.get(robot)
        est = self._estimate_for_active(active) if active else {}
        readiness_all = self._all(cfg.get("readiness_entity")) or {}
        display = {"running": "Running", "paused": "Paused", "returning": "Returning",
                   "charging": "Charging", "emptying_bin": "Emptying bin",
                   "servicing": "Servicing", "docked_wait": "Docked",
                   "finishing_at_dock": "Finishing at dock",
                   "error": "Error", "unavailable": "Unavailable",
                   "unknown": "Unknown"}.get(phase, phase.replace("_", " ").title())
        settled_at = self.settled_since.get(robot)
        settled_seconds = max(0.0, (now - settled_at).total_seconds()) if settled_at else 0.0
        settled_for_new_job = bool(
            phase == "docked_wait" and settled_seconds >= self.new_job_settle_seconds)
        attrs: Dict[str, Any] = {
            "friendly_name": f"{self.friendly_name_prefix} {robot.title()}",
            "icon": cfg["icon"],
            "entity": cfg["entity"], "raw_state": raw, "observer_phase": phase,
            "can_mop": cfg["can_mop"],
            "battery_percent": self._battery_percent(cfg["entity"], mission),
            "readiness": self._state(cfg.get("readiness_entity")),
            "readiness_reason": (readiness_all.get("attributes", {}) or {}).get("reason"),
            "ha_job_result": self._state(cfg.get("result_entity")),
            "job_method": self._state(self.job_entities.get("method")),
            "rest980_ok": self.rest980_status.get(robot, {}).get("ok"),
            "mission_phase": mission.get("phase"), "mission_cycle": mission.get("cycle"),
            "mission_minutes": mission.get("mission_minutes"),
            "native_progress_percent": self._native_progress(robot),
            "observed_run_active": bool(active),
            "settled_for_new_job": settled_for_new_job,
            "settled_since": self._iso(settled_at) if settled_at else None,
            "settled_seconds": round(settled_seconds, 1),
            "new_job_settle_seconds": self.new_job_settle_seconds,
            "last_update": self._iso(now),
        }
        if active:
            attrs.update({
                "run_id": active.get("run_id"),
                "started_at": active.get("started_at"),
                "room_ids": active.get("room_ids"),
                "plan_rooms": active.get("plan_rooms"),
                "job_method": active.get("job_method"),
                "elapsed_minutes": self._min(
                    (now - (self._parse_iso(active.get("started_at")) or now)).total_seconds()),
                "cleaning_minutes": self._min(active.get("cleaning_seconds")),
                "recharge_count": active.get("recharge_count", 0),
                "start_battery": active.get("start_battery"),
                "estimated_duration_minutes": self._min(est.get("estimated_seconds")),
                "estimated_remaining_minutes": self._min(est.get("remaining_seconds")),
                "estimated_progress_percent": est.get("progress_percent"),
                "progress_source": est.get("progress_source"),
                "recharge_likely": est.get("recharge_likely"),
                "estimate_confidence": est.get("confidence"),
                "estimate_sample_count": est.get("sample_count"),
                "estimate_source": est.get("source"),
            })
        self.set_state(self.sensor_ids[robot], state=display, attributes=attrs)

    def _publish_progress(self, now) -> None:
        items = [(robot, a, self._estimate_for_active(a))
                 for robot, a in self.active_runs.items()]
        if not items:
            self.set_state(self.sensor_ids["progress"], state="idle", attributes={
                "friendly_name": f"{self.friendly_name_prefix} Progress", "icon": "mdi:progress-clock",
                "active_robots": [], "last_update": self._iso(now)})
            return
        pcts = [it[2].get("progress_percent") for it in items
                if it[2].get("progress_percent") is not None]
        state = str(round(sum(pcts) / len(pcts))) if pcts else "learning"
        # Phase 2: was two hardcoded keys ("up_progress"/"down_progress") -
        # now one per configured robot. For Baird's own robots: {up, down}
        # this produces the exact same two attribute names as before.
        per_robot_progress = {robot: est.get("progress_percent") for robot, _, est in items}
        attrs = {
            "friendly_name": f"{self.friendly_name_prefix} Progress", "icon": "mdi:progress-clock",
            "active_robots": [it[0] for it in items],
            **{f"{robot}_progress": per_robot_progress.get(robot) for robot in self.robots},
            "last_update": self._iso(now),
        }
        if pcts:
            attrs["unit_of_measurement"] = "%"
        self.set_state(self.sensor_ids["progress"], state=state, attributes=attrs)

    def _publish_preview(self, now) -> None:
        method = self._clean(self._state(self.custom_method_entity)) or "Vacuum"
        all_ids: List[Dict[str, Any]] = []
        combined_route: List[str] = []
        combined_names: List[str] = []
        total_seconds = 0.0
        have_all = True
        confs, samples = [], 0
        recharge_any = False
        per_robot: Dict[str, Any] = {}
        for robot in self.robots:
            ids = self._draft_ids(robot)
            names = self._draft_names(robot, ids)
            if not ids:
                per_robot[robot] = {"room_ids": [], "estimated_minutes": None}
                continue
            m = method if self.robots[robot].get("can_mop") else "Vacuum"
            e = self._estimate_for_plan(robot, ids, m, ", ".join(names))
            cur_batt = self._battery_percent(
                self.robots[robot]["entity"], self._mission_or_empty(robot))
            e.update(self._battery_prediction(robot, e.get("estimated_seconds"), cur_batt))
            adj = e.get("estimated_seconds")
            if adj is not None and e.get("recharge_likely") and e.get("typical_recharge_seconds"):
                adj = float(adj) + float(e["typical_recharge_seconds"])
            per_robot[robot] = {
                "room_ids": ids, "room_names": names,
                "estimated_minutes": self._min(adj),
                "confidence": e.get("confidence"), "source": e.get("source"),
                "sample_count": e.get("sample_count"),
                "recharge_likely": e.get("recharge_likely"),
            }
            combined_route += [f"{robot}:{i}" for i in ids]
            combined_names += names
            confs.append(e.get("confidence", "learning"))
            samples += int(e.get("sample_count", 0))
            recharge_any = recharge_any or bool(e.get("recharge_likely"))
            if adj is None:
                have_all = False
            else:
                total_seconds += float(adj)
        selected = bool(combined_route)
        if not selected:
            confidence = "learning"
        elif "learning" in confs:
            confidence = "learning"
        elif "low" in confs:
            confidence = "low"
        elif "medium" in confs:
            confidence = "medium"
        else:
            confidence = "high"

        # Phase 2: was two hardcoded "up"/"down" keys - now one per
        # configured robot. For Baird's own robots: {up, down} this produces
        # exactly the same attribute names as before.
        self.set_state(self.sensor_ids["preview_order"],
                       state=json.dumps(combined_route, separators=(",", ":")),
                       attributes={"friendly_name": f"{self.friendly_name_prefix} Preview Route",
                                   "icon": "mdi:format-list-numbered",
                                   "route": combined_route, "room_names": combined_names,
                                   **{robot: per_robot.get(robot, {}).get("room_ids", [])
                                      for robot in self.robots},
                                   "last_update": self._iso(now)})
        self.set_state(self.sensor_ids["preview"],
                       state="ready" if selected else "none",
                       attributes={
            "friendly_name": f"{self.friendly_name_prefix} Preview", "icon": "mdi:clipboard-clock-outline",
            "method": method, "room_names": combined_names, "room_count": len(combined_route),
            "estimated_duration_minutes": self._min(total_seconds) if (selected and have_all) else None,
            "estimate_confidence": confidence, "estimate_sample_count": samples,
            **{f"{robot}_estimated_minutes": per_robot.get(robot, {}).get("estimated_minutes")
               for robot in self.robots},
            **{f"{robot}_rooms": per_robot.get(robot, {}).get("room_names", [])
               for robot in self.robots},
            "recharge_likely": recharge_any, "active_job": bool(self.active_runs),
            "last_update": self._iso(now)})

    def _publish_history(self, now) -> None:
        runs = self.data.get("runs", [])
        recent = [{
            "run_id": r.get("run_id"), "robot": r.get("robot"),
            "job_method": r.get("job_method"), "job_rooms": r.get("job_rooms"),
            "room_ids": r.get("room_ids"),
            "started_at": r.get("started_at"), "ended_at": r.get("ended_at"),
            "outcome": r.get("outcome"),
            "robot_error": r.get("last_robot_error"),
            "mop_confirmed": r.get("mop_confirmed", False),
            "cleaning_minutes": self._min(r.get("cleaning_seconds")),
            "total_minutes": self._min(r.get("elapsed_seconds")),
            "recharge_count": r.get("recharge_count", 0),
            "start_battery": r.get("start_battery"), "end_battery": r.get("end_battery"),
            "battery_drop": r.get("battery_drop"),
            "eligible_for_learning": r.get("eligible_for_learning", False),
        } for r in reversed(runs[-12:])]
        learning = sum(1 for r in runs if r.get("eligible_for_learning") is True)
        self.set_state(self.sensor_ids["history"], state=str(len(runs)), attributes={
            "friendly_name": f"{self.friendly_name_prefix} History", "icon": "mdi:history",
            "unit_of_measurement": "runs", "learning_samples": learning,
            "recent_runs": recent, "data_file": self.data_file,
            "last_update": self._iso(now)})

    def _publish_diagnostics(self, now) -> None:
        missing = self._missing_entities()
        writable = os.path.isdir(os.path.dirname(self.data_file) or ".") and \
            os.access(os.path.dirname(self.data_file) or ".", os.W_OK)
        maps = {robot: self._map_validation(robot) for robot in self.robots}
        map_drift = any(v.get("state") != "ok" for v in maps.values())
        self.set_state(self.sensor_ids["diagnostics"],
                       state="healthy" if (not missing and writable and not map_drift) else "attention",
                       attributes={
            "friendly_name": f"{self.friendly_name_prefix} Diagnostics", "icon": "mdi:stethoscope",
            "version": VERSION, "mode": "observe", "robot_commands_enabled": False,
            "poll_interval_seconds": self.poll_interval,
            "missing_entities": missing, "data_file": self.data_file,
            "data_file_writable": writable,
            "stored_runs": len(self.data.get("runs", [])),
            "learning_samples": sum(1 for r in self.data.get("runs", [])
                                    if r.get("eligible_for_learning") is True),
            "active_runs": list(self.active_runs.keys()),
            "map_drift": map_drift,
            "map_validation": maps,
            "rest980": {robot: {**self.rest980_status.get(robot, {}),
                                "map_validation": maps.get(robot)} for robot in self.robots},
            "draft_selected": {robot: self._draft_ids(robot) for robot in self.robots},
            "last_exception": self.last_exception, "last_update": self._iso(now)})

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _missing_entities(self) -> List[str]:
        return [self.robots[r]["entity"] for r in self.robots
                if self._all(self.robots[r]["entity"]) is None]

    def _state(self, e):
        if not e:
            return None
        try:
            v = self.get_state(e)
            return None if v is None else str(v)
        except Exception:  # noqa: BLE001
            return None

    def _all(self, e):
        if not e:
            return None
        try:
            v = self.get_state(e, attribute="all")
            return v if isinstance(v, dict) else None
        except Exception:  # noqa: BLE001
            return None

    def _attr(self, e, name):
        a = self._all(e) or {}
        return (a.get("attributes", {}) or {}).get(name)

    def _battery_percent(self, entity, mission) -> Optional[float]:
        a = (self._all(entity) or {}).get("attributes", {}) or {}
        for c in (a.get("battery_level"), a.get("battery"), a.get("battery_percent"),
                  a.get("batPct"), mission.get("battery")):
            try:
                if c is None or c == "":
                    continue
                v = float(str(c).replace("%", "").strip())
                if 0 <= v <= 100:
                    return round(v, 1)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _drop(a, b):
        try:
            return round(float(a) - float(b), 1) if a is not None and b is not None else None
        except (TypeError, ValueError):
            return None

    def _parse_ids(self, raw) -> List[str]:
        t = self._clean(raw)
        if not t or t.lower() in ("unknown", "unavailable", "none", "[]"):
            return []
        c = t.replace("[", "").replace("]", "").replace('"', "").replace("'", "")
        return [p.strip() for p in c.split(",") if p.strip()]

    @staticmethod
    def _route_key(robot, room_ids, method, room_text) -> str:
        if room_ids:
            return f"{robot}|ids|{','.join(str(x) for x in room_ids)}|{(method or '').lower()}"
        if method or room_text:
            return f"{robot}|text|{method or ''}|{room_text or ''}"
        return f"{robot}|unknown"

    @staticmethod
    def _clean(v):
        if v is None:
            return None
        t = str(v).strip()
        return None if t.lower() in ("", "unknown", "unavailable", "none") else t

    @staticmethod
    def _min(seconds):
        try:
            return round(float(seconds) / 60.0, 1) if seconds is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _utc_now():
        return datetime.now(timezone.utc)

    @staticmethod
    def _iso(v: datetime) -> str:
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_iso(v):
        if not v:
            return None
        try:
            t = str(v)
            if t.endswith("Z"):
                t = t[:-1] + "+00:00"
            p = datetime.fromisoformat(t)
            return p.replace(tzinfo=timezone.utc) if p.tzinfo is None else p.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None
