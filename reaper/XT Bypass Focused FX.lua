-- Reserved-row built-in: toggle bypass of the FX the panel is driving.
-- Fired by the daemon as /action/<command id>; OSC can trigger an action but
-- cannot hand a value to a defer loop, which is why these exist at all.
local retval, tracknumber, _item, fxnumber = reaper.GetFocusedFX2()
if retval == 0 or retval & 1 == 0 then return end
local track = tracknumber == 0 and reaper.GetMasterTrack(0)
              or reaper.GetTrack(0, tracknumber - 1)
if not track then return end
local fx = fxnumber & 0xFFFFFF
reaper.TrackFX_SetEnabled(track, fx, not reaper.TrackFX_GetEnabled(track, fx))
