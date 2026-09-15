-- Where the bridge keeps its files.
--
-- Under ~/.config/xtouchmini rather than REAPER's resource directory, because
-- the daemon reads state.json and has no business knowing where REAPER keeps
-- its things. See docs/ipc.md.

local M = {}

function M.config_dir()
    local xdg = os.getenv("XDG_CONFIG_HOME")
    if xdg and xdg ~= "" then return xdg .. "/xtouchmini" end
    return (os.getenv("HOME") or ".") .. "/.config/xtouchmini"
end

function M.mappings() return M.config_dir() .. "/mappings.json" end
function M.state()    return M.config_dir() .. "/state.json" end

function M.read(path)
    local f = io.open(path, "r")
    if not f then return nil end
    local text = f:read("a")
    f:close()
    return text
end

--- Write via a temp file and rename, so a reader can never see a half-file.
-- os.rename is atomic within a filesystem, and both paths are in the same dir.
function M.write_atomic(path, text)
    local tmp = path .. ".tmp"
    local f, err = io.open(tmp, "w")
    if not f then
        -- Most likely the directory does not exist yet.
        os.execute(string.format("mkdir -p %q", M.config_dir()))
        f, err = io.open(tmp, "w")
        if not f then return false, err end
    end
    f:write(text)
    f:close()
    local ok, rerr = os.rename(tmp, path)
    if not ok then
        os.remove(tmp)
        return false, rerr
    end
    return true
end

return M
