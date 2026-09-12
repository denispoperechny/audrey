# Mission 1 — Chassis testing

First on-water run of the chassis — a 15 minute cruise at part throttle
in calm water, to check for water ingress and to get a first real-world
figure for power draw.

<img src="media/pic1.jpg" alt="Chassis on the water during the test run" width="400">

## Conditions

| | |
|---|---|
| Weather / water | Calm |
| Chassis weight | 1250 g |

## Run parameters

| | |
|---|---|
| Throttle position | 50% |
| Average speed (GPS) | ~1.0 m/s |
| Run time | 15 min |

## Battery

| | |
|---|---|
| Type | 2S, 800 mAh |
| Voltage — initial | 8.5 V |
| Voltage — final | 7.7 V |

## Observations

- **Ingress of water**: below registering threshold.

## Power consumption

- Battery capacity: 7.4V * 800mAh / 1000 = 5.92 Wh
- Battery remained: (7.7V - 7.1V) / (8.5V - 7.1V) = 43% (naively assumed linear discharge in 8.5~7.1 battery range)
- Distance traveled: 15 * 60s * 1m/s = 0.9km
- **Power consumption**:  5.92Wh * (1 - 0.43) / 0.9km = 3.75 Wh/km
