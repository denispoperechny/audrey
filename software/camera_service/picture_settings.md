# Camera picture settings (for reference)

The settings the camera service used for picture tasks, the on-demand pictures the XIAO
ESP32-S3 Sense (OV3660 camera) took when asked. The task firmware is gone and the camera now
streams instead ([electronics/camera_stream_xiao](../../electronics/camera_stream_xiao/src/src.ino)),
but the task API and these defaults are still in the service (`PictureParams` in
[main.py](main.py), `GET /api/picture-defaults`).

## Defaults for tasks

| Setting | Default | Range | What it does, and why this value |
|---|---|---|---|
| `frame_size` | `FHD` (1920 × 1080) | QVGA … QXGA (2048 × 1536, the sensor's full 3 MP) | Picture size. Started at QXGA; FHD is about two thirds of the bytes, and uploads from the board are slow (see below) |
| `jpeg_quality` | `8` | 4–63, lower = better and bigger | High quality. In a dim, plain scene going to 12 shrank pictures only 10–20 % |
| `hmirror` | `true` | | Flip left-right, *relative to the sensor's power-on setting*: this board's pictures come out mirrored without it |
| `vflip` | `false` | | Flip upside down, also relative to the power-on setting |
| `settle_ms` | `1000` | 0–30000 | How long auto exposure and white balance run before the shot. Started at 3000; 0 vs 3000 made little difference to the exposure in tests, 1000 keeps a margin. At least 2 frames are always thrown away, since the first can be stale |
| `gain_ceiling` | `32X` | 2X … 128X | The most gain auto exposure may use: higher = brighter in the dark but grainier. 32X from the first dark-room tests |
| `night_mode` | `false` | | The sensor's AEC2: lets exposure run longer than a frame. Brighter in the dark but blurs anything moving; off by default |
| `ae_level` | `2` | −2 … +2 | Auto exposure bias, +2 = brighter |

Camera setup on the board: XCLK 20 MHz, JPEG, 2 frame buffers in PSRAM, `CAMERA_GRAB_LATEST`,
the camera switched on for each task and off again after (it stayed cool between tasks).

## The streaming firmware differs in

| | Streaming camera |
|---|---|
| Size | `HD` (1280 × 720) |
| JPEG quality | `12` |
| Settle | 1000 ms once at start; the camera stays on and keeps capturing |
| Rhythm | At most one picture a second; a frame more than 0.3 s old is dropped and the next one sent |

## Things learned along the way

- **Uploads from the board are slow and sometimes stall.** About 0.6 s for 25–35 KB, with 1 in 10
  over 1.5 s and stalls up to 5 s, against 0.08 s from a laptop to the same service. Most likely
  the prebuilt ESP32 Arduino core's small TCP send window (about 5.7 KB) and slow retransmission
  after a lost packet, not the WiFi signal (−30 to −47 dBm). Plain HTTP instead of HTTPS saved
  the TLS handshake on the board.
- **The course marks** (−25°, 0°, +25°) assume this lens: its focal length is 0.536 picture widths
  (horizontal field of view about 86°), measured against a wall clock's minute ticks; see
  [marks.py](marks.py). Frame size doesn't change it as long as the picture keeps the full width.
- **Clock:** pictures are only useful with a real time on them. The task firmware set its clock from
  the service (`utc_now` in every task answer); the streaming firmware uses internet time servers.
