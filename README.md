# Cleaning Observer

A passive, config-driven [AppDaemon](https://appdaemon.readthedocs.io/) app
for Home Assistant households running one or more robot vacuums/mops. It
**never sends a robot a command** - it only watches Home Assistant's own
vacuum entities (and, optionally, a robot's `rest980` mission endpoint) and
turns that into:

- a set of `sensor.<prefix>_*` entities per robot, with a live phase
  (`running` / `paused` / `returning` / `charging` / `docked_wait` / ...),
  progress estimate, and remaining-time estimate,
- a learned per-route duration/battery-drop history, so estimates get more
  accurate the more the robot actually runs a given set of rooms,
- a "settle guard" (`settled_for_new_job`) so a control layer knows the
  robot has genuinely finished housekeeping at the dock, not just that HA's
  vacuum entity flickered to `docked` for a moment,
- a small Custom-Clean "preview" builder (pick rooms on a map, see an
  estimated duration before you commit), and
- a rolling run history with outcomes (`Successful` / `Failed` /
  `Cancelled` / `completed_observed` / ...) for a dashboard history card.

It is deliberately **not** the thing that starts a cleaning job or decides
a job succeeded/failed at the household level - that's your own control
layer's job (your own scripts/automations). This app just observes and
remembers.

## Why "config-driven"?

This code has no built-in assumption about how many robots you have, what
they're called, or how your floors/rooms map to them. Every robot is
described entirely by one `robots:` block in `apps.yaml`. Two real
households' configs are shown below specifically because they're
*differently shaped* - one keys robots by **floor**, the other by **role**
- and neither shape needed a code change, only a different config.

### Example A - two robots, two floors, mixed platforms

One combo vacuum+mop robot upstairs (a Dreame, no `rest980`), one
vacuum-only robot downstairs (a Roomba on `rest980`):

```yaml
cleaning_observer:
  module: cleaning_observer
  class: CleaningObserver

  poll_interval_seconds: 15
  battery_reserve_percent: 12
  min_learning_seconds: 90

  job_active_entity: input_boolean.clean_job_active
  job_method_entity: input_text.clean_job_method
  job_rooms_entity: input_text.clean_job_rooms
  job_started_entity: input_datetime.clean_job_started
  custom_clean_method_entity: input_select.clean_preview_method

  sensor_prefix: sensor.baird_cleaning     # keeps this household's existing entity names
  friendly_name_prefix: "Baird Cleaning"   # keeps this household's existing dashboard labels

  robots:
    up:
      entity: vacuum.suck_it_up_2
      readiness_entity: sensor.suck_it_up_readiness
      result_entity: input_select.clean_job_up_result
      plan_room_ids_entity: input_text.clean_up_plan_room_ids
      plan_rooms_entity: input_text.clean_up_plan_rooms
      platform: dreame
      can_mop: true
      error_attribute: error
      native_progress_attribute: cleaning_progress
      icon: mdi:robot-vacuum-variant
      expected_selected_map: Upstairs
      expected_region_ids: [1, 2, 3, 4, 5, 6, 7, 8]
      room_names:
        "1": Dining room
        "2": Living Room
        # ...
      draft_rooms:
        "1": {name: Dining room, entity: input_boolean.clean_preview_up_dining_room}
        # ...

    down:
      entity: vacuum.suck_it_down
      readiness_entity: sensor.suck_it_down_readiness
      result_entity: input_select.clean_job_down_result
      plan_room_ids_entity: input_text.clean_down_plan_room_ids
      plan_rooms_entity: input_text.clean_down_plan_rooms
      platform: rest980
      can_mop: false
      error_attribute: error_msg
      icon: mdi:robot-vacuum
      rest980_state_url: http://homeassistant.local:3002/api/local/info/state
      expected_pmap_id: "..."
      expected_region_ids: [3, 13, 14, 15, 16, 17, 18, 19]
      room_names:
        "14": Entryway
        # ...
      draft_rooms:
        "14": {name: Entryway, entity: input_boolean.clean_preview_down_entryway}
        # ...
```

### Example B - two robots, one floor, same platform, split by role

One vacuum-only robot and one mop-only robot, sharing every room, both on
`rest980` (different ports):

```yaml
cleaning_observer:
  module: cleaning_observer
  class: CleaningObserver
  # ... same shared keys as above ...

  robots:
    vacuum:
      entity: vacuum.suck_stumpy
      platform: rest980
      can_mop: false
      error_attribute: error_msg
      rest980_state_url: http://homeassistant.local:3002/api/local/info/state
      # ...

    mop:
      entity: vacuum.mop_stumpy
      platform: rest980
      can_mop: true
      mop_confirm_attribute: pad_status
      error_attribute: error_msg
      rest980_state_url: http://homeassistant.local:3000/api/local/info/state
      # ...
```

Note that a household like Example B, where the two robots use
*independent* room-numbering for the same physical rooms, needs a
cross-robot room-ID translation table (e.g. "vacuum's room 9 == mop's room
23") - that mapping is a control-layer concern for your own
scripts/automations, not something this observer needs to know about. Each
robot's `room_ids` stay independent here on purpose.

## Config reference

| Key | Where | Default | Meaning |
|---|---|---|---|
| `poll_interval_seconds` | top-level | `15` | How often to re-check every robot's state. |
| `battery_reserve_percent` | top-level | `12` | Battery margin used by the recharge-likelihood estimate. |
| `min_learning_seconds` | top-level | `90` | A run must clean at least this long to count toward learning. |
| `new_job_settle_seconds` | top-level | `20` | How long a robot must sit `docked_wait` before `settled_for_new_job` goes true. |
| `sensor_prefix` | top-level | `sensor.cleaning` | Prefix for every published sensor's entity_id. |
| `friendly_name_prefix` | top-level | `Cleaning` | Prefix for every published sensor's `friendly_name` (cosmetic only). |
| `data_file` | top-level | `cleaning_observer_data.json` next to the module | Where run history/learning data persists. |
| `job_active_entity` / `job_method_entity` / `job_rooms_entity` / `job_started_entity` | top-level | `input_boolean.clean_job_active` etc. | Your control layer's shared "is a job running right now" helpers. |
| `robots.<name>.entity` | per robot | *(required)* | The `vacuum.*` entity. |
| `robots.<name>.platform` | per robot | inferred from `rest980_state_url` | `dreame` or `rest980` - which attribute/endpoint shape to read. |
| `robots.<name>.can_mop` | per robot | `false` | Whether this robot can mop at all. |
| `robots.<name>.error_attribute` | per robot | `error` | Which entity attribute carries the robot's own fault string. |
| `robots.<name>.no_fault_values` | per robot | a shared built-in set | Override the "this isn't really a fault" string set. |
| `robots.<name>.mop_confirm_attribute` | per robot | `cleaning_mode` | Attribute checked to confirm a mop pass actually happened. |
| `robots.<name>.native_progress_attribute` | per robot | *(none)* | If the robot exposes its own 0-100 progress attribute, name it here. |
| `robots.<name>.icon` | per robot | based on `can_mop` | Sensor icon. |
| `robots.<name>.rest980_state_url` | per robot | *(none)* | Full URL to a rest980-compatible mission-status endpoint. |
| `robots.<name>.expected_pmap_id` / `expected_user_pmapv_id` | per robot (rest980) | *(none)* | Map-drift check. |
| `robots.<name>.expected_selected_map` / `expected_region_ids` | per robot (dreame) | *(none)* | Map-drift check. |
| `robots.<name>.room_names` | per robot | `{}` | `{"<id>": "Friendly name"}` for this robot's rooms/segments. |
| `robots.<name>.draft_rooms` | per robot | `{}` | `{"<id>": {name, entity}}` for a Custom-Clean room-picker UI. |
| `robots.<name>.readiness_entity` / `result_entity` / `plan_room_ids_entity` / `plan_rooms_entity` | per robot | *(none)* | Your control layer's per-robot helpers, if you have them. |

## Installing via HACS

1. In HACS's own integration options, enable **"AppDaemon apps discovery &
   tracking"** - AppDaemon apps are hidden from the HACS UI by default.
2. **Before adding this repo**, confirm your AppDaemon add-on's `app_dir`
   points somewhere HACS can actually write to. HACS runs inside HA Core
   and has no filesystem access to an add-on's own private config folder
   (confirmed directly from the HACS maintainer, see
   [hacs/integration#4442](https://github.com/hacs/integration/issues/4442)):
   > "HACS have no access to `/addon_configs/<hash>_appdaemon` so it will
   > not be changed."

   HACS always deploys AppDaemon-category repos to
   `/config/appdaemon/apps/<app>/` (relative to HA Core's own `/config`).
   If your AppDaemon add-on has never used that path, add this to its own
   `appdaemon.yaml` and move your **existing** apps + `apps.yaml` there too
   (this is a global change affecting every app the add-on runs, not just
   this one - do it deliberately, and restart the add-on to confirm all
   your existing apps still load before adding this repo):

   ```yaml
   appdaemon:
     app_dir: /homeassistant/appdaemon/apps
   ```

   (`/homeassistant/` is how HA Core's `/config` is bind-mounted inside the
   AppDaemon add-on's own container.)
3. Add this repository to HACS as a custom repository (category:
   AppDaemon), install it, then add a `cleaning_observer:` block to your
   `apps.yaml` (see the examples above) and restart AppDaemon.

## What this app deliberately does NOT do

- It never calls a `vacuum.*` service. Starting/stopping/scheduling a
  clean, deciding a combined job "succeeded," sending notifications on
  fault, and any cross-robot room-ID translation are all control-layer
  concerns that live in your own scripts/automations, not here.
- It doesn't assume two robots, or any specific number - one robot, or
  five, works the same way; just add more entries under `robots:`.
