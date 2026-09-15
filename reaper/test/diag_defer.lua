-- Is REAPER running deferred callbacks at all?
--
-- Diagnostic, not a test. If a ReaImGui panel "starts" (REAPER warns that it
-- is already running) but no window ever appears, run this first: a modal
-- dialog -- Preferences, or the ReaScript task control warning -- blocks
-- REAPER's UI thread while the audio thread keeps running, so the process
-- looks perfectly healthy and every synchronous script still passes. Only
-- deferred code stops.
--
--   python3 ~/.claude/skills/reascript-lua/assets/reascript_test.py \
--           reaper/test/diag_defer.lua && sleep 2 && cat /tmp/xt-defer.txt
--
-- "sync: script loaded" with no "defer frame" lines means defers are blocked.
-- Close the dialog and the panel appears by itself.

local LOG = "/tmp/xt-defer.txt"
local f = io.open(LOG, "w")
f:write("sync: script loaded\n")
f:close()

local n = 0
local function loop()
    n = n + 1
    local g = io.open(LOG, "a")
    g:write("defer frame " .. n .. "\n")
    g:close()
    if n < 3 then reaper.defer(loop) end
end
reaper.defer(loop)
print("scheduled -- check " .. LOG .. " in a second")
