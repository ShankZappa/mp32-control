# Multiple MP32 devices — implementation design (not enabled yet)

Supporting multiple MP32 units in one controller is technically feasible, but it changes
the application from a single device client into a device manager. It should only be
enabled when two physical units are available for live protocol and failure testing.

## GitHub handoff — physical implementation owner needed

When this project is published on GitHub, this feature should be picked up by a
contributor who has the ability to build the application and test it with **at least two
physical Antelope MP32 units connected at the same time**. Demo-mode validation alone is
not sufficient for merging or marking the feature complete.

The contributor should ideally have access to:

- two physical MP32 units on one LAN;
- a macOS build machine;
- a Windows build machine;
- an iPad or iPhone for the stable-host web interface;
- permission to test gain, Mic/Line/Hi-Z, 48 V, presets and disconnect/reconnect behavior.

If that hardware coverage is unavailable, implementation may be developed behind a
feature flag, but it must remain disabled by default and documented as unverified.

## Intended user experience

- Every discovered MP32 gets its own collapsible 32-channel container.
- The container header shows device name, serial, firmware, online state and preset.
- Containers can be reordered with up/down controls (and later drag-and-drop).
- The saved order defines which unit is “first” in that studio.
- Collapsed/expanded state and order are stored locally by device serial.
- Each device retains its own Undo/Redo history, names, colours, groups and notes.
- Stereo Link and Group never cross physical-device boundaries unless an explicit future
  cross-device mode is designed and tested.

## Required backend changes

1. Replace the single `Handler.device` with a `DeviceManager` keyed by serial number.
2. Create one independent `MP32Device` TCP connection per discovered serial.
3. Add `device_id` to every status/control API request.
4. Namespace peer metadata as `<serial>:<field>` so two devices cannot overwrite names,
   colours, groups or input types belonging to each other.
5. Keep reconnect, notification parsing and command queues isolated per device.

## Required frontend changes

1. Replace the single global `st.config` with state keyed by serial.
2. Render one reusable 32-channel component per device.
3. Scope element IDs, pending-command locks and Undo/Redo stacks by serial.
4. Persist device order and collapsed state in local storage.
5. Extend save/load files with device serials and preserve backward compatibility.

## Why it is deferred

The current protocol path has only been live-verified against one physical MP32. A blind
multi-device refactor could introduce cross-device commands or metadata collisions. The
safe acceptance test needs two real units, simultaneous gain/type/phantom changes,
disconnect/reconnect of either unit, preset operations and Mac/Windows/iPad sync.

## Required acceptance tests before merge

- Both MP32 units are discovered by serial and maintain independent TCP connections.
- Each collapsible 32-channel container controls only its own physical device.
- Gain, input type and phantom changes never leak to the same channel number on the other
  unit.
- Names, colours, groups, Public Notes and Undo/Redo are namespaced by device serial.
- Device containers can be collapsed and reordered; order survives restart on each client.
- The configured first/primary unit is clearly identified and remains first after restart.
- Disconnecting either MP32 leaves the other fully controllable and shows the correct
  offline state only on the disconnected container.
- Reconnecting a unit restores its live state without overwriting the other unit.
- Preset recall/save is verified independently on both devices.
- Simultaneous Mac, Windows and iPad controllers show the same two-device state.
- Stable web-host failover is verified Mac→Windows and Windows→Mac while both MP32
  connections and container order remain correct.
- macOS Apple Silicon, macOS Intel and Windows packages are rebuilt and smoke-tested.

Record the device serials, firmware versions, operating-system versions and test results
in the pull request. Do not include private network credentials or signing certificates.
