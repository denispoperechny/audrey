## Autonomous vehicle project Audrey

The intent of this project is to explore capabilities of consumer/hobby grade modern technology and have fun.

### Chassis

- [Tug Boat](chassis/tug_boat/index.md)

### Electronics

- [Body Control Unit](electronics/body_control_unit_rpipico_mp/index.md) — driving the ESC and rudder servo; switches between RC and autonomous control, with failsafes
- [Main / Navigation Unit](electronics/main_controller_esp32_mp/index.md) — the autopilot: reads GPS and compass, takes a destination from the shore, decides throttle and rudder

### Missions

1. [Chassis testing](missions/1_chassis_testing/index.md) — first on-water run: water ingress and power consumption
2. [GPS precision testing](missions/2_gps_precision_testing/index.md) — GNSS precision, boat speed, time to fix
3. [Control test](missions/3_control_test/index.md) — dry-land check of the control chain: commands, RC takeover, telemetry
4. (Planned) [Waypoint navigation](missions/4_waypoint_navigation/index.md)
