-- Fired by the daemon when the device switches to layer A. The panel polls
-- this flag; OSC can trigger an action but cannot hand a value to a defer loop.
reaper.SetExtState("xtouch_mini", "layer", "A", false)
