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
4. [Waypoint navigation #1](missions/4_waypoint_navigation_1/index.md) — first autonomous runs to three destinations: GPS against a reference, compass accuracy, rudder behaviour
5. [Waypoint navigation #2](missions/5_waypoint_navigation_2/index.md) — re-test after adjustments driven by Mission 4 (re-integrated compass and rudder trim)
6. [Waypoint navigation #3](missions/6_waypoint_navigation_3/index.md) — re-test after Mission 5's gyro calibration and trim changes: near-straight tracks

