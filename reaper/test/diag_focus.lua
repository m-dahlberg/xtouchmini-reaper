-- What does REAPER think is focused?
--
-- WARNING: running this perturbs what it measures. `reaper -nonewinst`
-- activates the REAPER window, which takes focus away from a floating FX
-- window -- so bit 4 ("no longer focused") may be set BY this probe rather
-- than by whatever you were debugging. Prefer the panel's own `debug`
-- checkbox, which reports the same flags from inside the frame without
-- touching focus.
local retval, tracknumber, itemnumber, fxnumber = reaper.GetFocusedFX2()
print(string.format("GetFocusedFX2: retval=%d (track=%s item=%s fx=%s)",
      retval, tostring(tracknumber), tostring(itemnumber), tostring(fxnumber)))
print("  bit1 track FX   : " .. tostring(retval & 1 ~= 0))
print("  bit2 item FX    : " .. tostring(retval & 2 ~= 0))
print("  bit4 NOT focused: " .. tostring(retval & 4 ~= 0))
if retval & 1 ~= 0 then
  local tr = tracknumber == 0 and reaper.GetMasterTrack(0) or reaper.GetTrack(0, tracknumber - 1)
  if tr then
    local _, nm = reaper.TrackFX_GetFXName(tr, fxnumber & 0xFFFFFF, "")
    print("  fx name         : " .. nm)
  end
end
print("selected track  : " .. tostring(reaper.GetSelectedTrack(0,0) and
      math.floor(reaper.GetMediaTrackInfo_Value(reaper.GetSelectedTrack(0,0),"IP_TRACKNUMBER")) or "none"))
