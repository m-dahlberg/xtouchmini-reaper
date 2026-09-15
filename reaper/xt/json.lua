-- Minimal JSON encoder/decoder.
--
-- REAPER's Lua has no JSON and there is no pip on this machine to install one.
-- The ShuttleXpress bridge shares state with its daemon through a flat
-- key = value file, but mappings here are nested (plugin -> layer -> slot), so
-- that format cannot carry them.
--
-- Only what the bridge actually stores is supported: objects, arrays, strings,
-- numbers, booleans and null. No unicode escape generation -- REAPER hands us
-- UTF-8 already and JSON permits it raw.

local M = {}

-- A table with no entries is ambiguous: {} could be either shape. Tag the ones
-- that must survive a round-trip as arrays, since an empty slot list written
-- as {} and read back as an object would change type on every save.
M.EMPTY_ARRAY = setmetatable({}, {__tostring = function() return "[]" end})

-- Lua arrays cannot hold nil without breaking #, and an unassigned encoder in
-- the middle of a slot list is exactly that case: {a, nil, c} has a length of
-- either 1 or 3 depending on the interpreter's mood. NULL is a real value that
-- occupies the position and encodes as JSON null.
M.NULL = setmetatable({}, {__tostring = function() return "null" end})

local ESCAPES = {
    ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
    ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function escape(s)
    return (s:gsub('[%c"\\]', function(c)
        return ESCAPES[c] or string.format('\\u%04x', c:byte())
    end))
end

local function is_array(t)
    if t == M.EMPTY_ARRAY then return true end
    if t == M.NULL then return false end
    local n = 0
    for k in pairs(t) do
        if type(k) ~= "number" then return false end
        n = n + 1
    end
    return n == #t and n > 0
end

local function encode_value(v, indent, level)
    local t = type(v)
    if v == nil or v == M.NULL then return "null" end
    if t == "boolean" then return tostring(v) end
    if t == "string" then return '"' .. escape(v) .. '"' end
    if t == "number" then
        if v ~= v or v == math.huge or v == -math.huge then return "null" end
        if math.type(v) == "integer" then return tostring(v) end
        -- %.17g round-trips a double exactly; trailing zeros are not worth the
        -- risk of a value drifting on every save/load cycle.
        return (string.format("%.17g", v))
    end
    if t ~= "table" then return "null" end

    local nl, pad, pad2 = "", "", ""
    if indent then
        nl = "\n"
        pad = string.rep(indent, level + 1)
        pad2 = string.rep(indent, level)
    end

    if is_array(v) then
        if #v == 0 then return "[]" end
        local parts = {}
        for i = 1, #v do
            parts[i] = pad .. encode_value(v[i], indent, level + 1)
        end
        return "[" .. nl .. table.concat(parts, "," .. nl) .. nl .. pad2 .. "]"
    end

    local keys = {}
    for k in pairs(v) do
        if type(k) == "string" then keys[#keys + 1] = k end
    end
    if #keys == 0 then return "{}" end
    -- Sorted so a mapping file only changes when a mapping changes, which
    -- makes a diff of the file readable.
    table.sort(keys)
    local parts = {}
    for i, k in ipairs(keys) do
        parts[i] = pad .. '"' .. escape(k) .. '": '
                 .. encode_value(v[k], indent, level + 1)
    end
    return "{" .. nl .. table.concat(parts, "," .. nl) .. nl .. pad2 .. "}"
end

--- Encode a Lua value. Pass indent="  " for a human-editable file.
function M.encode(value, indent)
    return encode_value(value, indent, 0)
end

local Parser = {}
Parser.__index = Parser

function Parser.new(text)
    return setmetatable({s = text, i = 1}, Parser)
end

function Parser:error(msg)
    -- Report a line number: a mapping file people may hand-edit deserves better
    -- than a byte offset.
    local line = 1
    for _ in self.s:sub(1, self.i):gmatch("\n") do line = line + 1 end
    error(string.format("json: %s at line %d (offset %d)", msg, line, self.i), 0)
end

function Parser:skip()
    local _, e = self.s:find("^[ \t\r\n]*", self.i)
    self.i = e + 1
end

function Parser:literal(word, value)
    if self.s:sub(self.i, self.i + #word - 1) == word then
        self.i = self.i + #word
        return value
    end
    self:error("unexpected character '" .. self.s:sub(self.i, self.i) .. "'")
end

function Parser:string()
    self.i = self.i + 1  -- opening quote
    local out = {}
    while true do
        local c = self.s:sub(self.i, self.i)
        if c == "" then self:error("unterminated string") end
        if c == '"' then
            self.i = self.i + 1
            return table.concat(out)
        end
        if c == "\\" then
            local esc = self.s:sub(self.i + 1, self.i + 1)
            self.i = self.i + 2
            if esc == "n" then out[#out + 1] = "\n"
            elseif esc == "t" then out[#out + 1] = "\t"
            elseif esc == "r" then out[#out + 1] = "\r"
            elseif esc == "b" then out[#out + 1] = "\b"
            elseif esc == "f" then out[#out + 1] = "\f"
            elseif esc == "u" then
                local hex = self.s:sub(self.i, self.i + 3)
                if not hex:match("^%x%x%x%x$") then self:error("bad \\u escape") end
                self.i = self.i + 4
                out[#out + 1] = utf8.char(tonumber(hex, 16))
            else
                out[#out + 1] = esc  -- covers \" \\ \/
            end
        else
            out[#out + 1] = c
            self.i = self.i + 1
        end
    end
end

function Parser:number()
    local text = self.s:match("^-?%d+%.?%d*[eE]?[-+]?%d*", self.i)
    if not text or text == "" then self:error("bad number") end
    self.i = self.i + #text
    local n = tonumber(text)
    if not n then self:error("bad number '" .. text .. "'") end
    -- Keep integers as integers so a param index round-trips as 3, not 3.0.
    if not text:find("[%.eE]") then return math.tointeger(n) or n end
    return n
end

function Parser:value()
    self:skip()
    local c = self.s:sub(self.i, self.i)
    if c == "" then self:error("unexpected end of input") end
    if c == "{" then
        self.i = self.i + 1
        local obj = {}
        self:skip()
        if self.s:sub(self.i, self.i) == "}" then self.i = self.i + 1 return obj end
        while true do
            self:skip()
            if self.s:sub(self.i, self.i) ~= '"' then self:error("expected key") end
            local key = self:string()
            self:skip()
            if self.s:sub(self.i, self.i) ~= ":" then self:error("expected ':'") end
            self.i = self.i + 1
            obj[key] = self:value()
            self:skip()
            local d = self.s:sub(self.i, self.i)
            self.i = self.i + 1
            if d == "}" then return obj end
            if d ~= "," then self:error("expected ',' or '}'") end
        end
    end
    if c == "[" then
        self.i = self.i + 1
        local arr = {}
        self:skip()
        if self.s:sub(self.i, self.i) == "]" then self.i = self.i + 1 return arr end
        while true do
            arr[#arr + 1] = self:value()
            self:skip()
            local d = self.s:sub(self.i, self.i)
            self.i = self.i + 1
            if d == "]" then return arr end
            if d ~= "," then self:error("expected ',' or ']'") end
        end
    end
    if c == '"' then return self:string() end
    if c == "t" then return self:literal("true", true) end
    if c == "f" then return self:literal("false", false) end
    if c == "n" then return self:literal("null", M.NULL) end
    return self:number()
end

--- Decode JSON text. Returns ok, value-or-error-message.
function M.decode(text)
    if type(text) ~= "string" or text:match("^[ \t\r\n]*$") then
        return false, "json: empty input"
    end
    local p = Parser.new(text)
    local ok, result = pcall(function()
        local v = p:value()
        p:skip()
        if p.i <= #p.s then p:error("trailing content") end
        return v
    end)
    return ok, result
end

return M
