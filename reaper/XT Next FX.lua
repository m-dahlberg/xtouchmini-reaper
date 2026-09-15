-- Reserved-row built-in: focus the next FX in the same chain.
local retval, tracknumber, _item, fxnumber = reaper.GetFocusedFX2()
if retval == 0 or retval & 1 == 0 then return end
local track = tracknumber == 0 and reaper.GetMasterTrack(0)
              or reaper.GetTrack(0, tracknumber - 1)
if not track then return end
local count = reaper.TrackFX_GetCount(track)
if count < 2 then return end
local next_fx = ((fxnumber & 0xFFFFFF) + 1) % count
reaper.TrackFX_Show(track, next_fx, 3)   -- 3 = show floating window
