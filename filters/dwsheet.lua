--[[
dwsheet.lua: reading data sheets (.csv, .tsv) and working out their formulas, the same way
the editor's grid does (web/app/editor/sheet.js). Used by data.lua.

A sheet is rows of cells, the first row usually the header. A cell starting with = is a
formula: numbers, cell references (A1, $B$2), + - * / ^, parentheses and the functions
SUM, AVERAGE (AVG), MIN, MAX, COUNT, ROUND and ABS over cells and ranges (A2:A10).
Rows count from 1 at the top of the file, columns from A, as in spreadsheet programs.
]]

local M = {}

-- The separator: tab for .tsv; otherwise whichever of , ; and tab the first line has most of.
function M.separator(text, path)
  if path and path:lower():match("%.tsv$") then return "\t" end
  local first = text:match("^[^\n]*") or ""
  first = first:gsub('"[^"]*"', "")
  local best, count = ",", -1
  for _, sep in ipairs({ ",", ";", "\t" }) do
    local _, n = first:gsub(sep == "\t" and "\t" or "%" .. sep, "")
    if n > count then best, count = sep, n end
  end
  return best
end

-- RFC 4180: quoted cells may hold separators, newlines and doubled quotes.
function M.parse(text, sep)
  text = text:gsub("^\239\187\191", ""):gsub("\r\n?", "\n")
  sep = sep or M.separator(text)
  local rows, row, cell = {}, {}, {}
  local i, n = 1, #text
  local quoted = false
  while i <= n do
    local c = text:sub(i, i)
    if quoted then
      if c == '"' then
        if text:sub(i + 1, i + 1) == '"' then
          cell[#cell + 1] = '"'
          i = i + 1
        else
          quoted = false
        end
      else
        cell[#cell + 1] = c
      end
    elseif c == '"' and #cell == 0 then
      quoted = true
    elseif c == sep then
      row[#row + 1] = table.concat(cell)
      cell = {}
    elseif c == "\n" then
      row[#row + 1] = table.concat(cell)
      rows[#rows + 1] = row
      row, cell = {}, {}
    else
      cell[#cell + 1] = c
    end
    i = i + 1
  end
  if #cell > 0 or #row > 0 then
    row[#row + 1] = table.concat(cell)
    rows[#rows + 1] = row
  end
  -- Trailing empty lines aren't rows.
  while #rows > 0 and #rows[#rows] == 1 and rows[#rows][1] == "" do rows[#rows] = nil end
  return rows
end

-- "1,234.5", "1 234,5", "12%", "$3" -> number, or nil.
function M.number(s)
  if type(s) == "number" then return s end
  if type(s) ~= "string" then return nil end
  local t = s:gsub("^%s+", ""):gsub("%s+$", "")
  if t == "" then return nil end
  local percent = t:match("%%$") ~= nil
  t = t:gsub("%%$", ""):gsub("^[%$€£¥]", ""):gsub("[%$€£¥]$", ""):gsub("[\194\160 ']", "")
  local function grouped(str, sep)  -- 1,234,567 with sep ","
    local head, rest = str:match("^[-+]?(%d%d?%d?)(.*)$")
    if not head or rest == "" then return false end
    return rest:gsub("%" .. sep .. "%d%d%d", "") == ""
  end
  local int, frac = t:match("^([^.]*)%.(%d*)$")
  if int and grouped(int, ",") then
    t = int:gsub(",", "") .. "." .. frac  -- 1,234.5
  elseif not t:find("%.") and grouped(t, ",") then
    t = t:gsub(",", "")  -- 1,234
  else
    local gi, gf = t:match("^([^,]*),(%d+)$")
    if gi and (not gi:find("%.") or grouped(gi, ".")) then
      t = gi:gsub("%.", "") .. "." .. gf  -- 1.234,5 and 1,5
    end
  end
  -- Decimal numbers only: no hex, inf or nan.
  if not (t:match("^[-+]?%d+%.?%d*$") or t:match("^[-+]?%d*%.%d+$") or t:match("^[-+]?%d+%.?%d*[eE][-+]?%d+$")
          or t:match("^[-+]?%d*%.%d+[eE][-+]?%d+$")) then
    return nil
  end
  local v = tonumber(t)
  if v and percent then v = v / 100 end
  return v
end

local function col_index(letters)
  local n = 0
  for ch in letters:upper():gmatch("%a") do n = n * 26 + (ch:byte() - 64) end
  return n
end
M.col_index = col_index

function M.col_name(n)
  local s = ""
  while n > 0 do
    local r = (n - 1) % 26
    s = string.char(65 + r) .. s
    n = (n - 1 - r) // 26
  end
  return s
end

-- -- Formulas ----------------------------------------------------------------------
local function tokenize(src)
  local tokens, i = {}, 1
  while i <= #src do
    local c = src:sub(i, i)
    if c:match("%s") then
      i = i + 1
    else
      local a, b = src:find("^%$?%a+%$?%d+:%$?%a+%$?%d+", i)
      if a then
        tokens[#tokens + 1] = { "range", (src:sub(a, b):gsub("%$", "")) }
        i = b + 1
      else
        a, b = src:find("^%$?%a%a?%a?%$?%d+", i)
        if a then
          tokens[#tokens + 1] = { "ref", (src:sub(a, b):gsub("%$", "")) }
          i = b + 1
        else
          a, b = src:find("^%a[%w_]*", i)
          if a then
            tokens[#tokens + 1] = { "name", src:sub(a, b):upper() }
            i = b + 1
          else
            a, b = src:find("^%d*%.?%d+", i)
            if a then
              local _, e = src:find("^[eE][-+]?%d+", b + 1)
              if e then b = e end
            end
            if a then
              tokens[#tokens + 1] = { "num", tonumber(src:sub(a, b)) }
              i = b + 1
            elseif c:match("[%+%-%*/%^%(%),;]") then
              tokens[#tokens + 1] = { "op", c == ";" and "," or c }
              i = i + 1
            else
              error("unexpected " .. c)
            end
          end
        end
      end
    end
  end
  return tokens
end

local FUNCS = {
  SUM = function(v) local s = 0 for _, x in ipairs(v) do s = s + x end return s end,
  AVERAGE = function(v)
    if #v == 0 then error("#DIV/0") end
    local s = 0 for _, x in ipairs(v) do s = s + x end return s / #v
  end,
  MIN = function(v) if #v == 0 then return 0 end local m = v[1] for _, x in ipairs(v) do if x < m then m = x end end return m end,
  MAX = function(v) if #v == 0 then return 0 end local m = v[1] for _, x in ipairs(v) do if x > m then m = x end end return m end,
  COUNT = function(v) return #v end,
}
FUNCS.AVG = FUNCS.AVERAGE
FUNCS.MEAN = FUNCS.AVERAGE

-- A sheet whose formula cells can be asked for their values.
function M.sheet(rows)
  local S = { rows = rows, cache = {}, busy = {} }

  function S.raw(r, c)
    local row = rows[r]
    return row and row[c] or ""
  end

  function S.value(r, c)
    local key = r * 100000 + c
    if S.cache[key] ~= nil then return S.cache[key] end
    local raw = S.raw(r, c)
    local v
    if type(raw) == "string" and raw:sub(1, 1) == "=" and #raw > 1 then
      if S.busy[key] then return "#CYCLE" end
      S.busy[key] = true
      local ok, res = pcall(S.eval, raw:sub(2))
      S.busy[key] = nil
      if ok then
        v = res
      else
        local msg = tostring(res):match("#[%u/%d!%?]+")
        v = msg or "#ERROR"
      end
    else
      v = M.number(raw) or raw
    end
    S.cache[key] = v
    return v
  end

  function S.eval(src)
    local tokens = tokenize(src)
    local pos = 1
    local function peek() return tokens[pos] end
    local function take() pos = pos + 1 return tokens[pos - 1] end
    local function numeric(v)
      if type(v) == "number" then return v end
      if type(v) == "string" and v:match("^#") then error(v) end
      local n = M.number(v)
      if n then return n end
      if v == "" or v == nil then return 0 end
      error("#VALUE!")
    end
    local function cell(ref)
      local letters, digits = ref:match("^(%a+)(%d+)$")
      return tonumber(digits), col_index(letters)
    end
    local expr
    local function args()
      local list = {}
      if peek() and peek()[1] == "op" and peek()[2] == ")" then take() return list end
      while true do
        local t = peek()
        if t and t[1] == "range" then
          take()
          local a, b = t[2]:match("^(.-):(.+)$")
          local r1, c1 = cell(a)
          local r2, c2 = cell(b)
          for r = math.min(r1, r2), math.max(r1, r2) do
            for c = math.min(c1, c2), math.max(c1, c2) do
              local v = S.value(r, c)
              if type(v) == "string" and v:match("^#") then error(v) end
              if type(v) == "number" then list[#list + 1] = v end
            end
          end
        else
          local v = expr()
          list[#list + 1] = numeric(v)
        end
        local sep = take()
        if not sep or sep[1] ~= "op" then error("#ERROR") end
        if sep[2] == ")" then return list end
        if sep[2] ~= "," then error("#ERROR") end
      end
    end
    local function primary()
      local t = take()
      if not t then error("#ERROR") end
      if t[1] == "num" then return t[2] end
      if t[1] == "ref" then return numeric(S.value(cell(t[2]))) end
      if t[1] == "op" and t[2] == "(" then
        local v = expr()
        local close = take()
        if not close or close[2] ~= ")" then error("#ERROR") end
        return v
      end
      if t[1] == "op" and t[2] == "-" then return -numeric(primary()) end
      if t[1] == "op" and t[2] == "+" then return numeric(primary()) end
      if t[1] == "name" then
        local open = take()
        if not open or open[2] ~= "(" then error("#NAME?") end
        local list = args()
        if t[2] == "ROUND" then
          local digits = list[2] or 0
          local f = 10 ^ digits
          local x = list[1] or 0
          return (x >= 0 and math.floor(x * f + 0.5) or -math.floor(-x * f + 0.5)) / f
        end
        if t[2] == "ABS" then return math.abs(list[1] or 0) end
        local fn = FUNCS[t[2]]
        if not fn then error("#NAME?") end
        return fn(list)
      end
      error("#ERROR")
    end
    local function power()
      local v = primary()
      while peek() and peek()[1] == "op" and peek()[2] == "^" do
        take()
        v = numeric(v) ^ numeric(primary())
      end
      return v
    end
    local function term()
      local v = power()
      while peek() and peek()[1] == "op" and (peek()[2] == "*" or peek()[2] == "/") do
        local op = take()[2]
        local rhs = numeric(power())
        if op == "*" then v = numeric(v) * rhs
        else
          if rhs == 0 then error("#DIV/0") end
          v = numeric(v) / rhs
        end
      end
      return v
    end
    expr = function()
      local v = term()
      while peek() and peek()[1] == "op" and (peek()[2] == "+" or peek()[2] == "-") do
        local op = take()[2]
        local rhs = numeric(term())
        v = op == "+" and numeric(v) + rhs or numeric(v) - rhs
      end
      return v
    end
    local v = expr()
    if pos <= #tokens then error("#ERROR") end
    return v
  end

  -- What a cell shows: its text, or a formula's result.
  function S.display(r, c, decimals)
    local raw = S.raw(r, c)
    if type(raw) == "string" and raw:sub(1, 1) == "=" and #raw > 1 then
      return M.format(S.value(r, c), decimals)
    end
    return raw
  end

  return S
end

-- A number as a table shows it: whole numbers as they are, others rounded to *decimals* (2).
function M.format(v, decimals)
  if type(v) ~= "number" then return tostring(v) end
  if v ~= v or v == math.huge or v == -math.huge then return "#NUM!" end
  if decimals then return string.format("%." .. decimals .. "f", v) end
  if v == math.floor(v) and math.abs(v) < 1e15 then return string.format("%d", v) end
  local s = string.format("%.2f", v):gsub("0+$", ""):gsub("%.$", "")
  return s
end

return M
