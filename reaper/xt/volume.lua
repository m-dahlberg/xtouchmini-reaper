-- Track volume, in the one scale the bridge is allowed to say "volume" in.
--
-- Three different quantities get that name, and they are not interchangeable:
--
--   D_VOL            a linear GAIN factor. 1.0 is unity, ~3.98 is +12 dB.
--   dB               what the track panel shows.
--   fader position   0..1 along REAPER's fader taper, which is neither of the
--                    above: unity sits at 0.716, not at 0.25 or at 0.5.
--
-- REAPER's OSC /track/N/volume is the third one, in both directions -- it is
-- what the daemon sends and what feedback arrives in. So that is what
-- state.json carries, and everything else converts at the edge.
--
-- Getting this wrong is invisible rather than loud. The numbers stay inside
-- 0..1 and look plausible; what actually happens is that the daemon's shadow
-- is fed 0.25 by the panel and 0.716 by REAPER's feedback for the very same
-- level, so it flips between two scales. Soft pickup then waits for the fader
-- to reach a target that is not where the track is, and relative mode adds
-- its nudges to a base that moves under it. Both read as "jumps, and
-- sometimes nothing happens".
--
-- DB2SLIDER/SLIDER2DB are REAPER's own taper and the only honest way across.
-- ReaScript has no VAL2DB/DB2VAL, so the gain<->dB half is done here.

local M = {}

-- Matches REAPER's own floor for a fader pulled to -inf.
M.MIN_DB = -150.0

function M.gain_to_db(gain)
    gain = tonumber(gain) or 0
    if gain <= 0 then return M.MIN_DB end
    return math.max(M.MIN_DB, 20 * math.log(gain, 10))
end

function M.db_to_gain(db)
    db = tonumber(db) or M.MIN_DB
    if db <= M.MIN_DB then return 0.0 end
    return 10 ^ (db / 20)
end

--- A D_VOL gain factor as a 0..1 fader position, which is what OSC means.
function M.gain_to_slider(gain)
    local pos = reaper.DB2SLIDER(M.gain_to_db(gain)) / 1000.0
    return math.max(0.0, math.min(1.0, pos))
end

--- The inverse, for anything that has a fader position and wants a gain.
function M.slider_to_gain(pos)
    pos = math.max(0.0, math.min(1.0, tonumber(pos) or 0))
    return M.db_to_gain(reaper.SLIDER2DB(pos * 1000.0))
end

--- What the track panel would show, for a fader position. "-inf" at the floor.
function M.slider_to_text(pos)
    local db = reaper.SLIDER2DB(math.max(0.0, math.min(1.0, pos or 0)) * 1000.0)
    if db <= M.MIN_DB then return "-inf dB" end
    return string.format("%+.1f dB", db)
end

return M
