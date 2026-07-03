# Local OpenNeato Changes

Date: 2026-07-03

## Scope

These changes keep the ESP32-C3 as an OpenNeato add-on/controller, not a replacement for the robot firmware. The robot still owns autonomous navigation and docking. The C3 adds web UI, manual control tuning, history/map capture, saved map viewing, and Home Assistant-compatible OpenNeato behavior.

## Manual Drive

- Reworked manual UI from one joystick into two axis controls:
  - Left vertical joystick controls forward/reverse.
  - Right horizontal joystick controls turning.
  - Center button controls all cleaning motors together.
- Removed individual Vacuum, Brush, and Side Brush buttons from the manual page.
- Removed manual Dock button because TestMode/manual docking only seeks charger contacts and does not reverse a known path home.
- Added axis locking to the joystick component with `vertical`, `horizontal`, and `both` modes.
- Added command single-flight behavior for manual wheel commands:
  - Only one `/api/manual/move` request is in flight.
  - Newer joystick values replace pending older values.
  - This avoids building a stale command backlog when WiFi or serial is slow.
- Added acceleration ramping in the frontend:
  - Ramp tick: 50 ms.
  - Ramp rate: 0.8 joystick units per second.
  - Releasing the joystick or pressing Stop still sends zero immediately.
- Slowed manual keepalive from 400 ms to 1500 ms to reduce command pressure.
- Manual speed/turn now come from settings instead of fixed constants.

## Manual Speed, Turn, And Stall Safety

- Added persistent manual drive settings:
  - `manualSpeed`: max wheel speed in mm/s.
  - `manualTurn`: turn authority in percent.
- Defaults:
  - Manual speed: 120 mm/s.
  - Turn authority: 80%.
- Manual speed is clamped to 60-120 mm/s.
  - 160 and 200 mm/s were removed because they caused false stall detection on this robot.
  - Existing saved NVS values above 120 are clamped down on boot.
- Manual turn is clamped to 40-100%.
- Added settings UI presets for manual speed and turn response.
- Raised stall tolerance:
  - Stall load threshold default: 80%.
  - Stall count default: 6 polls.
  - With 500 ms stall polling, this gives about 3 seconds grace before stopping.

## LIDAR And Serial Responsiveness

- Added stale-while-refresh support to `AsyncCache`.
- LIDAR scan API now returns cached scan data while a fresh scan is in progress.
- This reduces UI stalls caused by slow LDS serial reads.

## Cleaning History And Live Map Updates

- Reduced idle history detection interval:
  - Before: 30 seconds.
  - Now: 5 seconds.
  - This makes externally-started cleaning/manual sessions get detected sooner.
- Reduced active history snapshot interval:
  - Before: 2 seconds.
  - Now: 1 second.
  - This gives smoother live maps and finer path resolution.
- Fixed live history map lag:
  - Pose snapshots are buffered in RAM and flushed to flash every 30 seconds to reduce flash wear.
  - Active session reads now include the in-memory buffered snapshot tail via `BufferedLogReader`.
  - The browser no longer needs to wait for the next 30 second flash flush to see new map points.
- Frontend live clean-map polling while a session is recording:
  - Before: 5 seconds.
  - Now: 1 second.
- Requests still do not overlap; if serial is busy, updates naturally run slower instead of queueing indefinitely.

## Saved Maps

- Added saved map storage namespace:
  - `/maps` on SPIFFS.
- Added firmware API routes:
  - `GET /api/maps` lists saved map snapshots.
  - `POST /api/maps?source=<history-file>` saves a map from an existing history session.
  - `GET /api/maps/<filename>` downloads/views a saved map.
  - `DELETE /api/maps/<filename>` deletes a saved map.
- Saved maps reuse the existing JSONL history format.
- Saved map snapshots can be created from Cleaning History.
- Added Saved Maps frontend page at `/maps`.
- Added dashboard button for Saved Maps.
- Saved map detail view supports:
  - Pan.
  - Zoom.
  - Rotate.
  - Playback.
  - Delete.
  - Download through the underlying API.
- Current limitation:
  - Saved maps are viewer/reference maps.
  - They do not make the robot navigate to a point, reverse home, clean selected rooms, or enforce no-go zones.

## OpenAPI And Frontend Types

- Updated `frontend/api/openapi.yaml` for:
  - Manual speed and turn settings.
  - Saved map endpoints.
- Regenerated `frontend/src/types.generated.ts` through the frontend build.
- API route checker passes with the new firmware routes.

## Build Artifacts

Latest C3 OTA artifact:

```text
/home/luka/Projects/OpenNeato/repo/.pio/build/c3-release/openneato-esp32-c3-firmware.bin
```

Latest version stamp used for the local build:

```text
1.12-local5
```

Latest SHA256:

```text
0b549d4f8513a79686824c450d1bbf50af3923e281c9f273c701a9fa04fb2eab
```

## Verification

The latest build passed:

```text
npm run build
./scripts/check_format.py
FIRMWARE_VERSION=1.12-local5 pio run -e c3-release
```

## Important Notes

- The C3 remains an add-on. Full SLAM, room planning, route-to-point, or true return-to-home would need a larger external brain such as a PC or Raspberry Pi doing mapping/planning and using the C3 as the robot I/O bridge.
- Manual speed above 120 mm/s is intentionally blocked because this robot reported false stalls around 160 mm/s.
- Saved maps preserve useful visual history, but they are not robot navigation maps yet.
